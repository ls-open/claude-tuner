"""本地配置台的 HTTP 层：路由、门禁、读写。

它挂在网关自己的端口上（网关本来就是个常驻的本地 HTTP 服务），所以用户记住
`http://127.0.0.1:<port>/` 就够了，不用另外起进程、不用装任何东西。

职责边界：
- 这个文件只做「取值 → 调用 config_ops 的纯函数 → 落盘」；
- 所有判断（能不能改、改成什么、幂等与否）都在 config_ops 里，便于单测；
- 上游探测（拉模型列表 / 测试连接）是**直连上游**，不经网关 —— 经网关会被
  探针拦截（max_tokens<=1 本地秒回），测出来的是假结果。

安全：只绑 127.0.0.1 + token 门禁 + Host/Origin 校验（防 DNS rebinding）。
读接口永不返回完整 Key。
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.dont_write_bytecode = True

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import config_ops  # noqa: E402
from common import (  # noqa: E402
    DEFAULT_RULES,
    DEFAULT_TOOL_PRUNING_CONFIG,
    atomic_write_json,
    normalize_upstream,
    upstream_client_headers,
)

HTML_PATH = os.path.join(os.path.dirname(SCRIPT_DIR), "webui", "index.html")
TOKEN_FILE = os.path.join(config_ops.platform_paths()["bridge_home"], "webui.token")
COOKIE_NAME = "ccds_token"
CLAUDE_TUNER_UPSTREAM_TIMEOUT = 15

_html_cache = (0.0, "")


# ==============================================================================
# token
# ==============================================================================

def ensure_token():
    """读（缺失则生成）配置台 token：0600，只在本机。"""
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token
    except OSError:
        pass
    import secrets
    token = secrets.token_hex(32)
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token)
    return token


def query_value(query, key):
    """取一个查询参数。网关传进来的是 parse_qs 的结果（值是列表），这里两种形状都吃。"""
    value = (query or {}).get(key)
    if isinstance(value, list):
        value = value[0] if value else ""
    return (value or "").strip()


def token_from(headers, query):
    """token 来源优先级：头 > cookie > query。"""
    direct = headers.get("X-Claude-Tuner-Token") or headers.get("x-claude-tuner-token")
    if direct:
        return direct.strip()
    cookie = headers.get("Cookie") or headers.get("cookie") or ""
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value.strip()
    return query_value(query, "token")


def authorized(handler, query):
    return config_ops.constant_time_eq(token_from(handler.headers, query), ensure_token())


# ==============================================================================
# 页面
# ==============================================================================

def load_html():
    """读单文件配置台（按 mtime 缓存，改了刷一下就能看到）。"""
    global _html_cache
    try:
        mtime = os.path.getmtime(HTML_PATH)
    except OSError:
        return ("<!doctype html><meta charset=utf-8><h1>配置台文件缺失</h1>"
                "<p>找不到 %s。请重跑安装脚本（install.sh）同步插件文件。</p>" % HTML_PATH)
    if _html_cache[0] == mtime and _html_cache[1]:
        return _html_cache[1]
    with open(HTML_PATH, "r", encoding="utf-8") as fh:
        text = fh.read()
    _html_cache = (mtime, text)
    return text


# ==============================================================================
# 配置的读与写
# ==============================================================================

def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _paths():
    return config_ops.platform_paths()


def bridge_path():
    return os.path.join(_paths()["bridge_home"], "config.json")


def three_p_path():
    from common import find_active_3p_config_path
    return find_active_3p_config_path()


def version_payload():
    """版本与更新状态。客户端版本与「按哪版验过」都如实报出来 ——
    客户端升级后注入面会变，我们只提示，不假装知道兼容性。"""
    from common import client_version, plugin_version, release_info, version_tuple
    info = release_info()
    client = client_version()
    return {
        "plugin": plugin_version(),
        "client": client,
        "tested_client": info["tested_client"],
        "client_newer_than_tested": bool(
            client and info["tested_client"]
            and version_tuple(client) > version_tuple(info["tested_client"])),
        "repo": info["repo"],
        "source_configured": bool(info["repo"]) and not info["repo"].startswith("OWNER"),
    }


def tool_inventory_payload():
    """按入口分档的工具清单 + **按当前配置现算的**剥离结论。

    分档的粒度与理由见 gateway.record_tool_inventory：入口 + 主/子代理是稳定的粗粒度，
    档内工具取并集。实测桌面端 85 个工具、终端 17 个 —— 差这么远，只给「最近一次」
    会让人照着这边的清单去勾那边用不上的工具。

    只来自真实请求：客户端发什么工具是它决定的，我们猜不出来，所以没跑过请求时如实
    报「还没采集到」，而不是编一份默认列表糊弄。

    剥没剥按**现值**算，不读落盘里那份判断：用户刚改完开关就在这里看到结果，而不是要
    等下一次请求才刷新。判断用的是 tool_rules —— 网关真拿它做决定的那份，所以这里显示
    的「会剥」和请求里实际发生的必然是同一个答案。
    """
    from tool_rules import (CATEGORY_LABELS, PROTECTED_TOOLS, get_stripped_tool_names,
                            strip_reason)

    store = _read(os.path.join(_paths()["bridge_home"], "tool_inventory.json"))
    profiles = [p for p in (store.get("profiles") or []) if isinstance(p, dict)]
    pruning = _read(bridge_path()).get("tool_pruning") or {}
    pruning = {**DEFAULT_TOOL_PRUNING_CONFIG, **pruning}
    custom = [c for c in (pruning.get("custom_strip_tools") or []) if isinstance(c, str)]
    stripped_names = get_stripped_tool_names(pruning)

    out = []
    for prof in profiles:
        known = prof.get("tools")
        if not isinstance(known, dict):
            continue
        rows = []
        for name, meta in known.items():
            if not isinstance(name, str) or not name:
                continue
            meta = meta if isinstance(meta, dict) else {}
            reason = strip_reason(name, stripped_names, pruning)
            rows.append({
                "name": name,
                "bytes": meta.get("bytes") or 0,
                "tokens": meta.get("tokens") or 0,
                "stripped": bool(reason),
                "reason": reason,
                "reason_label": CATEGORY_LABELS.get(reason, reason),
                "custom": name in custom,
                # 受保护的（读/写/跑命令这些）在页面上不给勾 —— 剥了会话就残了
                "protected": name in PROTECTED_TOOLS,
            })
        if not rows:
            continue
        rows.sort(key=lambda r: (-r["bytes"], r["name"]))
        out.append({
            "slot": prof.get("slot") or "",
            "label": prof.get("label") or "未识别",
            "evidence": prof.get("evidence") or "",
            "kind": (prof.get("slot") or "|").split("|")[-1],
            "first_seen": prof.get("first_seen") or 0,
            "last_seen": prof.get("last_seen") or 0,
            "requests": prof.get("requests") or 0,
            "models": [m for m in (prof.get("models") or []) if isinstance(m, str)],
            "cc_versions": [v for v in (prof.get("cc_versions") or []) if isinstance(v, str)],
            "tools": rows,
            "count": len(rows),
            "raw_bytes": sum(r["bytes"] for r in rows),
            "kept_bytes": sum(r["bytes"] for r in rows if not r["stripped"]),
            "raw_tokens": sum(r["tokens"] for r in rows),
            "kept_tokens": sum(r["tokens"] for r in rows if not r["stripped"]),
        })
    out.sort(key=lambda p: p["last_seen"], reverse=True)
    return {
        "seen": bool(out),
        "profiles": out,
        "custom": custom,
        "pruning_enabled": pruning.get("enabled") is not False,
    }


def stats_payload():
    """实时战绩：从网关日志读上游自己报回来的真实用量。"""
    import live_stats
    upstream = _read(bridge_path()).get("upstream_url") or ""
    return live_stats.read_recent_usage(upstream)


def client_running():
    """客户端在不在跑。偏好类改动只在它没跑时写得进去，所以页面要据此提示。"""
    try:
        res = subprocess.run(["pgrep", "-f", "Claude.app/Contents/MacOS/Claude"],
                             capture_output=True, timeout=5)
        return res.returncode == 0
    except Exception:
        return False


def read_config_payload(handler):
    """给前端的全量配置（Key 一律脱敏）。"""
    paths = _paths()
    bridge_cfg = _read(bridge_path())
    three_p = _read(three_p_path()) if three_p_path() else {}
    claude_settings = _read(paths["claude_settings"])

    upstream = bridge_cfg.get("upstream_url") or ""
    base = upstream[:-len("/v1/messages")] if upstream.endswith("/v1/messages") else upstream

    env = claude_settings.get("env") if isinstance(claude_settings.get("env"), dict) else {}
    safe_env = {k: v for k, v in env.items() if k != "ANTHROPIC_AUTH_TOKEN"}

    return {
        "upstream": {
            "url": base,
            "preset": config_ops.find_preset(base),
            "api_key_masked": config_ops.mask_api_key(three_p.get("inferenceGatewayApiKey", "")),
            "api_key_set": bool((three_p.get("inferenceGatewayApiKey") or "").strip()),
        },
        "models": [{"name": m.get("name", ""),
                    "label": m.get("labelOverride") or m.get("name", ""),
                    # 1M 是**按模型**的（官方就是这么设计的：每个条目各自带
                    # supports1m / prefer1m），所以回显也要逐条给。
                    "one_m": bool(m.get("prefer1m", m.get("supports1m", False)))}
                   for m in (three_p.get("inferenceModels") or []) if isinstance(m, dict)],
        "gateway": {
            "port": _port(handler),
            "upstream_url": upstream,
            "default_model": bridge_cfg.get("default_model", ""),

            "unknown_model_fallback": bridge_cfg.get("unknown_model_fallback", True) is not False,
        },
        "three_p": {
            "config_path": three_p_path(),
            "activated": three_p.get("inferenceProvider") == "gateway",
            "problems": config_ops.validate_3p_config(three_p) if three_p else ["尚无 3P 配置"],
        },
        "rules": bridge_cfg.get("rules") or {},
        "tool_pruning": bridge_cfg.get("tool_pruning") or {},
        "settings": {
            "linked": "ANTHROPIC_BASE_URL" in env,
            "env": safe_env,
            # 白名单里登记过的键一律回传现值（页面按现值决定每个开关的初始状态）——
            # 少回传一个，那个开关就会以「未设置」的假状态显示，用户一保存就写错值。
            **{k: claude_settings.get(k) for k in config_ops.SETTINGS_TOP_KEYS},
        },
        "versions": version_payload(),
        "client": {
            "running": client_running(),
            "browser_tools": (_read(paths["desktop_config_3p"]).get("preferences") or {})
                             .get("coworkBrowserToolsEnabled"),
        },
        "options": {
            "presets": [{k: p[k] for k in ("id", "name", "url", "model", "key_hint")}
                        for p in config_ops.UPSTREAM_PRESETS],
            "client_models": list(config_ops.CLIENT_MODEL_IDS),
            "recommended": list(config_ops.RECOMMENDED_MODEL_IDS),
            "rules_defaults": DEFAULT_RULES,
            "pruning_defaults": DEFAULT_TOOL_PRUNING_CONFIG,
        },
    }


def _port(handler):
    """当前网关端口：端口是 manager 独占决策，这里只从 config.json 读结果。"""
    port = _read(bridge_path()).get("gateway_port")
    return port if isinstance(port, int) and port > 0 else 8789


def apply_patch(handler, patch):
    """把前端交上来的补丁落盘，返回 (响应体, 需要重启什么)。"""
    paths = _paths()
    effects = {"restart_gateway": False, "restart_client": False, "restart_session": False}
    notes = []

    bridge = _read(bridge_path())
    three_p = _read(three_p_path()) if three_p_path() else {}

    # 上游地址
    url = (patch.get("upstream_url") or "").strip()
    if url:
        normalized = normalize_upstream(url)
        if bridge.get("upstream_url") != normalized:
            bridge["upstream_url"] = normalized
            notes.append("上游地址已更新")
            effects["restart_gateway"] = True

    # API Key 与模型映射都写在 3P 配置里（网关只是把它转发的 Key 转出去）
    three_p_changed = False
    key = patch.get("api_key", "")
    if key and not config_ops.key_unchanged(key, three_p.get("inferenceGatewayApiKey", "")):
        three_p["inferenceGatewayApiKey"] = key.strip()
        three_p_changed = True
        notes.append("API Key 已更新")
        effects["restart_client"] = True
    models = patch.get("models")
    if isinstance(models, list):
        # 1M 逐条读 —— 官方就是这么设计的：每个模型条目各自带 supports1m / prefer1m。
        # 不同上游对 1M 的支持不一样，一个全局开关表达不了这件事。
        cleaned = [{"name": (m.get("name") or "").strip(),
                    "labelOverride": (m.get("label") or m.get("name") or "").strip(),
                    "supports1m": bool(m.get("one_m")), "prefer1m": bool(m.get("one_m"))}
                   for m in models if isinstance(m, dict) and (m.get("name") or "").strip()]
        # 顶层那个 modelPrefer1mContext 跟随「是否至少有一个模型开了」。
        # 它对逐条标记的确切作用我们没有实测过，所以只让它跟着走、不靠它表达区别。
        want_top = any(m["prefer1m"] for m in cleaned) if cleaned else False
        if three_p.get("modelPrefer1mContext") != want_top:
            three_p["modelPrefer1mContext"] = want_top
            three_p_changed = True
        if cleaned and cleaned != three_p.get("inferenceModels"):
            three_p["inferenceModels"] = cleaned
            three_p_changed = True
            notes.append("模型映射已更新")
            effects["restart_client"] = True
    if three_p_changed and three_p_path():
        atomic_write_json(three_p_path(), three_p)

    # 网关侧规则
    if isinstance(patch.get("rules"), dict):
        bridge, changed = config_ops.apply_rules_patch(bridge, patch["rules"])
        notes += ["规则 %s" % k for k in changed]
    if isinstance(patch.get("tool_pruning"), dict):
        bridge, changed = config_ops.apply_pruning_patch(bridge, patch["tool_pruning"])
        notes += ["工具精简 %s" % k for k in changed]
    if "unknown_model_fallback" in patch:
        bridge["unknown_model_fallback"] = bool(patch["unknown_model_fallback"])
    if not bridge.get("rules"):
        bridge["rules"] = dict(DEFAULT_RULES)
    atomic_write_json(bridge_path(), bridge, indent=1)

    # 客户端偏好（浏览器工具等）。它在 preferences 里，客户端运行时会覆盖回去，
    # 所以跑着的时候不写，直接告诉用户先退出客户端。
    prefs = patch.get("client_prefs")
    if isinstance(prefs, dict) and prefs:
        if client_running():
            notes.append("浏览器工具那一项没改：客户端正在运行，改了会被它覆盖回去 —— "
                         "先完全退出 Claude，再回这个页面改")
        else:
            desktop = _read(paths["desktop_config_3p"])
            merged, changed = config_ops.merge_desktop_prefs(desktop, prefs)
            if changed:
                atomic_write_json(paths["desktop_config_3p"], merged)
                notes += ["客户端偏好 %s" % k for k in changed]
                effects["restart_client"] = True

    # 客户端行为开关与 CLI 联动
    settings_patch = dict(patch.get("settings") or {})
    linked = patch.get("cli_linked")
    claude_settings = _read(paths["claude_settings"])
    if linked is not None:
        if linked:
            settings_patch["env"] = config_ops.cli_env(
                "http://127.0.0.1:%d" % _port(handler), three_p.get("inferenceGatewayApiKey", ""),
                _primary_model(three_p))
        else:
            claude_settings, changed = config_ops.merge_settings(
                claude_settings, {}, unset_env=config_ops.SETTINGS_ENV_KEYS)
            if changed:
                atomic_write_json(paths["claude_settings"], claude_settings)
    if patch.get("settings") or patch.get("unset_settings"):
        merged, changed = config_ops.merge_settings(
            claude_settings, settings_patch,
            unset_top=patch.get("unset_settings") or ())
        if changed:
            atomic_write_json(paths["claude_settings"], merged)
            notes += ["客户端设置 %s" % k for k in changed]
            effects["restart_session"] = True

    problems = config_ops.validate_3p_config(three_p) if three_p else ["尚无 3P 配置"]
    return {"ok": True, "data": {"notes": notes, "effects": effects, "problems": problems}}


def _primary_model(three_p):
    """CLI 环境变量里用的上游模型名：取映射表第一个目标，没有就用默认值。"""
    for m in three_p.get("inferenceModels") or []:
        if isinstance(m, dict) and (m.get("labelOverride") or "").strip():
            return m["labelOverride"].strip()
    return config_ops.DEFAULT_MODEL


# ==============================================================================
# 上游探测（直连，不经网关）
# ==============================================================================

def _upstream_headers(api_key, session_id="webui"):
    headers = {"Content-Type": "application/json", "x-api-key": api_key}
    headers.update(upstream_client_headers(session_id=session_id,
                                           opencode=session_id.endswith("-opencode")))
    return headers


def _upstream_base(url):
    """上游 base：网关配置里存的是完整端点，这里剥回 base 用于拼 /v1/models。"""
    url = (url or "").strip().rstrip("/")
    return url[:-len("/v1/messages")] if url.endswith("/v1/messages") else url


def _http_json(url, api_key, payload=None):
    """直连上游。用默认 opener，与网关转发那条路完全一致 ——
    否则会出现「配置页说连不上、网关却转得通」这种自相矛盾的提示。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = _upstream_headers(api_key, session_id="webui" + ("-opencode" if "opencode" in url.lower() else ""))
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=CLAUDE_TUNER_UPSTREAM_TIMEOUT) as resp:
        body = resp.read(2 * 1024 * 1024).decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def list_upstream_models(url, api_key):
    """拉上游的模型列表（各家字段不一，只挑出 id/name 两个字段）。"""
    if not url:
        return {"ok": False, "error": "还没有填上游地址"}
    if not api_key:
        return {"ok": False, "error": "还没有填 API Key"}
    try:
        payload = _http_json(_upstream_base(url) + "/v1/models", api_key)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "上游返回 HTTP %s：这家可能不提供模型列表，手填即可"
                                      % exc.code}
    except Exception as exc:
        return {"ok": False, "error": "连不上上游：%s" % exc}
    items = payload.get("data") if isinstance(payload, dict) else None
    models = []
    for item in items or []:
        if isinstance(item, dict):
            models.append({"id": item.get("id") or "", "name": item.get("name") or ""})
        elif isinstance(item, str):
            models.append({"id": item, "name": item})
    models = [m for m in models if m["id"]]
    if not models:
        return {"ok": False, "error": "上游没返回模型列表，手填模型名即可"}
    return {"ok": True, "data": {"models": models}}


def test_upstream(url, api_key, model):
    """打一次最小真实请求：只有它通了，用户才算真的接上了。"""
    if not url:
        return {"ok": False, "error": "还没有填上游地址"}
    if not api_key:
        return {"ok": False, "error": "还没有填 API Key"}
    if not model:
        return {"ok": False, "error": "还没有选上游模型"}
    endpoint = normalize_upstream(url)
    payload = {"model": model, "max_tokens": 8,
               "messages": [{"role": "user", "content": "ping"}]}
    started = time.time()
    try:
        resp = _http_json(endpoint, api_key, payload)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("error", {}).get(
                "message", "")
        except Exception:
            pass
        return {"ok": False, "error": "上游返回 HTTP %s：%s" % (exc.code, detail or exc.reason)}
    except Exception as exc:
        return {"ok": False, "error": "连不上上游：%s" % exc}
    elapsed = int((time.time() - started) * 1000)
    text = ""
    for block in (resp.get("content") or []):
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text", "")
    return {"ok": True, "data": {"model": model, "ms": elapsed, "reply": text[:200]}}


def restart_gateway():
    """委托 manager 重启网关 —— 网关自己不会重启自己。"""
    manager = os.path.join(SCRIPT_DIR, "manager.py")
    subprocess.Popen([sys.executable, manager, "restart"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def config_url():
    """给用户打开的配置台地址（含 token）。安装脚本靠它拿到那条链接。"""
    return "http://127.0.0.1:%d/?token=%s" % (_port(None), ensure_token())


def main(argv=None):
    """安装脚本要用的两个小入口：拿地址、拿 token。"""
    import argparse
    parser = argparse.ArgumentParser(description="claude-tuner 配置台")
    parser.add_argument("--url", action="store_true", help="打印带 token 的配置台地址")
    parser.add_argument("--token", action="store_true", help="只打印 token")
    args = parser.parse_args(argv)
    if args.token:
        print(ensure_token())
    else:
        print(config_url())
    return 0


# ==============================================================================
# 路由
# ==============================================================================

def _gate(handler, query, need_token=True):
    """Host 校验（防 DNS rebinding）+ token 校验。通过返回 None，否则返回错误响应。"""
    host = handler.headers.get("Host", "")
    if not config_ops.host_ok(host, handler.server.server_port):
        handler._send_json(421, {"ok": False, "error": {"type": "bad_host",
                                                        "message": "Host 不是本机回环地址"}})
        return False
    if not need_token:
        return True
    if not authorized(handler, query):
        handler._send_json(403, {"ok": False, "error": {"type": "forbidden",
                                                        "message": "token 无效，请用安装时打开的链接访问"}})
        return False
    return True


def handle_get(handler, query):
    """处理 GET；返回 True 表示这个请求归配置台管。"""
    path = handler.path.split("?")[0]
    if path not in ("/", "/index.html", "/api/config", "/api/status",
                    "/api/upstream-models", "/api/update-check",
                    "/api/tools", "/api/stats"):
        return False
    if not _gate(handler, query, need_token=(path != "/")):
        return True

    if path in ("/", "/index.html"):
        token = query_value(query, "token")
        if token and config_ops.constant_time_eq(token, ensure_token()):
            # 带 token 的链接一次性用完就丢：避免 token 留在浏览器历史与 Referer 里
            handler.send_response(302)
            handler.send_header("Location", "/")
            handler.send_header("Set-Cookie",
                                "%s=%s; HttpOnly; SameSite=Strict; Path=/" % (COOKIE_NAME, token))
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.end_headers()
            return True
        if not authorized(handler, query):
            handler._send_html(401, "<!doctype html><meta charset=utf-8>"
                                    "<h1>需要一个带 token 的链接</h1>"
                                    "<p>配置台地址在安装时自动打开过，也可以重新运行 "
                                    "<code>install.sh</code> 拿回链接。</p>")
            return True
        handler._send_html(200, load_html())
        return True

    if path == "/api/config":
        handler._send_json(200, {"ok": True, "data": read_config_payload(handler)})
        return True

    if path == "/api/status":
        handler._send_json(200, {"ok": True, "data": {
            "port": _port(handler),
            "token_file": TOKEN_FILE,
        }})
        return True

    if path == "/api/update-check":
        import updater
        handler._send_json(200, updater.check())
        return True

    if path == "/api/upstream-models":
        bridge_cfg = _read(bridge_path())
        result = list_upstream_models(bridge_cfg.get("upstream_url", ""), _current_key())
        handler._send_json(200, result)
        return True

    if path == "/api/tools":
        handler._send_json(200, {"ok": True, "data": tool_inventory_payload()})
        return True

    if path == "/api/stats":
        handler._send_json(200, {"ok": True, "data": stats_payload()})
        return True
    return False


def _current_key():
    path = three_p_path()
    return (_read(path).get("inferenceGatewayApiKey") or "").strip() if path else ""


def handle_post(handler, query, body):
    """处理 POST；body 是已解析的 JSON。返回 True 表示这个请求归配置台管。"""
    path = handler.path.split("?")[0]
    if path not in ("/api/config", "/api/test-upstream", "/api/restart", "/api/update"):
        return False
    if not _gate(handler, query):
        return True
    if not config_ops.origin_ok(handler.headers.get("Origin", ""), handler.server.server_port):
        handler._send_json(403, {"ok": False, "error": {"type": "bad_origin",
                                                        "message": "跨源请求被拒绝"}})
        return True

    if path == "/api/config":
        handler._send_json(200, apply_patch(handler, body or {}))
        return True

    if path == "/api/test-upstream":
        url = (body or {}).get("upstream_url") or _upstream_base(
            _read(bridge_path()).get("upstream_url", ""))
        key = (body or {}).get("api_key") or ""
        if config_ops.key_unchanged(key, _current_key()):
            key = _current_key()
        handler._send_json(200, test_upstream(url.strip(), key.strip(),
                                              ((body or {}).get("model") or "").strip()))
        return True

    if path == "/api/update":
        import updater
        result = updater.apply_update()
        handler._send_json(200, result)
        if result.get("ok"):
            # 先把回执发出去再重启 —— 重启会掐掉当前连接
            try:
                handler.wfile.flush()
            except Exception:
                pass
            threading.Timer(0.5, updater.restart_gateway).start()
        return True

    if path == "/api/restart":
        # 先回执再重启：重启会把当前连接掐掉，等它做完用户只会看到超时
        handler._send_json(200, {"ok": True, "data": {"restarting": True}})
        try:
            handler.wfile.flush()
        except Exception:
            pass
        threading.Timer(0.3, restart_gateway).start()
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
