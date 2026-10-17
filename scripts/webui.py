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
    DEFAULT_PORT,
    DEFAULT_PROBE_TIMEOUT,
    DEFAULT_RULES,
    DEFAULT_TOOL_PRUNING_CONFIG,
    atomic_write_json,
    normalize_upstream,
    upstream_client_headers,
)

HTML_PATH = os.path.join(os.path.dirname(SCRIPT_DIR), "webui", "index.html")
TOKEN_FILE = os.path.join(config_ops.platform_paths()["bridge_home"], "webui.token")
COOKIE_NAME = "claude_tuner_token"

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


def stats_payload():
    """用量统计：从网关日志读上游自己报回来的真实用量。

    **不折算金额** —— 本项目支持任意上游，硬编码一张价目表只会给出没有依据的数字。
    客户端本身有官方机制（3P 的 inferenceModelPricing / inferenceModelPricingEnabled），
    要算钱应该由用户填自己那家的价，而不是我们替他猜。
    """
    import live_stats
    return live_stats.read_recent_usage()


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
            "log_retention_days": int(bridge_cfg.get("log_retention_days", 7)),
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
            # 允许定时任务写在 3P 配置里，不在桌面偏好里 —— 两者位置不同，
            # 页面上却该并排放在一起（用户眼里都是「开不开某项能力」）
            "scheduled_tasks": three_p.get("scheduledTasksEnabled") is True,
            # 随附技能与动态工作流：**两个键合看成一个开关**。disableBundledSkills 去掉的是
            # 那份随附技能清单（含 workflow-authoring），enableWorkflows 去掉的是 Workflow
            # 工具本身 —— 工具说明指着那条技能，只关一个就是半截引用，故同进同退。
            # **两个键住的地方不同**：disableBundledSkills 是 3P 配置的键（客户端的设置页
            # 也写在那儿），enableWorkflows 是 Claude Code 那层的设置。读到错的地方，
            # 客户端自己关掉的那个在页面上就会永远显示「没关」。
            "bundled_skills": (three_p.get("disableBundledSkills") is True
                               and claude_settings.get("enableWorkflows") is False),
            # 三个界面开关：**缺省即开**（客户端不认识 false 以外的值），
            # 所以判据是「显式为 false 才算关掉」。
            "surfaces": {k: three_p.get(k) is not False for k in config_ops.SURFACE_KEYS},
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
    return port if isinstance(port, int) and port > 0 else DEFAULT_PORT


_write_lock = threading.Lock()


def apply_patch(handler, patch):
    """把前端交上来的补丁落盘，返回 (响应体, 需要重启什么)。

    整段读-改-写必须在锁内：配置是「读整份、改几项、写回整份」，两个标签页同时保存时
    没有锁就会互相覆盖 —— 后写的把先写的那几项一起抹掉，而两边都提示「已保存」。
    """
    with _write_lock:
        return _apply_patch_unlocked(handler, patch)


def _apply_patch_unlocked(handler, patch):
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
    # 3P 配置的落盘**必须放在最后** —— 下面 surfaces / scheduled_tasks / bundled_skills
    # 三块都还要往 three_p 里写；先落盘的话，这三块改的只是内存副本，一个字节都存不下来。
    # 网关侧规则
    if isinstance(patch.get("rules"), dict):
        bridge, changed = config_ops.apply_rules_patch(bridge, patch["rules"])
        notes += ["规则 %s" % k for k in changed]
    if isinstance(patch.get("tool_pruning"), dict):
        bridge, changed = config_ops.apply_pruning_patch(bridge, patch["tool_pruning"])
        notes += ["工具精简 %s" % k for k in changed]
    if "unknown_model_fallback" in patch:
        bridge["unknown_model_fallback"] = bool(patch["unknown_model_fallback"])
    if "log_retention_days" in patch:
        try:
            days = max(1, min(365, int(patch["log_retention_days"])))
            if bridge.get("log_retention_days") != days:
                bridge["log_retention_days"] = days
                notes.append("日志保留天数已设为 %d 天" % days)
        except (ValueError, TypeError):
            pass
    if not bridge.get("rules"):
        bridge["rules"] = dict(DEFAULT_RULES)
    atomic_write_json(bridge_path(), bridge, indent=1)

    # 客户端偏好（浏览器工具等）。它在 preferences 里，客户端运行时会覆盖回去，
    # 所以跑着的时候不写，直接告诉用户先退出客户端。
    surfaces = patch.get("surfaces")
    if isinstance(surfaces, dict):
        # 先算出「改完之后三个面各是什么状态」，再一次性判断能不能接受 ——
        # 逐个改边判断容易写出「改到一半才发现不行」的中间态。
        want = {}
        for k in config_ops.SURFACE_KEYS:
            v = surfaces.get(k)
            want[k] = (three_p.get(k) is not False) if not isinstance(v, bool) else v
        # 客户端要求至少留一个面，全关掉它不加载这份配置。拦在这里，别等它报错。
        if not any(want.values()):
            notes.append("没有改动：Chat / Cowork / Code 至少要留一个开着")
        else:
            for k, v in want.items():
                if (three_p.get(k) is not False) != v:
                    three_p[k] = v
                    three_p_changed = True
                    effects["restart_client"] = True
                    notes.append("%s：%s" % (config_ops.SURFACE_LABELS.get(k, k), "开" if v else "关"))

    sched = patch.get("scheduled_tasks")
    if isinstance(sched, bool) and (three_p.get("scheduledTasksEnabled") is True) != sched:
        three_p["scheduledTasksEnabled"] = sched
        three_p_changed = True
        notes.append("定时任务：%s" % ("允许" if sched else "不允许"))
        effects["restart_client"] = True

    # 随附技能（那半在 3P 配置里；另一半 enableWorkflows 走上面的 settings 白名单）
    bundle = patch.get("bundled_skills")
    if isinstance(bundle, bool) and (three_p.get("disableBundledSkills") is True) != bundle:
        three_p["disableBundledSkills"] = bundle
        three_p_changed = True
        notes.append("随附技能：%s" % ("剥离" if bundle else "保留"))
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

    if three_p_changed and three_p_path():
        atomic_write_json(three_p_path(), three_p)

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
    with urllib.request.urlopen(req, timeout=DEFAULT_PROBE_TIMEOUT) as resp:
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
    if path not in ("/", "/index.html", "/api/config",
                    "/api/upstream-models", "/api/update-check", "/api/stats"):
        return False
    if not _gate(handler, query, need_token=(path != "/")):
        return True

    if path in ("/", "/index.html"):
        token = query_value(query, "token")
        if token and config_ops.constant_time_eq(token, ensure_token()):
            # 带 token 的链接一次性用完就丢：避免 token 留在浏览器历史与 Referer 里
            handler.send_response(302)
            handler.send_header("Location", "/")
            # **必须带 Max-Age**：不带就是会话级 cookie，浏览器一关就没，用户每次都得
            # 重新去找那个带 token 的链接。带一年：拿链接开一次，之后直接访问端口即可。
            handler.send_header("Set-Cookie",
                                "%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"
                                % (COOKIE_NAME, token))
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.end_headers()
            return True
        if not authorized(handler, query):
            handler._send_html(401, """<!doctype html><meta charset=utf-8>
<title>需要访问令牌</title>
<style>body{font:14px/1.8 -apple-system,"PingFang SC",system-ui;background:#0b0d11;color:#e8ebf0;
padding:60px 24px;max-width:640px;margin:0 auto}
h1{font-size:19px;margin:0 0 14px}code{background:#1a1e26;border:1px solid #262b35;
border-radius:6px;padding:2px 7px;font-family:ui-monospace,Menlo,monospace;font-size:13px}
pre{background:#1a1e26;border:1px solid #262b35;border-radius:8px;padding:12px 14px;overflow:auto}
p{margin:0 0 12px;color:#8b94a7}b{color:#e8ebf0}</style>
<h1>需要访问令牌</h1>
<p>这个页面能改你的客户端配置（上游地址、API&nbsp;Key、3P 配置），所以不能任人打开 ——
浏览器里任何一个网页都可能偷偷对着本机端口发请求。</p>
<p><b>只需要拿一次链接。</b>用下面这条命令打印出带令牌的地址，打开之后浏览器会记住一年，
之后直接访问 <code>127.0.0.1:%d</code> 就行。</p>
<pre>python3 ~/.claude/skills/claude-tuner/scripts/webui.py --url</pre>
<p>看到这一页通常说明：换了浏览器、清了 Cookie，或者距上次打开已超过一年。</p>"""
                                    % _port(handler))
            return True
        handler._send_html(200, load_html())
        return True

    if path == "/api/config":
        handler._send_json(200, {"ok": True, "data": read_config_payload(handler)})
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
