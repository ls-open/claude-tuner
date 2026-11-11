#!/usr/bin/env python3
"""安装协调者：把插件需要的客户端状态一次写好，让用户只填一个 API Key。

它管四件事，由安装器（`install.py`；`install.sh` 只负责找到 Python 并转交）调用：

1. **打开 3P 模式** —— 写 `developer_settings.json`（就是菜单 Enable Developer Mode
   写的那份）与 `<userData>-3p/claude_desktop_config.json` 的 `deploymentMode`；
2. **建 3P 推理配置** —— 在 `configLibrary/` 里写好指向本地网关的连接、默认模型映射；
3. **接上 CLI** —— 按 DeepSeek 官方 Claude Code 文档写 `~/.claude/settings.json` 的 env；
4. **告诉网关上游是谁** —— 写 `~/.claude-tuner/config.json` 的 `upstream_url` 与兜底模型。

设计约束（改这个文件前先读）：

- `plan(state)` 只做判断、不碰磁盘；`apply(actions)` 只执行、不再判断。
  `--dry-run` 走的是同一条 plan，所以演练看到的和真跑的是同一份动作清单。
- **改任何既有文件之前先备份**，且只合并不删除未知键 —— 那份 3P 配置是客户端的，
  删掉它不认识的字段会触发它的校验告警甚至起不来。
- 幂等：装过就只做校验与探活，零写入，除非 `--force`。

用法：
    bootstrap.py install [--dry-run] [--force] [--key K] [--upstream URL] [--no-cli]
    bootstrap.py status [--json]
    bootstrap.py doctor
    bootstrap.py uninstall [--dry-run] [--purge-3p]
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

sys.dont_write_bytecode = True

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import config_ops  # noqa: E402
import platform_support  # noqa: E402
from common import (  # noqa: E402
    BACKUP_KEEP_FILES,
    DEFAULT_PORT,
    RUNTIME_INFO_NAME,
    plugin_version,
    atomic_write_json,
    ensure_config_initialized,
    configure_stdio,
    find_active_3p_config_path,
    normalize_upstream,
    prune_backups,
)


def log(msg=""):
    """输出一行。Windows 控制台默认编码打不了 emoji / 中文，兜一层转义 ——
    安装器在那边失败时只留一句 UnicodeEncodeError，用户完全看不懂发生了什么。"""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("unicode_escape").decode("ascii"))


# ==============================================================================
# 读取现状（唯一做 IO 的读路径）
# ==============================================================================

def read_json(path):
    """读 JSON；不存在或损坏一律返回 None（调用方只看内容，不区分这两种情况）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def gather_state(paths=None):
    """把 plan() 需要的全部现状读进来（不改任何东西）。"""
    paths = paths or config_ops.platform_paths()
    bridge_cfg = read_json(os.path.join(paths["bridge_home"], "config.json"))
    bridge_cfg = bridge_cfg if isinstance(bridge_cfg, dict) else {}

    # 「当前生效的是哪一份」只认 common 的那一个入口，别在这里再写一遍判断
    three_p_path = find_active_3p_config_path()
    meta = read_json(os.path.join(paths["config_library"], "_meta.json"))
    three_p = read_json(three_p_path) if three_p_path else None
    desktop_1p = read_json(paths["desktop_config_1p"])
    desktop_3p = read_json(paths["desktop_config_3p"])
    dev_1p = read_json(paths["developer_settings_1p"])
    dev_3p = read_json(paths["developer_settings_3p"])
    claude_settings = read_json(paths["claude_settings"])

    return {
        "paths": paths,
        "bridge_cfg": bridge_cfg,
        "three_p_path": three_p_path,
        "three_p": three_p if isinstance(three_p, dict) else {},
        "meta": meta if isinstance(meta, dict) else None,
        "desktop_1p": desktop_1p if isinstance(desktop_1p, dict) else {},
        "desktop_3p": desktop_3p if isinstance(desktop_3p, dict) else {},
        "developer_settings_1p": dev_1p if isinstance(dev_1p, dict) else {},
        "developer_settings_3p": dev_3p if isinstance(dev_3p, dict) else {},
        "claude_settings": claude_settings if isinstance(claude_settings, dict) else {},
        "port": bridge_cfg.get("gateway_port") or DEFAULT_PORT,
    }


# ==============================================================================
# 计划（纯判断，不碰磁盘）
# ==============================================================================

def _write_action(path, data, reason, backup=True):
    return {"kind": "write_json", "path": path, "data": data, "reason": reason,
            "backup": backup}


def plan(state, api_key=None, upstream=None, model=None, preset=None, with_cli=True, force=False):
    """算出要做哪些动作，返回 (actions, notes)。

    actions 是给 apply() 执行的数据；notes 是给 --dry-run 与用户看的说明。
    本函数不读写任何文件，故可被测试以生产同一形态直接调用。

    上游与映射表按预设决定，**不是写死的**：第三方模型用户映射到该上游的模型，
    官方模型中转站用户则把代号一一映射到同名模型（identity）。
    """
    paths = state["paths"]
    port = state["port"]
    gateway_url = "http://127.0.0.1:%d" % port
    upstream = (upstream or config_ops.DEFAULT_UPSTREAM).strip().rstrip("/")
    preset_id = preset or config_ops.find_preset(upstream)
    preset_info = config_ops.get_preset(preset_id)
    model = (model or preset_info["model"]).strip()
    api_key = (api_key or "").strip()

    actions, notes = [], []
    already = config_ops.is_already_installed(state["three_p"], state["bridge_cfg"])
    if already and not force:
        notes.append("检测到已经装过（3P 已指向网关、且网关已知上游）—— 只做校验，不写任何文件。")
        notes.append("要强制重写请加 --force。")
        return actions, notes

    # 1) 3P 推理配置
    three_p_path = state["three_p_path"]
    if not three_p_path:
        config_id = config_ops.new_config_id()
        three_p_path = os.path.join(paths["config_library"], "%s.json" % config_id)
        notes.append("新建 3P 配置：%s" % three_p_path)
    else:
        notes.append("就地合并 3P 配置：%s" % three_p_path)
    one_m = (state["bridge_cfg"].get("one_m_context") is not False)
    sched = (state["bridge_cfg"].get("scheduled_tasks") is True)
    mapping = config_ops.preset_mapping(preset_id, model, one_m)
    bundle, changed = config_ops.build_3p_bundle(
        state["three_p"], api_key=api_key or None, gateway_url=gateway_url,
        models=mapping, one_m=one_m, scheduled_tasks=sched)
    # 工具精简的**另一半**：让客户端根本不发那些工具，而不是到网关再剥。
    # 落点是 3P 配置的 disabledBuiltinTools ——（客户端 Setup 窗口里那一栏）。
    # 只增删自己写过的条目，用户自己加的（例如 ClaudeDesign）一条不动。
    bundle, disabled_changed, disabled_written = config_ops.sync_client_disabled(
        bundle, (state["bridge_cfg"] or {}).get("tool_pruning") or {},
        (state["bridge_cfg"] or {}).get(config_ops.CLIENT_DISABLED_KEY) or [])
    if disabled_changed:
        changed = sorted(set(list(changed) + disabled_changed))
        notes.append("  客户端侧拦截 %d 个工具（新会话生效）" % len(disabled_written))
    actions.append(_write_action(three_p_path, bundle,
                                 "3P 推理配置（连接本地网关 + 模型映射 + 客户端侧工具拦截）"))
    notes.append("  上游预设：%s → %s" % (preset_info["name"], upstream))
    mapped_to = {m["labelOverride"] for m in mapping}
    notes.append("  模型映射：%d 条，指向 %s" % (
        len(mapping), "、".join(sorted(mapped_to)) if len(mapped_to) < 4
        else "、".join(sorted(mapped_to)[:3]) + " 等"))
    if mapped_to == set(config_ops.CLIENT_MODEL_IDS):
        notes.append("  （不认识这家上游的模型名，先让代号同名直通 —— "
                     "装完在配置页上拉一次模型列表改成真实名称）")
    notes.append("  3P 变更字段：%s" % (", ".join(changed) if changed else "无"))

    # _meta.json 只在缺失时写：它记录客户端当前生效的是哪一份，已有内容一律不动
    meta_path = os.path.join(paths["config_library"], "_meta.json")
    if not state["meta"]:
        config_id = os.path.splitext(os.path.basename(three_p_path))[0]
        actions.append(_write_action(
            meta_path, {"appliedId": config_id, "entries": [{"id": config_id, "name": "Default"}]},
            "把新建的 3P 配置设为生效", backup=False))

    if not api_key and not (state["three_p"].get("inferenceGatewayApiKey") or "").strip():
        notes.append("⚠️ 还没有 API Key：装完会自动打开配置页，填一个就能用。")

    # 2) 打开 3P 模式
    dev1 = dict(state["developer_settings_1p"])
    if dev1.get("allowDevTools") is not True:
        dev1["allowDevTools"] = True
        actions.append(_write_action(paths["developer_settings_1p"], dev1,
                                     "等同于菜单 Enable Developer Mode"))
    dev3 = dict(state["developer_settings_3p"])
    if dev3.get("allowDevTools") is not True:
        dev3["allowDevTools"] = True
        actions.append(_write_action(paths["developer_settings_3p"], dev3,
                                     "3P 数据目录下的同一开关"))

    desktop3 = dict(state["desktop_3p"])
    if desktop3.get("deploymentMode") != "3p":
        desktop3["deploymentMode"] = "3p"
        actions.append(_write_action(paths["desktop_config_3p"], desktop3,
                                     "让客户端启动即进入 3P（deploymentMode）"))
    desktop1 = dict(state["desktop_1p"])
    if desktop1.get("deploymentMode") != "3p":
        desktop1["deploymentMode"] = "3p"
        actions.append(_write_action(paths["desktop_config_1p"], desktop1,
                                     "同上，1P 数据目录也写一份"))

    # 3) 客户端行为开关与 CLI 联动
    settings_patch = {
        "env": config_ops.cli_env(gateway_url, api_key or state["three_p"].get(
            "inferenceGatewayApiKey", ""), model, one_m) if with_cli else {},
        # 省钱相关，默认帮用户开：
        # - promptCacheTtl：不设时客户端自己决定（订阅号 1 小时、API Key 只有 5 分钟）。
        #   1 小时的缓存写入更贵，但能跨更长的中断保温；上游认不认这个时长各家不同。
        # - 自动压缩：上下文快满时自动压缩，压缩那一次会丢掉已有缓存。
        # 输入联想不在这里：客户端设置页已有自己的「Prompt suggestions」开关
        # （写 preferences.ccPromptSuggestions），由它管，我们不重复实现。
        "promptCacheTtl": "1h",
        "subagentPromptCacheTtl": "1h",
        "autoCompactEnabled": False,
    }
    merged_settings, changed_keys = config_ops.merge_settings(state["claude_settings"], settings_patch)
    if changed_keys:
        actions.append(_write_action(paths["claude_settings"], merged_settings,
                                     "客户端行为开关 + CLI 联动（新会话生效）"))
        notes.append("  客户端设置变更：%s" % ", ".join(changed_keys))
    if not with_cli:
        notes.append("  已跳过 CLI 联动（--no-cli）")

    # 4) 网关侧配置。**不写 default_model**：它留空时网关的兜底会取映射表里第一个目标，
    # 也就是用户自己配的那个模型 —— 比在安装时钉一个常量准确，也少一处会漂的状态。
    bridge = dict(state["bridge_cfg"])
    normalized = normalize_upstream(upstream)
    if bridge.get("upstream_url") != normalized:
        bridge["upstream_url"] = normalized
    if disabled_written != (state["bridge_cfg"] or {}).get(config_ops.CLIENT_DISABLED_KEY):
        # 记下这轮写进客户端的是哪几条 —— 下一轮只回收这份名单里的（用户自己加的条目不动）
        bridge[config_ops.CLIENT_DISABLED_KEY] = disabled_written
    actions.append({"kind": "ensure_bridge_config", "path": os.path.join(
        paths["bridge_home"], "config.json"), "data": bridge,
        "reason": "告诉网关上游地址（默认模型留空，由网关按映射表兜底）", "backup": False})

    notes.append("装完要**完全退出并重开 Claude**，3P 与配置才会被读进去。")
    return actions, notes


# ==============================================================================
# 执行
# ==============================================================================

def _backup(path, ts, dry_run):
    """改既有文件前先备份到 ~/.claude-tuner/backups/3p/，只留最近 BACKUP_KEEP_FILES 份。"""
    dest = config_ops.backup_path(path, ts)
    if dry_run:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(path, dest)
    prune_backups(os.path.dirname(dest), BACKUP_KEEP_FILES)
    return dest


def apply(actions, dry_run=False):
    """执行 plan() 产出的动作。返回 (已执行数, 备份路径列表, 问题列表)。"""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    done, backups, problems = 0, [], []
    for act in actions:
        path = act["path"]
        existed = os.path.exists(path)
        if act.get("backup") and existed:
            backups.append(_backup(path, ts, dry_run))
        if dry_run:
            done += 1
            continue
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if act["kind"] == "ensure_bridge_config":
                ensure_config_initialized(path)
                on_disk = read_json(path)
                base = on_disk if isinstance(on_disk, dict) else {}
                base.update(act["data"])
                atomic_write_json(path, base, indent=1)
            else:
                atomic_write_json(path, act["data"])
            done += 1
            # 写后复读：防半截写。3P 那份还要过一遍客户端会认的必备字段。
            written = read_json(path)
            if written is None:
                problems.append("%s 写入后无法解析" % path)
            elif isinstance(act["data"], dict) and "inferenceProvider" in act["data"]:
                # 这份是给客户端的 3P 配置：写后复读一遍，缺必备字段要立刻说出来，
                # 不能等用户重启客户端才发现起不来
                problems += ["%s：%s" % (path, p) for p in config_ops.validate_3p_config(written)]
        except Exception as exc:
            problems.append("%s 写入失败：%s" % (path, exc))
    return done, backups, problems


def restart_gateway():
    """拉起/重启本地网关（网关没有自我重启能力，必须由 manager 代劳）。"""
    manager = os.path.join(SCRIPT_DIR, "manager.py")
    try:
        subprocess.run([sys.executable, manager, "restart"], timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return 1
    return 0


def status(paths=None):
    """体检：装没装、网关活没活、3P 是不是已经生效。"""
    paths = paths or config_ops.platform_paths()
    state = gather_state(paths)
    installed = config_ops.is_already_installed(state["three_p"], state["bridge_cfg"])
    problems = config_ops.validate_3p_config(state["three_p"]) if state["three_p"] else ["尚无 3P 配置"]
    return {
        "installed": installed,
        "plugin_dir": paths["plugin_dir"],
        "plugin_present": os.path.isfile(os.path.join(paths["plugin_dir"], ".claude-plugin",
                                                     "plugin.json")),
        "three_p_config": state["three_p_path"],
        "three_p_problems": [] if installed else problems,
        "gateway_port": state["port"],
        "upstream_url": state["bridge_cfg"].get("upstream_url", ""),
        "api_key_set": bool((state["three_p"].get("inferenceGatewayApiKey") or "").strip()),
        "client_settings_managed": config_ops.managed_settings_present(state["claude_settings"]),
    }


# ==============================================================================
# 诊断（doctor）
# ==============================================================================

def _plugin_version():
    """版本号只有一处权威（`common.release_info`），这里不再自己 open 一遍。"""
    try:
        return plugin_version() or "未知"
    except Exception:                              # noqa: BLE001 —— 诊断工具不许抛
        return "未知"


def _hooks_command():
    """hooks.json 里现在写的是哪条命令。用户报「网关没被拉起来」时，第一个要看的就是它。

    exec form 下 `command` 只是解释器名，真正的调用在 `args` 里 —— 只打印 command
    会得到一句没用的「python3」，所以两种形态都拼成一条可读的调用。
    """
    with open(os.path.join(os.path.dirname(SCRIPT_DIR), "hooks", "hooks.json"),
              "r", encoding="utf-8") as fh:
        data = json.load(fh)
    for entries in (data.get("hooks") or {}).values():
        for entry in entries or []:
            for hook in (entry or {}).get("hooks") or []:
                cmd = (hook or {}).get("command") or ""
                if not cmd:
                    continue
                args = hook.get("args")
                if isinstance(args, list):
                    return " ".join([cmd] + [str(a) for a in args])
                return cmd
    return "（hooks.json 里没有 command）"


def _gateway_state(bridge_home):
    pid_path = os.path.join(bridge_home, "gateway.pid")
    try:
        with open(pid_path, "r", encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return "没有 pid 文件（还没启动过，或被清掉了）"
    alive = platform_support.is_alive(pid)
    script = ""
    try:
        with open(os.path.join(bridge_home, RUNTIME_INFO_NAME), "r", encoding="utf-8") as fh:
            script = json.load(fh).get("script") or ""
    except (OSError, ValueError):
        pass
    return "pid %d · %s%s" % (pid, "在跑" if alive else "不在跑了（pid 可能已被复用）",
                              (" · " + script) if script else "")


def _newest_log(bridge_home):
    """优先当天的 JSON 日志（`gateway.<日期>.log`）。

    目录里还有 `gateway.out.log`（manager 拉到 stdout 的原始输出）。按文件名排序时
    `out` 排在日期后面，取「最后一个」会拿到它 —— 那是排查启动失败时才看的，平时是噪音。
    """
    log_dir = os.path.join(bridge_home, "logs")
    names = os.listdir(log_dir)
    day = sorted(n for n in names if re.match(r"^gateway\.\d{4}-\d{2}-\d{2}\.log$", n))
    if not day:
        day = sorted(n for n in names if n.startswith("gateway.") and n.endswith(".log"))
    if not day:
        return "（还没有日志文件）"
    path = os.path.join(log_dir, day[-1])
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        rows = fh.readlines()
    tail = rows[-8:]
    return "（%s，共 %d 行）\n" % (os.path.basename(path), len(rows)) + (
        "\n".join("  " + r.rstrip("\n")[:180] for r in tail) or "  （空）")


def doctor(paths=None):
    """把排查要用的信息打一段可直接粘进 issue 的文本。

    **这个命令自己不能失败。** 用户来报告的就是「它坏了」——诊断工具再抛异常，
    他们能给的线索就只剩一句「不行」。所以每一段各兜各的，取不到就把原因写出来。

    不打印任何密钥：这里只回答「填了没」，不回答「填的是什么」。
    """
    paths = paths or config_ops.platform_paths()
    rows = []

    def add(title, fn):
        try:
            rows.append((title, fn()))
        except Exception as exc:                   # noqa: BLE001 —— 诊断工具不许抛
            rows.append((title, "（取不到：%s: %s）" % (type(exc).__name__, exc)))

    add("版本", _plugin_version)
    add("系统", lambda: "%s · %s" % (platform.platform(), sys.platform))
    add("Python", lambda: "%s（%s）" % (sys.version.split()[0], sys.executable))
    add("解释器", lambda: " · ".join(
        "%s=%s" % (n, "有" if shutil.which(n) else "无") for n in PYTHON_CANDIDATES))
    add("插件目录", lambda: paths["plugin_dir"])
    add("hooks 命令", _hooks_command)
    try:
        base = status(paths)
    except Exception:                              # noqa: BLE001
        base = None
    add("3P 配置", lambda: "（读不到）" if base is None else
        (base["three_p_config"] or "无") + ("" if base["installed"] else "（未生效）"))
    add("网关", lambda: _gateway_state(paths["bridge_home"]))
    add("日志尾部", lambda: _newest_log(paths["bridge_home"]))

    log("claude-tuner 诊断（把下面整段一起复制，别只贴最后一行）")
    log("-" * 60)
    for title, value in rows:
        log("%s：%s" % (title, value))
    log("-" * 60)
    log("以上的 Key 一律不打印（只报「填了没」）。遇到问题请附上这段：")
    log("https://github.com/ls-open/claude-tuner/issues")
    return 0


# ==============================================================================
# CLI
# ==============================================================================

def cmd_install(args):
    paths = config_ops.platform_paths()
    api_key = args.key or os.environ.get("CLAUDE_TUNER_API_KEY", "")
    state = gather_state(paths)
    actions, notes = plan(state, api_key=api_key, upstream=args.upstream,
                          model=args.model, preset=args.preset,
                          with_cli=not args.no_cli, force=args.force)
    log("📋 安装计划：")
    for line in notes:
        log("  " + line)
    if not actions:
        return 0
    key_source = "命令行/环境变量" if api_key else "未提供"
    log("  将写入 %d 处（API Key：%s）" % (len(actions), key_source))
    done, backups, problems = apply(actions, dry_run=args.dry_run)
    if args.dry_run:
        log("🔍 演练完成：%d 个动作**未执行**（--dry-run 不写任何文件）" % done)
        for act in actions:
            log("    · %s → %s" % (act["reason"], act["path"]))
        return 0
    log("✅ 已写入 %d 处" % done)
    for b in backups:
        log("   ↩︎ 原文件已备份：%s" % b)
    for p in problems:
        # 用专用字样：`⚠️ ` 这个前缀在安装输出里也被「还没有 API Key」这类提醒用着，
        # 混在一起就没人分得清哪条是真失败（含测试与 CI）。
        log("   ❌ 写入失败：%s" % p)
    # 机器相关的适配要在「文件落地之后」再跑一遍：hooks.json 里那句解释器名取决于这台
    # 机器上叫什么（Windows 上通常是 python / py，没有 python3）。不在这里对齐，
    # 客户端就永远拉不起网关，而且没有任何报错 —— 这正是它必须在安装路径上的理由。
    aligned = align_runtime()
    if aligned.get("changed"):
        log("   🔧 hooks 的解释器已对齐到本机：%s" % aligned.get("interpreter"))
    if args.no_restart:
        log("   已跳过网关重启（--no-restart），由调用方负责拉起")
    else:
        restart_gateway()
        log("✅ 本地网关已就绪（http://127.0.0.1:%d）" % state["port"])
    return 0


def cmd_uninstall(args):
    """卸载：只收回本插件写的东西。

    3P 配置**默认不动** —— 它是客户端的资产，我们只是往里填了字段。
    真要连它一起清掉得显式给 --purge-3p。
    """
    paths = config_ops.platform_paths()
    state = gather_state(paths)
    merged, changed = config_ops.merge_settings(
        state["claude_settings"], {},
        unset_env=config_ops.SETTINGS_ENV_KEYS, unset_top=("promptCacheTtl",
                                                            "subagentPromptCacheTtl",
                                                            "autoCompactEnabled"))
    # 客户端侧拦截写在 3P 配置的 disabledBuiltinTools 里 —— 卸 载时只删自己写过的那些，
    # 用户自己在 Setup 窗口加的条目原样留着。
    three_p_bundle, disabled_changed, _ = config_ops.merge_client_disabled(
        state["three_p"], [], (state["bridge_cfg"] or {}).get(config_ops.CLIENT_DISABLED_KEY) or [])
    if disabled_changed and state["three_p_path"]:
        log("  客户端侧工具拦截将收回：%s" % ", ".join(disabled_changed))
        if args.dry_run:
            log("  [dry-run] 未写入 %s" % state["three_p_path"])
        else:
            _backup(state["three_p_path"], time.strftime("%Y-%m-%dT%H:%M:%S%z"), False)
            atomic_write_json(state["three_p_path"], three_p_bundle)
    log("🧹 卸载：")
    if changed:
        log("  客户端设置将清除：%s" % ", ".join(changed))
        if args.dry_run:
            log("  [dry-run] 未写入 %s" % paths["claude_settings"])
        else:
            _backup(paths["claude_settings"], time.strftime("%Y-%m-%dT%H:%M:%S%z"), False)
            atomic_write_json(paths["claude_settings"], merged)
    else:
        log("  客户端设置里没有本插件写的键，跳过")
    if args.purge_3p and state["three_p_path"]:
        log("  --purge-3p：删除 %s" % state["three_p_path"])
        if not args.dry_run:
            _backup(state["three_p_path"], time.strftime("%Y-%m-%dT%H:%M:%S%z"), False)
            os.remove(state["three_p_path"])
    else:
        log("  3P 配置保持不变（要一起清掉请加 --purge-3p）")
    log("  备份都在 %s" % os.path.join(paths["bridge_home"], "backups", "3p"))
    if not args.dry_run:
        subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "manager.py"), "stop"],
                       timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("  本地网关已停止")
    return 0


# ── 机器相关的适配（每次「文件落地」之后都要重跑一遍） ──────────────────────────
# hooks.json 是随插件发布的清单，但里面那句 `python3 ...` 要看**这台机器**上叫什么。
# 早先把改写写在 install.py 里，只在安装那一刻生效 —— 下次点「检查更新」整棵树被覆盖，
# 适配就没了，Hook 静默失效、网关再也拉不起来。
# 所以适配必须做成**幂等的收尾步骤**，由所有「往插件目录写文件」的入口调用：
# bootstrap install（首次安装，install.py 转交给它）、updater.py（检查更新）、
# tools/sync.py（开发同步）。
PYTHON_CANDIDATES = ("python3", "python", "py")


def _pick_interpreter():
    """hooks 该用哪个解释器启动本插件 —— 取这台机器上真的能跑起来的那个名字。

    优先 `python3`（POSIX 惯例），其次 `python`、`py`（Windows 上装的是这两个）。
    都找不到就什么都不改，让客户端自己的报错暴露问题，比我们写一个猜的名字强。
    """
    import shutil as _shutil
    for name in PYTHON_CANDIDATES:
        if _shutil.which(name):
            return name
    return None


def align_runtime(dest=None, dry_run=False):
    """把 hooks.json 里的解释器名对齐到本机。幂等：已经对就一个字节都不写。"""
    dest = dest or os.path.dirname(os.path.abspath(__file__))
    # 路径可以被环境变量顶掉：测试要在一个临时副本上验「装完对齐了没有」，
    # 不改写仓库里的那份（测试不该弄脏源码树——本机恰好不需要改写时看不出来，
    # 换到没有 python3 的 Windows 上就会真的改到仓库文件）。
    hooks_file = os.environ.get("CLAUDE_TUNER_HOOKS_FILE") or os.path.join(
        os.path.dirname(dest), "hooks", "hooks.json")
    if not os.path.isfile(hooks_file):
        return {"changed": False, "reason": "no-hooks-file"}
    interpreter = _pick_interpreter()
    if not interpreter:
        return {"changed": False, "reason": "no-interpreter"}
    try:
        with open(hooks_file, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        return {"changed": False, "reason": "read-error: %s" % exc}
    fixed = text
    for name in PYTHON_CANDIDATES:
        if name == interpreter:
            continue
        fixed = fixed.replace('"%s ' % name, '"%s ' % interpreter)
        fixed = fixed.replace('"%s"' % name, '"%s"' % interpreter)
    if fixed == text:
        return {"changed": False, "reason": "already-aligned", "interpreter": interpreter}
    if dry_run:
        return {"changed": True, "dry_run": True, "interpreter": interpreter}
    try:
        with open(hooks_file, "w", encoding="utf-8") as fh:
            fh.write(fixed)
    except OSError as exc:
        return {"changed": False, "reason": "write-error: %s" % exc}
    return {"changed": True, "interpreter": interpreter}


def build_parser():
    p = argparse.ArgumentParser(description="claude-tuner 安装协调者")
    sub = p.add_subparsers(dest="cmd")

    ins = sub.add_parser("install", help="写好客户端状态并拉起网关")
    ins.add_argument("--dry-run", action="store_true", help="只打印计划，不写任何文件")
    ins.add_argument("--force", action="store_true", help="已装过也强制重写")
    ins.add_argument("--key", default="", help="API Key（也可用环境变量 CLAUDE_TUNER_API_KEY）")
    ins.add_argument("--upstream", default="", help="上游 base URL，默认 DeepSeek 官方")
    ins.add_argument("--preset", default="", help="上游预设 id（deepseek/opencode/custom）")
    ins.add_argument("--model", default="", help="上游模型名（identity 预设不使用）")
    ins.add_argument("--no-cli", action="store_true", help="不写 ~/.claude/settings.json 的 CLI 联动")
    ins.add_argument("--no-restart", action="store_true",
                     help="不重启网关（安装器 install.py 之后会自己重启并核对端口）")

    st = sub.add_parser("status", help="体检")
    st.add_argument("--json", action="store_true", dest="as_json")

    sub.add_parser("doctor", help="打印一段可直接粘进 issue 的诊断信息（不含密钥）")

    al = sub.add_parser("align", help="把机器相关的适配重新对齐（hooks 的解释器名等，幂等）")
    al.add_argument("--dry-run", action="store_true")

    un = sub.add_parser("uninstall", help="收回本插件写的客户端状态")
    un.add_argument("--dry-run", action="store_true")
    un.add_argument("--purge-3p", action="store_true", help="连 3P 推理配置一起删（危险）")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "status"
    if cmd == "install":
        return cmd_install(args)
    if cmd == "uninstall":
        return cmd_uninstall(args)
    if cmd == "align":
        result = align_runtime(dry_run=getattr(args, "dry_run", False))
        log("适配：%s" % json.dumps(result, ensure_ascii=False))
        return 0
    if cmd == "doctor":
        return doctor()
    info = status()
    if getattr(args, "as_json", False):
        print(json.dumps(info, ensure_ascii=False, indent=1))
    else:
        log("装好了吗：%s" % ("是" if info["installed"] else "否"))
        log("插件目录：%s（%s）" % (info["plugin_dir"],
                                   "在" if info["plugin_present"] else "缺失"))
        log("3P 配置：%s" % (info["three_p_config"] or "无"))
        log("API Key：%s" % ("已填" if info["api_key_set"] else "未填"))
        log("上游：%s" % (info["upstream_url"] or "未设置"))
        log("网关端口：%d" % info["gateway_port"])
        for p in info["three_p_problems"]:
            log("  ⚠️ %s" % p)
    return 0


if __name__ == "__main__":
    configure_stdio()
    sys.exit(main())
