"""共享工具函数：gateway.py / manager.py 共用。

本文件包含通用规则配置脚手架、配置初始化与辅助函数。
模型映射已完全由 Claude 3P 配置 (configLibrary) 管理，单一真理源。
"""
import json
import os
import re


def get_3p_config_dir():
    """获取 Claude 3P configLibrary 目录路径（全仓唯一入口，仅 macOS）。"""
    custom = os.environ.get("CLAUDE_TUNER_3P_DIR")
    if custom:
        return os.path.expanduser(custom)
    glob_pattern = os.environ.get("CLAUDE_TUNER_3P_CONFIG_GLOB")
    if glob_pattern:
        return os.path.dirname(os.path.expanduser(glob_pattern))
    return os.path.expanduser("~/Library/Application Support/Claude-3p/configLibrary")


# 3P 配置扫描 Glob 路径：由 get_3p_config_dir() 派生而非各写一份默认值，
# 否则 CLAUDE_TUNER_3P_DIR / 非 macOS 平台下会出现「网关读 A 目录、同步改 B 目录」的静默错位。
THREEP_CONFIG_GLOB = os.path.join(get_3p_config_dir(), "*.json")


_meta_cache = (None, 0.0, "")  # (meta_path, mtime, applied_id)


def _read_applied_id(meta_path):
    """读取 _meta.json 的 appliedId，按 mtime 缓存解析结果。

    find_active_3p_config_path 在网关热路径上每请求调用一次，而 _meta.json 极少变；
    缓存后稳态下每请求只剩一次 stat，不再每次解析 JSON。
    """
    global _meta_cache
    try:
        mtime = os.path.getmtime(meta_path)
    except OSError:
        return ""
    cached_path, cached_mtime, cached_id = _meta_cache
    if cached_path == meta_path and cached_mtime == mtime:
        return cached_id
    applied_id = ""
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        if isinstance(meta, dict):
            applied_id = (meta.get("appliedId") or "").strip()
    except Exception:
        applied_id = ""
    _meta_cache = (meta_path, mtime, applied_id)
    return applied_id


def find_active_3p_config_path():
    """定位当前生效的 3P 配置文件绝对路径。

    1. 优先读取 configLibrary/_meta.json 得到 appliedId，找到对应的 <appliedId>.json；
    2. 若 _meta.json 缺失或无效，则扫描目录下除 _meta.json 外最新修改的 .json 文件。
    """
    cfg_dir = get_3p_config_dir()
    if not os.path.isdir(cfg_dir):
        return None

    meta_path = os.path.join(cfg_dir, "_meta.json")
    if os.path.isfile(meta_path):
        applied_id = _read_applied_id(meta_path)
        if applied_id:
            applied_path = os.path.join(cfg_dir, f"{applied_id}.json")
            if os.path.isfile(applied_path):
                return applied_path

    candidates = []
    try:
        for name in os.listdir(cfg_dir):
            if name.endswith(".json") and not name.startswith("_meta"):
                full = os.path.join(cfg_dir, name)
                if os.path.isfile(full):
                    try:
                        candidates.append((os.path.getmtime(full), full))
                    except OSError:
                        pass
    except OSError:
        pass

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def read_3p_models():
    """从当前生效的 3P 配置文件中解析模型映射字典。

    返回值形如: {"claude-sonnet-5": "space-bunny-free", "claude-opus-5": "deepseek-flash"}
    规则:
      - 客户端代号 (item.name) 转小写作为 key；
      - 若 labelOverride 非空，则其修剪后的字符串作为目标模型；
      - 若 labelOverride 为空，则目标模型为 item.name 本身。
    若未找到 3P 配置或配置中无模型列表，返回空 dict。
    """
    active_path = find_active_3p_config_path()
    if not active_path:
        return {}
    try:
        with open(active_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        models_list = data.get("inferenceModels")
        if not isinstance(models_list, list):
            return {}
        mapping = {}
        for item in models_list:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            if not name:
                continue
            label = (item.get("labelOverride") or "").strip()
            target = label if label else name
            mapping[name.lower()] = target
        return mapping
    except Exception:
        return {}


def atomic_write_json(path, data, indent=2):
    """原子写 JSON：同目录临时文件 + os.replace，并保留目标文件原有权限位。

    3P 配置文件里有 inferenceGatewayApiKey 且默认 0600，若直接 open()+replace，
    临时文件会按 umask 落成 0644，替换后就把密钥暴露给同机其他用户。
    """
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o600
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent)
        os.chmod(tmp, mode)  # 已存在的 .tmp 会保留旧权限，显式对齐
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# 默认清洗与防抖规则配置（全部可在 ~/.claude-tuner/config.json 的 "rules" 节点动态配置与热加载）
DEFAULT_RULES = {
    "probe_interception": True,          # 测长探针本地秒回 (max_tokens=1 拦截)
    "strip_billing_header": True,        # 剥离 x-anthropic-billing-header 计费头
    "strip_model_declarations": True,    # 剥离 - You are powered by the model named... 等声明行
    "strip_date": True,                  # 剥离 Today's date is... 动态跨天日期
    "strip_git_status": True,            # 剥离 gitStatus 仓库初始快照
    "split_mixed_user_messages": True,   # 拆分混合 user 消息 (text + tool_result，防 400)
    "restore_thinking": True,            # 智能补齐历史 Thinking 思考链
    "tool_block_ordering": True,         # 并发工具调用的 tool_use / tool_result 块按 id 稳定排序
    "strip_security_guidelines": False,  # 剥离安全攻防与拒绝服务限制指令 (省 ~110 Tokens，防本地运维脚本误拒答)
    "strip_pronoun_guidelines": False,   # 剥离性别代词道德指引指令 (省 ~85 Tokens，专注于纯粹代码工程)
    "strip_caution_guidelines": False,   # 剥离高危操作与外发确认防御指引 (省 ~120 Tokens，减少模型过度犹豫)
    "strip_total_tokens_tag": True,      # 剥离 <total_tokens> 计数行（数字每轮都在变，会击穿前缀缓存）
}


# 默认工具精简配置（模块化可插拔）。此处刻意不复述体积数字：
# 客户端一升级就会变，写进代码必然漂移 —— 页面上按真实请求现称。
#
# **这份默认值 = 界面上的「日常开发均衡档」**（新装用户开箱即用的那一档）。
# 改这里必须同步改 `webui/index.html` 的 `PRESETS.balanced.tool_pruning`，反之亦然 ——
# 两处本来就是同一份「推荐基线」，分头改必然漂移。其余档位（纯代码极限 / 只换模型）是
# 在它基础上的加减，不必对齐。
DEFAULT_TOOL_PRUNING_CONFIG = {
    "enabled": True,                      # 工具精简总开关
    # 联网：搜索与抓取是两个独立工具，分开 —— 有人要抓取文档但不想要搜索。
    # 默认**保留**：它俩是服务端工具，上游实现了才有用，纯代码/离线场景用户可自己关。
    "strip_web_search_tools": False,      # WebSearch
    "strip_web_fetch_tools": False,       # WebFetch
    # 桌面端界面遥控：按**功能**拆成六项。原来全在 strip_session_mgmt_tools 一个键下，
    # 实测管着 52 个工具 / 61 KB —— 想清掉侧栏那 9 KB，就得连会话管理那 32 KB 一起清掉，
    # 而两者毫无关系。**不含 ccd_view**：那是呈现工具，客户端提示词要求模型用它。
    # 会话管理里性质不同的六类单独成项（都默认开，与原来一个键全剥时行为一致）：
    # 它们不是界面遥控，而是改权限 / 改模型档位 / 导出数据 / 删会话 / 跨会话发消息 / 起停后台任务。
    "strip_session_tools": True,          # mcp__ccd_session* / mcp__session_info*（纯界面遥控那部分）
    "strip_session_permission_tools": True,   # 改会话权限模式
    "strip_session_model_tools": True,        # 改会话模型 / 思考档位 / fast 模式
    "strip_session_export_tools": True,       # 导出会话、检索历史、列会话
    "strip_session_delete_tools": True,       # 删除 / 清空 / 归档 / 取消归档会话
    "strip_session_remote_tools": True,       # 向其他会话发消息、开远程控制
    "strip_session_task_tools": True,         # 起 / 停会话内的后台任务
    "strip_sidebar_tools": True,          # mcp__ccd_sidebar*
    "strip_window_tools": True,           # mcp__ccd_window*
    "strip_view_tools": False,            # mcp__ccd_view*（代码修改推送与预览面板，默认保留）
    "strip_host_tools": False,            # mcp__ccd_host*（编辑器、存储、保持唤醒）
    "strip_settings_tools": True,         # mcp__ccd_settings*
    "strip_directory_tools": True,        # mcp__ccd_directory*
    "strip_pr_ci_tools": True,            # 剥离 GitHub PR 关联与 CI 监控自动化 (mcp__ccd_pr__*)
    "strip_terminal_tools": False,        # 桌面右侧终端面板读取工具 (mcp__terminal__*)
    "strip_notebook_tools": True,         # 剥离 Jupyter Notebook 单元格编辑工具 (NotebookEdit)
    "strip_design_sync_tools": True,      # 剥离 DesignSync (3P模式下无法官方认证)
    "strip_plugin_mgmt_tools": True,      # 剥离插件市场管理与技能建议死重 (mcp__plugins__*, mcp__skills__*)
    "custom_strip_tools": [],             # 自定义额外剥离工具名列表
    # 以下几项默认**关**：删掉的是真本事（跨会话协作 / 工作树隔离 / 计划模式），
    # 不该由我们替用户决定。页面上把各自的体积标出来，让用户自己掂量。
    # 按真实耦合分组：独立的独立，配对的成对（见 tool_rules.CATEGORY_NAMES 的注释）
    "strip_cross_session_tools": False,   # SendMessage / ListAgents
    "strip_worktree_tools": False,        # EnterWorktree / ExitWorktree
    "strip_plan_tools": False,            # EnterPlanMode / ExitPlanMode
    # 这两个是「多代理编排与后台任务」那个大开关拆开时掉出来的 —— 一路没人接住，
    # 于是长期没有开关（哨兵看不出来，因为它当时只挑 mcp__ 前缀报）。同样是真本事，
    # 默认保留：TaskStop 用来终止跑飞的后台任务，ReportFindings 是 code-review 那套
    # 技能把结论交回界面的通道。
    "strip_task_stop_tools": False,       # TaskStop
    "strip_report_findings_tools": False, # ReportFindings
}


# 已废弃的配置键，由 ensure_config_initialized 自动从旧 config.json 中清理
RETIRED_CONFIG_KEYS = (
    "passthrough",                # 曾用于整体跳过稳定化，已被细粒度的 rules.* 取代
    "cache_stabilize",            # 同上
    "intercept_suggestion_mode",  # 网关侧输入预测拦截已移除（客户端自带的开关更好使）
)

# 已废弃的 tool_pruning 子键（同上，但它们在 tool_pruning 节点下，不是顶级键）
RETIRED_TOOL_PRUNING_KEYS = (
    "strip_cli_cron_tools",        # Cron 定时任务改由 3P 的 Allow scheduled tasks 在源头关闭
    "strip_scheduled_tasks_tools", # 同上（两套机制一起被客户端开关移除）
    "strip_workflow_tools",        # 动态工作流改由客户端的 enableWorkflows 在源头关闭
)

# 改过名的 tool_pruning 子键：旧名 → 新名（一个旧键可能拆成几个新键）。
#
# **改名时必须把旧值带过去，不能只删旧键。** 新键的默认值是 True，如果用户原来把
# 旧键关掉过（= 想留着那批工具），直接删旧键 + 补新键会让它们被静默剥掉 ——
# 他什么都没做，工具却没了。这类「升级后行为变了且没人知道」的问题最难排查。
RENAMED_TOOL_PRUNING_KEYS = {
    "strip_native_web_tools": ("strip_web_search_tools", "strip_web_fetch_tools"),
    "strip_session_mgmt_tools": ("strip_session_tools", "strip_sidebar_tools",
                                 "strip_window_tools", "strip_host_tools",
                                 "strip_settings_tools", "strip_directory_tools"),
    # Workflow 原本在这串里，已改由客户端的 enableWorkflows 管（不再有网关开关），
    # 所以迁移只带剩下的三个；旧配置里的 strip_workflow_tools 由 RETIRED_TOOL_PRUNING_KEYS 清掉。
    "strip_orchestration_tools": ("strip_cross_session_tools",
                                  "strip_task_stop_tools", "strip_report_findings_tools"),
}


# 默认配置文件完整脚手架模板
DEFAULT_CONFIG = {
    "upstream_url": "",
    "gateway_port": 8789,
    "upstream_timeout": 45,
    # 客户端代号漏配时的兜底上游模型（空 = 关闭，原样透传）。
    # 客户端模型选择器由 3P 的 inferenceModels 决定，用户换了代号而映射表没跟上时，
    # 原样透传会让上游以 404 打回 —— 那是首次安装最像「装坏了」的失败。
    "default_model": "",
    "unknown_model_fallback": True,
    # 「1M 上下文」：写进 3P 的 inferenceModels（supports1m / prefer1m）与顶层的
    # modelPrefer1mContext，并决定 CLI 的 ANTHROPIC_MODEL 带不带 [1m] 后缀。
    # 它影响的是**客户端**怎么算上下文窗口（能长到多大、多久压缩一次）；
    # 上游吃不吃得下 1M 是另一回事 —— 所以做成开关，上游不支持时能关掉。
    "one_m_context": True,
    # 「允许定时任务」：写进 3P 的 scheduledTasksEnabled。默认关 —— 定时任务整套工具
    # 会从源头消失（客户端干脆不提供创建/修改/运行它们的工具），比事后剥离干净。
    "scheduled_tasks": False,
    "rules": DEFAULT_RULES,
    "tool_pruning": DEFAULT_TOOL_PRUNING_CONFIG,
}
# 注：此处刻意不声明任何第三方抓取服务的 Key —— 那类凭据归它自己的工具管，
# 跟网关配置无关。网关只管「客户端 → 上游模型」这一条链路。


def ensure_config_initialized(config_path=None):
    """自动初始化或补全 ~/.claude-tuner/config.json 配置文件。

    1. 若文件不存在：自动生成包含 rules / tool_pruning 的脚手架配置；
    2. 若文件已存在：安全补充缺失的顶级节点与子配置项，确保全新安装或升级后开箱即用；
    3. 顺手清掉 RETIRED_CONFIG_KEYS 里的废弃键（只会删登记过的，不碰其余任何键）。
    """
    base_dir = os.environ.get("CLAUDE_TUNER_HOME", os.path.expanduser("~/.claude-tuner"))
    path = config_path or os.path.join(base_dir, "config.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if not os.path.exists(path):
            atomic_write_json(path, DEFAULT_CONFIG)
            return path

        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        if not isinstance(cfg, dict):
            cfg = {}

        updated = False
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
                updated = True

        # 改名的键：把旧值带到新键上（见 RENAMED_TOOL_PRUNING_KEYS 的注释）
        if isinstance(cfg.get("tool_pruning"), dict):
            tp = cfg["tool_pruning"]
            for old, news in RENAMED_TOOL_PRUNING_KEYS.items():
                if old not in tp:
                    continue
                old_value = tp.pop(old)
                for n in news:
                    if n not in tp:        # 新键已经在了？以新键为准，不覆盖用户的明确设置
                        tp[n] = old_value
                updated = True

        # 安全补充 tool_pruning 缺失的子配置项
        if "tool_pruning" in cfg and isinstance(cfg["tool_pruning"], dict):
            for tk, tv in DEFAULT_TOOL_PRUNING_CONFIG.items():
                if tk not in cfg["tool_pruning"]:
                    cfg["tool_pruning"][tk] = tv
                    updated = True

        # 清掉已废弃的键（登记在 RETIRED_CONFIG_KEYS）
        for rk in RETIRED_CONFIG_KEYS:
            if rk in cfg:
                cfg.pop(rk)
                updated = True

        # 同上，但作用于 tool_pruning 下的子键
        if isinstance(cfg.get("tool_pruning"), dict):
            for rk in RETIRED_TOOL_PRUNING_KEYS:
                if rk in cfg["tool_pruning"]:
                    cfg["tool_pruning"].pop(rk)
                    updated = True

        # 安全补充 rules 缺失的子配置项
        if "rules" in cfg and isinstance(cfg["rules"], dict):
            for rk, rv in DEFAULT_RULES.items():
                if rk not in cfg["rules"]:
                    cfg["rules"][rk] = rv
                    updated = True

        if updated:
            atomic_write_json(path, cfg)
    except Exception:
        pass
    return path


# 上游客户端标识（本插件唯一一处定义，gateway / manager 共用）：
# - 对所有上游恒定显式发送自有 User-Agent（避免漏出 Python-urllib 默认指纹触发 Cloudflare 1010 拦截）；
# - 仅对 OpenCode 上游附带 x-opencode-session 以协助路由与提示词缓存。
# - 版本号代表上游可见的客户端身份（与清单文件无关），改动可能触发路由/缓存分桶重置，仅在需变更身份时手动提升。
CLIENT_UA = "claude-tuner/0.2.0"


def is_opencode_upstream(url):
    """判断上游是否为 OpenCode 系网关（需要附带其会话标识头）。"""
    return "opencode.ai" in (url or "").lower()


def upstream_client_headers(session_id=None, opencode=False):
    """构造发往上游的客户端标识头。

    :param session_id: 会话 ID，用于 OpenCode 的路由与提示词缓存优化
    :param opencode:   上游是否为 OpenCode 系（决定是否附带 x-opencode-session）
    """
    headers = {"User-Agent": CLIENT_UA}
    if opencode:
        headers["x-opencode-session"] = "claude-%s" % (session_id or "default")
    return headers


def client_api_key(headers):
    """从请求头提取客户端 API Key（支持 x-api-key 与 Authorization Bearer）。"""
    if hasattr(headers, "get"):
        x = headers.get("x-api-key") or headers.get("X-Api-Key")
        if x:
            return x.strip()
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
    return ""


def extract_session_id(parsed):
    """从 metadata 中提取 session_id（兼容 session_id 字段与 user_id 嵌套 JSON）。"""
    try:
        meta = parsed.get("metadata") or {}
        if not isinstance(meta, dict):
            return ""
        if meta.get("session_id"):
            return str(meta["session_id"]).strip()
        uid = meta.get("user_id") or ""
        if isinstance(uid, str) and uid.strip().startswith("{"):
            return json.loads(uid).get("session_id") or ""
    except Exception:
        pass
    return ""


def estimate_tokens(obj):
    """快速估算消息对象的 token 数量。"""
    def walk(x):
        if isinstance(x, str):
            if len(x) > 500 and (x.startswith("/9j/") or x.startswith("iVBORw0KGgo") or re.match(r"^data:image\/[a-zA-Z0-9.+_-]+;base64,", x[:60])):
                return 1600
            cn_count = sum(1 for c in x if "一" <= c <= "鿿")
            other_count = len(x) - cn_count
            return int(cn_count * 0.95 + other_count / 3.4)
        if isinstance(x, list):
            return sum(walk(i) for i in x)
        if isinstance(x, dict):
            if x.get("type") in ("image", "image_url") or "image/" in str(x.get("media_type", "")):
                return 1600
            return sum(walk(v) for v in x.values())
        return 0
    return max(1, walk(obj))


def canonical_local_url(url):
    """规范化本地网关 URL：统一 localhost ➔ 127.0.0.1 并剥离空白与尾斜杠（完全幂等）。"""
    url = (url or "").strip().rstrip("/")
    return re.sub(r"^http://localhost(?=[:/]|$)", "http://127.0.0.1", url, flags=re.IGNORECASE)


def normalize_upstream(url):
    """3P 填的是 base URL，本地网关转发需要完整端点；缺 /v1/messages 后缀时自动补全。"""
    url = (url or "").strip().rstrip("/")
    if not url or url.endswith("/v1/messages"):
        return url
    if url.endswith("/v1"):
        return url + "/messages"
    return url + "/v1/messages"


# ==============================================================================
# 版本与来源（release.json 是唯一真理源，install.sh 与 Python 都读它）
# ==============================================================================

_plugin_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE_FILE = os.path.join(_plugin_root, "release.json")
CLIENT_APP_PLIST = "/Applications/Claude.app/Contents/Info.plist"

_release_cache = None


def release_info():
    """读 release.json：从哪个仓库来（repo / mirror）、本版号、按哪个客户端版本验过。"""
    global _release_cache
    if _release_cache is not None:
        return _release_cache
    info = {"repo": "", "mirror": "", "version": "", "tested_client": ""}
    try:
        with open(RELEASE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            info.update({k: str(data.get(k) or "").strip() for k in info})
    except Exception:
        pass
    _release_cache = info
    return info


def plugin_version():
    """本插件的版本号。刻意不写进 plugin.json —— 那份清单是给客户端读的，
    版本号在里面不起开关作用（见 DECISIONS #9）；这里用它只为「装的是哪一版」。"""
    return release_info()["version"]


def client_version():
    """Claude 桌面端的版本号（读它的 Info.plist）。读不到返回空串，不抛异常。"""
    try:
        import subprocess
        res = subprocess.run(["/usr/libexec/PlistBuddy", "-c",
                              "Print :CFBundleShortVersionString", CLIENT_APP_PLIST],
                             capture_output=True, text=True, timeout=5)
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:
        return ""


def version_tuple(text):
    """把版本号切成可比较的元组：'2.31226.0' → (2, 31226, 0)。
    非数字段按 0 处理，读不出任何数字时返回 ()。"""
    parts = []
    for chunk in re.split(r"[._-]", (text or "").strip()):
        digits = re.match(r"\d+", chunk)
        parts.append(int(digits.group()) if digits else 0)
    return tuple(parts) if any(parts) else ()
