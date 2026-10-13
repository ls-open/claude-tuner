#!/usr/bin/env python3
"""工具剥离规则：纯判断，无 IO、无网络。

为什么单独一个文件
------------------
这份判断有两个消费者：**网关**（每轮真拿它决定剥不剥）和**配置页**（拿它算出
「按现在的配置，这个工具会不会被剥」给用户看）。两边各写一份的话，页面就会在
某次改动后开始骗人 —— 显示的和实际发生的不一样，而用户没有别的办法发现。

所以规则只在这里写一份：网关 import 它，配置页也 import 它。

它是**纯函数层**（同 config_ops.py）：打进去一份 cfg，返回判断结果，不读盘不写盘。
纯函数的好处是能被测试以生产同一形态直接调用（铁律 6），不必起进程、不必造假文件。
"""
from common import DEFAULT_TOOL_PRUNING_CONFIG


def tp_flag(cfg, key):
    """读取工具精简开关，缺键时回退到 DEFAULT_TOOL_PRUNING_CONFIG 声明默认值。"""
    return cfg.get(key, DEFAULT_TOOL_PRUNING_CONFIG[key])


# 与任何开关无关、恒定剥离的名单。WaitForMcpServers 是客户端等待 MCP 就绪的内部工具，
# 对模型毫无意义，任何配置下都不该占一份上下文。
ALWAYS_STRIPPED = ("WaitForMcpServers", "wait_for_mcp_servers")

# 类别键 → (该类别覆盖的工具名，前缀匹配的命名空间)
# 顺序即优先级：先命中的类别就是页面上显示的原因。
CATEGORY_NAMESPACES = (
    ("strip_session_mgmt_tools", (
        "mcp__ccd_session", "mcp__ccd_directory", "mcp__ccd_sidebar",
        "mcp__ccd_window", "mcp__ccd_host", "mcp__ccd_settings", "mcp__session_info",
    )),
    ("strip_plugin_mgmt_tools", ("mcp__plugins", "mcp__skills")),
    ("strip_pr_ci_tools", ("mcp__ccd_pr",)),
    ("strip_terminal_tools", ("mcp__terminal",)),
)

# 类别键 → 该类别覆盖的精确工具名
CATEGORY_NAMES = {
    "strip_native_web_tools": ("WebSearch", "WebFetch", "web_search", "web_fetch",
                               "mcp__workspace__web_fetch"),
    "strip_notebook_tools": ("NotebookEdit",),
    "strip_design_sync_tools": ("DesignSync", "design_sync"),
}

# 页面与 CLI 共用的类别中文名（配置页直接显示它，避免两处各译一遍）
CATEGORY_LABELS = {
    "strip_native_web_tools": "原生联网工具",
    "strip_session_mgmt_tools": "桌面端遥控与会话自省",
    "strip_pr_ci_tools": "PR 关联与 CI 监控",
    "strip_terminal_tools": "右侧终端面板",
    "strip_notebook_tools": "Notebook 单元格编辑",
    "strip_design_sync_tools": "DesignSync",
    "strip_plugin_mgmt_tools": "插件市场与技能建议",
    "custom": "你自己勾的",
    "builtin": "恒定剥离",
    "enabled": "总开关关着时的名单",
}


def get_stripped_tool_names(cfg):
    """根据配置动态计算需要被物理剥离的精确工具名称黑名单集合。"""
    strip_set = set(ALWAYS_STRIPPED)
    if not tp_flag(cfg, "enabled"):
        return strip_set

    for key, names in CATEGORY_NAMES.items():
        if tp_flag(cfg, key):
            strip_set.update(names)
    for ct in cfg.get("custom_strip_tools", []):
        if isinstance(ct, str) and ct:
            strip_set.add(ct)
    return strip_set


def strip_reason(tool_name, stripped_names, cfg):
    """这个工具为什么会被剥掉？返回原因（类别配置键 / 'custom' / 'builtin'），不剥返回空串。

    之所以要连原因一起给出来：配置页要把「哪些是预设规则替你剥的、哪些是你自己勾的」
    分开显示 —— 只说剥了不说谁剥的，用户看到一个自己没勾过的勾会以为是界面出错。
    """
    if not tp_flag(cfg, "enabled"):
        return "enabled" if tool_name in stripped_names else ""

    # 先看类别命名空间：它们的归属是确定的，能给出准确原因
    for key, prefixes in CATEGORY_NAMESPACES:
        if tp_flag(cfg, key) and any(tool_name.startswith(p) for p in prefixes):
            return key
    for key, names in CATEGORY_NAMES.items():
        if tp_flag(cfg, key) and tool_name in names:
            return key

    # 再看到名单里的（自定义清单 / 恒定名单）。自定义优先报出来，那是用户自己做的决定。
    if tool_name in stripped_names:
        custom = cfg.get("custom_strip_tools") or []
        return "custom" if tool_name in custom else "builtin"
    return ""


def should_strip_tool(tool_name, stripped_names, cfg):
    """判断单个工具是否应被剥离（纯粹服从配置规则，按功能命名空间精准剥离死重工具）。"""
    return bool(strip_reason(tool_name, stripped_names, cfg))


# ==============================================================================
# 工具与系统提示词的配对关系
# ==============================================================================
#
# 为什么需要这张表
# ----------------
# 剥掉一个工具、却把它在系统提示词里的说明段落留着，会留下一个**孤句**：
# 模型读到的是一段「请用 X 工具做 Y」的指引，而 X 根本不在工具列表里。
# 这不是省钱，是把模型引到一个不存在的工具上 —— 比不剥更糟。
#
# 所以配对关系在这里登记成**数据**（不是散在代码里的 if）：剥哪个工具就连带走哪段，
# 由 strip_reason 的同一份判断驱动 —— 无论是类别开关剥的，还是用户自己勾的。
#
# 只有「整段存在的意义就是解释这个工具」的段落才进这张表。
# 段中顺带提一句工具名的（例如通用指引里那句「invoke it via Skill」）**不进**：
# 整段删会把无关指引一起删掉，句中手术又容易切坏。那类交给 orphan_mentions() 去发现。
PROMPT_PAIRS = (
    {
        "id": "ccd_pr",
        "why": "PR 关联与 CI 监控工具的说明段落",
        # 段落里点名的工具全部被剥，这段才没有意义 —— 部分剥则原样保留（段落仍成立）
        "tools": ("mcp__ccd_pr__get_status", "mcp__ccd_pr__bind_pr"),
        "pattern": r"(?m)^After opening a PR, use the ccd_pr tools:[\s\S]*?"
                   r"never enable auto-merge unless the user asked\.\s*\n*",
    },
    {
        "id": "ccd_settings",
        "why": "Code 标签页偏好工具的说明段落",
        "tools": ("mcp__ccd_settings__get_settings", "mcp__ccd_settings__set_setting"),
        "pattern": r"(?m)^To read or change the user's Code tab preferences in this app[\s\S]*?"
                   r"security settings stay theirs\.\s*\n*",
    },
    {
        "id": "ccd_host_sync",
        "why": "worktree 同步工具的说明段落",
        "tools": ("mcp__ccd_host__sync_with_base_branch",),
        "pattern": r"(?m)^When this session runs in a worktree the app made for it[\s\S]*?"
                   r"merge the base branch yourself there\.\s*\n*",
    },
    {
        "id": "ccd_window_split",
        "why": "分屏工具的说明段落",
        "tools": ("mcp__ccd_window__open_session_in",),
        "pattern": r"(?m)^To show sessions side by side \(split view\)[\s\S]*?"
                   r"`mcp__ccd_window__open_session_in`\.\s*\n*",
    },
    {
        # 2026-10 从真实请求体里比对出来的：这段整段只为解释 show_pane。
        # 我们默认**保留** ccd_view（呈现面板，客户端提示词要求模型用它），
        # 但用户一旦自己勾掉它，这段就必须跟着走。
        "id": "ccd_view_show_pane",
        "why": "呈现面板 show_pane 的说明段落",
        "tools": ("mcp__ccd_view__show_pane",),
        "pattern": r"(?m)^To show the user this session's diff, a file at a line, or its terminal, "
                   r"PR, tasks or plan pane, use `mcp__ccd_view__show_pane` instead of describing it\.\s*\n*",
    },
)


def pair_is_orphaned(pair, stripped_names, cfg):
    """这对配对是不是「工具已经全被剥了」——即它的段落已成孤句。"""
    return all(should_strip_tool(name, stripped_names, cfg) for name in pair["tools"])


def active_pairs(cfg, stripped_names):
    """当前配置下已成孤句的配对（工具全剥了，段落也该走）。"""
    return [p for p in PROMPT_PAIRS if pair_is_orphaned(p, stripped_names, cfg)]


def paired_tool_names(cfg):
    """哪些工具名参与了配对 —— 配置页要据此告诉用户「勾它也会带走一段提示词」。"""
    mapping = {}
    for pair in PROMPT_PAIRS:
        for name in pair["tools"]:
            mapping.setdefault(name, []).append(pair["why"])
    return mapping


def orphan_mentions(text, stripped_names):
    """扫一段文本，找出「已经被剥掉、却仍被提示词提到」的工具名。

    这是**探测器**，不是改写器：我们不去自动删这类句子（整段删会连累无关内容，
    句中删容易切坏），只把它报出来。客户端哪天新增了工具或交叉引用，这里会先知道，
    不必等用户遇到怪问题再回头查。
    """
    if not isinstance(text, str) or not text:
        return []
    return sorted({name for name in stripped_names if name and name in text})


def sanitize_custom_list(value, cap=500, name_max=200):
    """清洗 custom_strip_tools：只留非空字符串、去重、限量。

    这个清单会被当成工具名黑名单用，且会落盘。前端送什么进来都得先过这一关 ——
    落一个几万项、每项几万字符的数组进配置，下次读配置的每条路径都要为它付代价。
    """
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or len(name) > name_max or name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= cap:
            break
    return out
