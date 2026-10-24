"""配置台与安装脚本共用的纯函数层：无 IO、无 HTTP、无副作用。

把「改用户文件」这件事里所有需要判断的部分（合并、校验、幂等、门禁）都收在这里，
让测试能以生产同一形态调用它。真正的落盘由 bootstrap.py / webui.py 做。

平台路径只在这里求值一次（platform_paths），补 Windows 时只改那一处。
"""
import copy
import os
import re
import uuid


# ==============================================================================
# 平台路径
# ==============================================================================

def platform_paths():
    """返回本机各处配置文件的路径（只做路径拼接，不判断存在性）。

    环境变量覆盖：HOME / CLAUDE_TUNER_HOME / CLAUDE_TUNER_3P_DIR。测试靠它们指到临时目录。
    """
    home = os.path.expanduser("~")
    app_support = os.path.join(home, "Library", "Application Support")
    claude_dir = os.path.join(app_support, "Claude")
    claude_3p_dir = os.path.join(app_support, "Claude-3p")
    return {
        "home": home,
        "claude_dir": claude_dir,
        "claude_3p_dir": claude_3p_dir,
        # Enable Developer Mode 写的那份（1P 目录）
        "developer_settings_1p": os.path.join(claude_dir, "developer_settings.json"),
        # 3P 模式下 userData 会切到 Claude-3p，这份是「已经在 3P」的记录
        "desktop_config_1p": os.path.join(claude_dir, "claude_desktop_config.json"),
        "desktop_config_3p": os.path.join(claude_3p_dir, "claude_desktop_config.json"),
        "developer_settings_3p": os.path.join(claude_3p_dir, "developer_settings.json"),
        "config_library": os.environ.get("CLAUDE_TUNER_3P_DIR")
        or os.path.join(claude_3p_dir, "configLibrary"),
        "bridge_home": os.environ.get("CLAUDE_TUNER_HOME")
        or os.path.join(home, ".claude-tuner"),
        "claude_settings": os.path.join(home, ".claude", "settings.json"),
        "plugin_dir": os.path.join(home, ".claude", "skills", "claude-tuner"),
    }


def backup_path(target, ts):
    """备份路径：同目录不行（会被客户端当配置扫到），统一落 ~/.claude-tuner/backups/3p/。"""
    stamp = re.sub(r"[^0-9A-Za-z]", "", ts)
    return os.path.join(platform_paths()["bridge_home"], "backups", "3p",
                        "%s.%s.bak" % (os.path.basename(target), stamp))


# ==============================================================================
# 密钥脱敏与门禁判据
# ==============================================================================

MASK_SENTINEL = "__unchanged__"


def mask_api_key(key):
    """脱敏：读接口永不返回完整 Key。长度不足 8 的一律全遮。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) < 8:
        return "****"
    return key[:4] + "****" + key[-4:]


def key_unchanged(submitted, current):
    """前端回传脱敏串 / 空 / 哨兵值都表示「保留原 Key」。"""
    submitted = (submitted or "").strip()
    if not submitted or submitted == MASK_SENTINEL:
        return True
    return submitted == mask_api_key(current)


def constant_time_eq(a, b):
    """定长比较，避免 token 校验被计时侧信道区分。"""
    import hmac
    return hmac.compare_digest((a or "").encode("utf-8"), (b or "").encode("utf-8"))


def _local_host_names():
    return ("127.0.0.1", "localhost", "[::1]")


def host_ok(host_header, port):
    """Host 头必须是本机回环 + 本网关端口 —— 防 DNS rebinding（恶意域名解析到 127.0.0.1）。"""
    host = (host_header or "").strip().lower()
    if not host:
        return False
    name, _, hport = host.rpartition(":")
    if not name or not hport.isdigit():
        name, hport = host, ""
    if name not in _local_host_names():
        return False
    return hport in ("", str(port))


def origin_ok(origin, port):
    """带 Origin 的写请求必须同源；不带 Origin（curl 等本机调用）交由 token 把关。"""
    origin = (origin or "").strip().lower()
    if not origin:
        return True
    if origin in ("null", "file://"):
        return False
    m = re.match(r"^https?://([^/]+)$", origin)
    return bool(m) and host_ok(m.group(1), port)


# ==============================================================================
# 网关侧配置（~/.claude-tuner/config.json）
# ==============================================================================

def _merge_subkeys(container, defaults, patch):
    """只接受 defaults 里登记过的子键，未知键丢弃（防前端塞进任意键）。"""
    changed = []
    out = copy.deepcopy(container) if isinstance(container, dict) else {}
    for k, v in (patch or {}).items():
        if k not in defaults:
            continue
        if isinstance(defaults[k], bool):
            v = bool(v)
        if out.get(k) != v:
            out[k] = v
            changed.append(k)
    return out, changed


def apply_rules_patch(cfg, patch):
    """把补丁合进 cfg["rules"]，返回 (新配置, 实际变动的键)。"""
    from common import DEFAULT_RULES
    new = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    merged, changed = _merge_subkeys(new.get("rules"), DEFAULT_RULES, patch)
    new["rules"] = merged
    return new, changed


def apply_pruning_patch(cfg, patch):
    """把补丁合进 cfg["tool_pruning"]；未知键丢弃，列表型键做整体替换。

    custom_strip_tools 额外过一遍 sanitize_custom_list：它会被当成工具名黑名单，
    且会落盘，前端送什么进来都得先收敛（去重、限量、限长）。
    """
    from common import DEFAULT_TOOL_PRUNING_CONFIG
    from tool_rules import sanitize_custom_list
    patch = dict(patch or {})
    if "custom_strip_tools" in patch:
        patch["custom_strip_tools"] = sanitize_custom_list(patch["custom_strip_tools"])
    new = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
    merged, changed = _merge_subkeys(new.get("tool_pruning"), DEFAULT_TOOL_PRUNING_CONFIG, patch)
    new["tool_pruning"] = merged
    return new, changed


# ==============================================================================
# 客户端行为开关（~/.claude/settings.json）
# ==============================================================================

# 只允许本插件写这些键；其余键（含用户自有的）一律原样保留。
SETTINGS_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
)

SETTINGS_TOP_KEYS = (
    "promptCacheTtl",
    "subagentPromptCacheTtl",
    "promptSuggestionEnabled",
    "autoCompactEnabled",
    "autoMemoryEnabled",
    "includeCoAuthoredBy",
    "attribution",
    # 更激进的省法：会改变客户端行为，默认不动，页面里单独一组
    "idleCompaction",              # 会话空闲时自动压缩长对话
    "precomputeCompactionEnabled", # 后台预生成压缩摘要（只在自动压缩开着时有意义）
    "showThinkingSummaries",       # 请服务端额外生成一份思考摘要
    "enableWorkflows",             # 动态工作流（Workflow 工具）总开关
    "alwaysThinkingEnabled",       # 支持思考的模型是否自动思考
)
# 注：`disableBundledSkills` **不在这里** —— 它是 3P 配置的键（客户端设置页也写那儿），
# 归 `build_3p_bundle` 的 `bundled_skills` 管。写到这个清单里等于永不生效。


def cli_env(base_url, api_key, model, one_m=True):
    """CLI 联动环境变量：照抄 DeepSeek 官方 Claude Code 接入文档，只换 base_url。

    官方要用户手工 export 这一整套；这里由安装器一次写好，用户一个都不用碰。
    """
    # [1m] 是**客户端自己的**上下文标记，网关转发前会剥掉（见 gateway.map_model）。
    # 上游看到的始终是干净模型名，所以加不加它只影响客户端怎么算窗口。
    model_1m = (model + "[1m]") if one_m else model
    return {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": api_key,
        "ANTHROPIC_MODEL": model_1m,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model_1m,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model_1m,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        "CLAUDE_CODE_SUBAGENT_MODEL": model,
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "786432",
    }


def merge_settings(existing, patch, unset_env=(), unset_top=()):
    """合并客户端设置，返回 (新配置, 变动的键)。

    - 只碰 SETTINGS_ENV_KEYS / SETTINGS_TOP_KEYS 里的键，其余原样保留；
    - env 做深合并（保留用户其它环境变量），不整体替换；
    - 只在该键确实变化时才计入 changed（幂等：changed 为空则调用方不落盘）。
    """
    new = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    changed = []

    env_patch = (patch or {}).get("env") or {}
    top_patch = {k: v for k, v in (patch or {}).items() if k != "env"}

    env = new.get("env")
    if env_patch or unset_env:
        env = dict(env) if isinstance(env, dict) else {}
        for k, v in env_patch.items():
            if k in SETTINGS_ENV_KEYS and env.get(k) != v:
                env[k] = v
                changed.append(k)
        for k in unset_env:
            if k in SETTINGS_ENV_KEYS and k in env:
                del env[k]
                changed.append(k)
        if env:
            new["env"] = env
        else:
            new.pop("env", None)

    for k, v in top_patch.items():
        if k in SETTINGS_TOP_KEYS and new.get(k) != v:
            new[k] = v
            changed.append(k)
    for k in unset_top:
        if k in SETTINGS_TOP_KEYS and k in new:
            del new[k]
            changed.append(k)

    return new, sorted(set(changed))


def managed_settings_present(existing):
    """判断客户端设置里还有没有本插件写的键（卸载时用）。"""
    if not isinstance(existing, dict):
        return False
    env = existing.get("env")
    if isinstance(env, dict) and any(k in env for k in SETTINGS_ENV_KEYS):
        return True
    return any(k in existing for k in SETTINGS_TOP_KEYS)


# ==============================================================================
# 3P 配置（configLibrary/<appliedId>.json）
# ==============================================================================

# 默认只配这两个：客户端模型列表就来自这份映射，配几个用户就能选几个 ——
# 列一长串旧代号只会让选择器变脏。要用别的模型就把这两条换掉。
RECOMMENDED_MODEL_IDS = ("claude-opus-5-5", "claude-sonnet-5-5")

# 客户端实际认识的全部代号（取自 ~/Library/Application Support/Claude-3p/
# model-catalog/published.json 的 cc / ccd / chat / cowork 四个面）。
# 页面用它来告诉用户「还能换成哪些」，网关的漏配兜底也用它判断是不是客户端代号。
CLIENT_MODEL_IDS = (
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-opus-4-5-20251101", "claude-opus-4-1-20250805",
    "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
    "claude-haiku-5-5", "claude-haiku-4-5-20251001",
    "claude-fable-5-1", "claude-fable-5",
)

def _1m_flags(one_m):
    """1M 上下文相关的三个字段。关掉时一并置否，不留半开半关的状态。"""
    return {"supports1m": bool(one_m), "prefer1m": bool(one_m)}


def default_model_mapping(target="deepseek-flash", one_m=True):
    """只推荐最新代的两个代号，都指向同一个上游模型。

    客户端的选择器直接读这份映射，列全量代号只会让它变脏。上游是第三方模型时
    拿不到按代号区分的提示词，只配最新的两个最省事；要用别的自己在页面里换。
    """
    return [{"name": mid, "labelOverride": target, **_1m_flags(one_m)}
            for mid in RECOMMENDED_MODEL_IDS]


def identity_model_mapping(one_m=True):
    """官方模型中转站用的映射：每个代号都指向同名的真实模型。

    这类用户要的就是 Claude 官方模型本身（中转站转发过来），映射表不该改写模型名，
    但必须**列全** —— 客户端选择器里出现的每个代号都得有对应条目，
    漏一个就会走到兜底逻辑、把用户的 Opus 悄悄换成别的模型。
    """
    return [{"name": mid, "labelOverride": mid, **_1m_flags(one_m)}
            for mid in CLIENT_MODEL_IDS]


DEFAULT_UPSTREAM = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-flash"

# 客户端界面的三个面（Chat / Cowork / Code）。都是 3P 配置里的键，false = 关掉该面。
# **不是默认写入的** —— 只写用户明确改过的那一个，其余保持客户端原样。
# 跟随安装一起写死的 3P 字段。只写这些，其余键一律保留。
_3P_FORCED = {
    "inferenceProvider": "gateway",
    "inferenceCredentialKind": "static",
    "inferenceGatewayAuthScheme": "x-api-key",
}

# 省钱/开箱可用相关的默认值（页面上都可改）
_3P_DEFAULTS = {
    "coworkEgressAllowedHosts": ["*"],   # 不填 Agent 就跑不了 curl
    "scheduledTasksEnabled": False,      # 定时任务整套工具从源头消失
    "modelPrefer1mContext": True,   # 由 build_3p_bundle 按配置项 one_m_context 覆盖
}


def new_config_id():
    return str(uuid.uuid4())


def build_3p_bundle(existing, api_key=None, gateway_url=None, models=None, one_m=None,
                    scheduled_tasks=None, bundled_skills=None):
    """在现有 3P 配置上合并出新的配置，返回 (新配置, 变动的键)。

    **绝不删未知键** —— 客户端自己会往这份文件里写别的字段，删掉会触发它的校验告警。
    Base URL 写本地网关（不是真实上游）：manager 的 sync() 只在读到非本地地址时
    才去学习 upstream_url 并改写，我们两条都自己写好，那个两段式接管就不发生。

    `bundled_skills=True` 表示「剥离随附技能」（写 `disableBundledSkills: true`）。
    它**只在显式传值时写**，不设默认 —— 不写 = 客户端默认的「保留」。
    """
    new = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    changed = []

    def put(key, value):
        if new.get(key) != value:
            new[key] = value
            changed.append(key)

    for k, v in _3P_FORCED.items():
        put(k, v)
    for k, v in _3P_DEFAULTS.items():
        if k == "modelPrefer1mContext" and one_m is not None:
            put(k, bool(one_m))   # 装配时按配置项决定，不留半开半关的状态
            continue
        if k == "scheduledTasksEnabled" and scheduled_tasks is not None:
            put(k, bool(scheduled_tasks))
            continue
        if k not in new:          # 用户已经设置过的，不覆盖
            put(k, v)
    if bundled_skills is not None:
        put("disableBundledSkills", bool(bundled_skills))
    if gateway_url:
        put("inferenceGatewayBaseUrl", gateway_url)
    if models:
        put("inferenceModels", models)
    if api_key:
        put("inferenceGatewayApiKey", api_key)
    return new, sorted(set(changed))


def validate_3p_config(cfg):
    """写后自检：返回问题列表，空列表 = 可以交给客户端。

    只校验「不满足客户端就跑不起来」的项，不重复客户端自己的字段校验。
    """
    problems = []
    if not isinstance(cfg, dict):
        return ["配置不是对象"]
    if cfg.get("inferenceProvider") != "gateway":
        problems.append("inferenceProvider 不是 gateway")
    if not (cfg.get("inferenceGatewayBaseUrl") or "").strip():
        problems.append("inferenceGatewayBaseUrl 为空")
    if not (cfg.get("inferenceGatewayApiKey") or "").strip():
        problems.append("inferenceGatewayApiKey 为空（还没填 API Key）")
    if cfg.get("inferenceCredentialKind") not in ("static", "interactive", "helper-script"):
        problems.append("inferenceCredentialKind 缺失或不受支持")
    models = cfg.get("inferenceModels")
    if not isinstance(models, list) or not models:
        problems.append("inferenceModels 为空（客户端会没有可选模型）")
    else:
        for item in models:
            if not isinstance(item, dict) or not (item.get("name") or "").strip():
                problems.append("inferenceModels 里有条目缺 name")
                break
    return problems


def is_already_installed(cfg_3p, bridge_cfg):
    """幂等判据：3P 已经指向网关、且网关已知上游，就算装过了。"""
    if not isinstance(cfg_3p, dict):
        return False
    if cfg_3p.get("inferenceProvider") != "gateway":
        return False
    if not (cfg_3p.get("inferenceGatewayApiKey") or "").strip():
        return False
    local = (cfg_3p.get("inferenceGatewayBaseUrl") or "")
    pointing_local = bool(re.match(r"^https?://(127\.0\.0\.1|localhost)[:/]", local, re.I))
    return pointing_local or bool((bridge_cfg or {}).get("upstream_url"))


# 上游预设只做两件事：给一个 URL 省得用户敲，以及**知道的话**给一个默认上游模型。
# url 是 Anthropic 格式的 base URL。「其他供应商 / 自建代理」的 url 留空是刻意的 ——
# 地址各人不同，要用户自己填。**不再单列「官方模型中转站」**：它同样填不出 URL，
# 摆一个选不动的入口，只会让人以为选它能自动配好。
UPSTREAM_PRESETS = (
    {"id": "deepseek", "name": "DeepSeek 官方",
     "url": "https://api.deepseek.com/anthropic", "model": "deepseek-flash",
     "key_hint": "在 platform.deepseek.com 申请"},
    # OpenCode 是两家不同的服务，地址也不同 —— Zen 是充值制（84 个模型，含全套真正的
    # claude-* 官方模型），Go 是套餐制（38 个）。填错就调不通，所以分开列。
    {"id": "opencode-zen", "name": "OpenCode Zen（充值制）",
     "url": "https://opencode.ai/zen", "model": "",
     "key_hint": "在 opencode.ai 控制台获取"},
    {"id": "opencode-go", "name": "OpenCode Go（套餐制）",
     "url": "https://opencode.ai/zen/go", "model": "deepseek-flash",
     "key_hint": "在 opencode.ai 订阅后获取"},
    {"id": "custom", "name": "其他供应商 / 自建代理",
     "url": "", "model": "", "key_hint": ""},
)


def find_preset(url):
    """按 URL 反查预设 id，用于页面回显与安装器选默认映射。"""
    norm = (url or "").strip().rstrip("/").lower()
    for p in UPSTREAM_PRESETS:
        if p["url"] and p["url"].rstrip("/").lower() == norm:
            return p["id"]
    return "custom"


def get_preset(preset_id):
    for p in UPSTREAM_PRESETS:
        if p["id"] == preset_id:
            return p
    return UPSTREAM_PRESETS[-1]


def preset_mapping(preset_id, target="", one_m=True):
    """默认映射表只有一条规则，不按用户分类型：

    - **知道上游模型名**（预设带了、或用户指定了）→ 两个最新代号映射到它，
      开箱即用、选择器也干净；
    - **不知道**（用户自填的 URL）→ 全部代号同名直通，等他到页面上拉真实模型
      列表再改。这既不会写错，也不会把上一家的模型名发给下一家。

    自填 URL 的多半就是官方模型中转站，同名直通正是他们要的。
    """
    preset = get_preset(preset_id)
    target = (target or preset["model"] or "").strip()
    if target:
        return default_model_mapping(target, one_m)
    return identity_model_mapping(one_m)
