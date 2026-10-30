#!/usr/bin/env python3
"""版本哨兵：在一份真实抓包上自检「客户端注入面是否已被清洗规则吸收」。

`audit` 回答一个与版本无关的问题：这份入口报文过完现有规则后，还剩什么会被客户端
版本或环境改变？留下来的每一样都是前缀缓存杀手。

    · ① 工具面   —— 剥离 N / 保留 M；不在剥离名单内的 `mcp__*` 逐个列出待判定
                    （本命令不预设是死重还是生产力工具，判定权在你）
    · ② system 面 —— 清洗后仍残留的易变痕迹（日期 / 路径 / 版本号 / 模型宣称 / 计费头 / token 计数…）
    · ③ messages 面 —— user / system 消息纯文本块里的客户端注入标记（注入实际落在这一面）
    · ④ 提示词 / 工具搭配 —— 该删的整段有没有删到（计入结论）；被剥掉的工具是否仍被
      正文引用（只提示、不计入结论）

判定只需**单份抓包**，不依赖任何人工时机。

前置条件：~/.claude-tuner/config.json 中 `"capture_raw_inbound": true`，且已向客户端
发过至少一条消息（探针请求不抓）。

用法：
    python3 claude-tuner/scripts/sentinel.py capture on|off|status   # 抓包开关
    python3 claude-tuner/scripts/sentinel.py audit [--model <模型>] [--session <会话>] [--last]
    python3 claude-tuner/scripts/sentinel.py list
    python3 claude-tuner/scripts/sentinel.py compare <旧抓包> <新抓包>   # 客户端这次动了什么
"""
import argparse
import copy
import difflib
import glob
import json
import os
import re
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.dont_write_bytecode = True  # 不在插件运行目录留 __pycache__（代码以仓库为准）

import gateway  # noqa: E402  （复用与线上完全同一套清洗规则，杜绝哨兵与生产行为不一致）
import tool_rules  # noqa: E402  （配对表与它的判据都在这儿，与网关读的是同一份）

BASE_DIR = os.environ.get("CLAUDE_TUNER_HOME", os.path.expanduser("~/.claude-tuner"))
CAPTURE_DIR = os.path.join(BASE_DIR, "captures")
GLOBAL_NAME = "latest_raw_inbound.json"
PER_MODEL_PREFIX = "latest_raw_inbound_"
MAX_COMPARE_DIFF_LINES = 200   # compare 的逐行差异上限：够看清改了哪几句，又不至于刷屏
MAX_TOOL_DIFF_LINES = 24       # 单个工具定义的差异行上限：工具说明动辄几十行，别刷屏

# 清洗后不该再出现的易变痕迹：命中即说明存在未被规则吸收的注入面
VOLATILE_PATTERNS = {
    "日期": r"\b\d{4}-\d{2}-\d{2}\b",
    "绝对路径": r"/(?:Users|home)/[^\s\"'`,)]+",
    "系统版本": r"\bDarwin \d+(?:\.\d+)*\b",
    "版本号串": r"\b\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)?\b",
    "环境块": r"(?m)^#\s+Environment\b",
    "模型绑定宣称": r"(?i)powered by the model|the model named",
    # 只匹配注入面的真实形态（JSON 键、行首 git status 或 # gitStatus），避免系统提示词里
    # 「run `git status` before ...」等常规说明触发假命中。
    "git 状态": r"(?im)\"gitStatus\"|gitStatus:|^git status|#\s*gitStatus",
    "计费头": r"x-anthropic-billing-header",
    # 剩余 token 数每轮都在掉（网关由 `strip_date` 顺手剥掉）。两边都要认得出：它同时出现在
    # 顶层 system 与 role:system 消息里，只写在 messages 面那张表里会漏掉 system 那一份。
    "Token 计数块": r"<total_tokens>",
}

# messages 面的注入标记：由「规则实际从 messages 里剥什么」倒推（见 canonicalize 的
# 三处 `_msg` / messages 分支），**不是照搬 system 面那套** —— messages 是任意对话
# 正文，日期/路径/版本号在那里大量合法存在，照搬必然满屏假命中。故只收带注入容器或
# 固定机器格式的形态，不收裸字符串。
INJECTION_MARKERS = {
    "gitStatus 注入块": r"(?m)^\s*<system-reminder>\s*(?:As you answer the user's questions, you can use the following context:\s*)?#\s*gitStatus",
    "currentDate 注入块": r"(?m)^\s*<system-reminder>\s*#\s*currentDate",
    "动态日期行": r"(?m)^\s*(?:-\s*)?Today's date is \d{4}-\d{2}-\d{2}\.",
    # 要求标签后**紧跟计数**，不收裸标签 —— 上面那条「不收裸字符串」在它身上正好成立：
    # 裸 `<total_tokens>` 在我们自己的正文里遍地都是（压缩摘要、刚读过的源码、文档都写过它），
    # 实测一次审计假命中 6 处。要求紧跟数字既排除引用，又仍认得出形态变了的注入
    # （如 `<total_tokens>1.5M tokens remaining</total_tokens>`）。
    "Token 计数块": r"<total_tokens>\s*\d",
    "模型绑定宣称": r"(?im)^\s*You are powered by the model\b",
}


# ─────────────────────────── 抓包文件定位 ───────────────────────────

def _session_dir():
    """按会话归档目录。每次从 CAPTURE_DIR 现算 —— 测试会改写 CAPTURE_DIR，缓存成常量就跟着失效。"""
    return os.path.join(CAPTURE_DIR, gateway.CAPTURE_SESSIONS_DIR)


def _norm_session(value):
    """把 --session 的取值归一成归档文件名（不含 .json）。

    接受会话短键（`06aed897`）、完整 uuid（`06aed897-3c5c-…`）、以及子代理/CoWork后缀
    （`06aed897_subagent` / `06aed897_cowork` / `06aed897_cowork_subagent`）—— 与 `--model` 允许写 `<模型>_subagent` / `<模型>_cowork` 是同一套约定。
    """
    v = str(value).strip()
    suffix = ""
    for s in sorted(gateway.KIND_SUFFIX.values(), key=len, reverse=True):
        if v.endswith(s):
            suffix = s
            break
    core = v[: -len(suffix)] if suffix else v
    return core.split("-")[0][: gateway.SESSION_KEY_LEN] + suffix


def _resolve_current(model=None, session=None, last=False):
    """定位「当前」抓包文件，返回绝对路径；找不到返回 None。

    未显式指定 --model 时的自动选择顺序：
      1. 全局快照 latest_raw_inbound.json；
      2. 若不存在，而按模型的抓包恰好只有一个，自动采用；
      3. 若有多个，列出候选并要求显式指定 —— 绝不猜测。

    `--last` 走的是另一条轴：取**最近改动的那份会话归档**。全局快照是「最后写者赢」，
    多对话并存时它未必是你刚跟版的那个；会话归档按 mtime 取最新，答的是「我刚才那条」。
    """
    if session:
        p = os.path.join(_session_dir(), _norm_session(session) + ".json")
        return p if os.path.exists(p) else None

    if model:
        safe = str(model).replace("/", "_").replace(":", "_")
        p = os.path.join(CAPTURE_DIR, PER_MODEL_PREFIX + safe + ".json")
        return p if os.path.exists(p) else None

    if last:
        sessions = sorted(glob.glob(os.path.join(_session_dir(), "*.json")), key=_mtime_key, reverse=True)
        if sessions:
            return sessions[0]

    global_path = os.path.join(CAPTURE_DIR, GLOBAL_NAME)
    if os.path.exists(global_path):
        return global_path

    # 排除 `_subagent.json`、`_cowork.json` 与 `_cowork_subagent.json` 特殊形态抓包，仅在主会话模型抓包间做自动单选（需审子代理或CoWork时可显式传对应后缀）
    per_model = sorted(
        p for p in glob.glob(os.path.join(CAPTURE_DIR, PER_MODEL_PREFIX + "*.json"))
        if not any(p.endswith(s + ".json") for s in gateway.KIND_SUFFIX.values())
    )
    if len(per_model) == 1:
        return per_model[0]
    if len(per_model) > 1:
        print("❌ 未指定 --model，但存在多个按模型的抓包，请显式选择其一：")
        for p in per_model:
            print("     --model %s" % _model_of(p))
        sys.exit(1)
    return None


def _model_of(path):
    return os.path.basename(path)[len(PER_MODEL_PREFIX):-len(".json")]


def _mtime_str(path):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
    except OSError:
        return "?"


def _mtime_key(path):
    """给排序用的数值键 —— `_mtime_str` 在取不到时返回 "?"，拿它排序会把异常项拍到最前。"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ─────────────────────────── 报文结构抽取 ───────────────────────────

def extract_system_texts(payload):
    """把 system 归一化成「文本块列表」，兼容 str / 块数组两种形态。"""
    sysv = payload.get("system")
    if isinstance(sysv, str):
        return [sysv]
    if isinstance(sysv, list):
        out = []
        for b in sysv:
            if isinstance(b, str):
                out.append(b)
            elif isinstance(b, dict) and isinstance(b.get("text"), str):
                out.append(b["text"])
        return out
    return []


def extract_tool_names(payload):
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return []
    return [t["name"] for t in tools if isinstance(t, dict) and t.get("name")]


def cleaned_copy(payload, tp_cfg=None):
    """跑一遍**生产那条路**（`gateway.stabilize_request`），返回清洗后的副本。

    不能只调 `canonicalize`：生产是「先剥工具、再清洗」两步，剥完 `tools` 就少了，
    配对段落的判定依赖剥离前那份工具名。只调一步时哨兵看到的字节与真发出去的不是同一份
    —— 段落该删没删这种故障会被它报成 ✅（铁律 6）。
    """
    p = copy.deepcopy(payload)
    try:
        gateway.stabilize_request(p, None, kind=gateway.classify_kind(p), tool_pruning_cfg=tp_cfg)
    except Exception as exc:  # 清洗器本身出问题时不应阻断哨兵
        print("  ⚠️ stabilize_request() 执行异常: %s" % exc, file=sys.stderr)
    return p


def extract_client_version(payload):
    """从计费头里取客户端版本（清洗前读，清洗后这行就被剥掉了）。"""
    for text in extract_system_texts(payload):
        m = re.search(r"cc_version=([0-9A-Za-z.\-]+)", text)
        if m:
            return m.group(1)
    return "(未标注)"


def extract_client_entrypoint(payload):
    """从计费头里取客户端入口面（local-agent / claude-desktop-3p / …）。

    入口面决定报文的形态：终端面与桌面面的工具集、system 提示词本就不一样，
    跨面比较出来的差异多半是「面差异」而不是「版本差异」。
    """
    for text in extract_system_texts(payload):
        m = re.search(r"cc_entrypoint=([0-9A-Za-z.\-]+)", text)
        if m:
            return m.group(1)
    return "(未标注)"


def _snippet(text, start, end, width=90):
    a, b = max(0, start - width), min(len(text), end + width)
    return "…" + text[a:b].replace("\n", " ⏎ ") + "…"


# ─────────────────────────── 报告 ───────────────────────────

def unknown_tools(names, cfg):
    """既没有类别开关、也不在底线名单里的工具 —— 我们还没对它表过态的那些。

    「已知」的判据是 `tool_rules` 的类别表里有没有它 —— 不看开关的值：给它定开关
    本身就是判定结论（开着是死重、关着是判为保留）。所以判为保留的命名空间不会
    每次跟版都被当成新面孔重报一遍，也不需要另维护一份「允许清单」。

    **含原生工具**：只看 `mcp__` 前缀会漏掉不在任何开关里的原生工具 —— 它们占着上下文
    却没人能剥，而这正是最该被看见的一类。
    """
    stripped = gateway.get_stripped_tool_names(cfg)
    return [n for n in names
            if not gateway.should_strip_tool(n, stripped, cfg)
            and not gateway.category_for(n)
            and n not in tool_rules.PROTECTED_TOOLS]


def _pruning_switch_keys():
    """配置页上每一个精简开关的键 —— 类别表就是这份名单的唯一来源。"""
    return {key for key, _ in tool_rules.CATEGORY_NAMESPACES} | set(tool_rules.CATEGORY_NAMES)


def _audit_tools(raw, tp_cfg, surface_skipped=False):
    """工具面：剥离多少、保留哪些；只把类别表外的工具列为待判定。

    待判定的判据是「我们有没有为它定过开关」，不是「它有没有被剥」——
    判为保留也是判定，见 `unknown_tools`。只看原文（`raw`）：剥离是配置算出来的，
    清洗后的副本对「剥了哪些」没有增量信息。
    """
    cfg = tp_cfg
    stripped = gateway.get_stripped_tool_names(cfg)
    names = extract_tool_names(raw)
    kept = [n for n in names if not gateway.should_strip_tool(n, stripped, cfg)]
    pending = [] if surface_skipped else unknown_tools(names, cfg)

    print()
    print("─" * 78)
    print("① 工具面")
    print("─" * 78)
    print("  %d 个工具 → 剥离 %d，保留 %d" % (len(names), len(names) - len(kept), len(kept)))
    print("  保留：%s" % (", ".join(sorted(kept)) or "(无)"))
    if surface_skipped:
        print("  ℹ️ 该客户端面整面不剥工具（`tool_rules.SKIP_COWORK_PRUNING`）：")
        print("     体积由该面自己承担，换掉的是「每个命名空间逐个判定」的维护成本；")
        print("     工具剥离对前缀恒定零贡献，所以缓存侧没有损失。")
    elif pending:
        mcp_ns, native = {}, []
        for n in pending:
            if n.startswith("mcp__") and "__" in n[6:]:
                mcp_ns.setdefault(n.split("__")[1], []).append(n)
            else:
                native.append(n)
        print("  ⚠️ %d 个工具我们没有对应开关，客户端增删即击穿一次缓存，待逐个判定：" % len(pending))
        for ns in sorted(mcp_ns):
            print("       · mcp__%s__*（%d 个）" % (ns, len(mcp_ns[ns])))
        for n in sorted(native):
            print("       · %s（原生工具）" % n)
        print("     → 判定：是死重（商店管理、技能建议、会话自省…）就把命名空间补进")
        print("       tool_rules.CATEGORY_NAMESPACES（或 CATEGORY_NAMES），默认开；")
        print("       是生产力工具（CoWork 交付物、工作区执行…）也同样补进去、默认关。")
        print("       两种都算判过了：表里有了就不再报，也不必每次跟版重判一遍。")
    else:
        print("  ✅ 类别表已覆盖本次出现的全部工具")
        # 开着、却一个工具都管不到的开关：不是故障（规则本身是好的，只是这一版客户端
        # 不再发那些工具），但它此刻**什么都不做**。静默无用比报出来更糟 —— 用户会以为
        # 那一项正在生效。关着的开关不列：它本来就没打算动任何东西。
        idle = sorted(k for k in _pruning_switch_keys()
                      if tool_rules.tp_flag(cfg, k)
                      and not any(tool_rules.category_for(n) == k for n in names))
        if idle:
            print("  ℹ️ %d 个开关开着、但本次请求里没有它名下的工具（此刻不做事，非故障）：" % len(idle))
            print("     %s" % ", ".join(idle))
            print("     → 客户端不再发这些工具后可考虑删掉该开关；若只面向新客户端则纯属死重。")
    print("  ℹ️ 底线名单里的原生工具（Bash / Read 等）刻意不给开关，客户端增删它们不在这里报")
    return len(pending)


def _audit_system(clean):
    """system 面：清洗后仍残留的易变痕迹，命中即报原文行。"""
    text = "\n".join(extract_system_texts(clean))
    hits = []
    print()
    print("─" * 78)
    print("② system 面（清洗后残留扫描，%d 字节）" % len(text))
    print("─" * 78)
    for label, pattern in VOLATILE_PATTERNS.items():
        found = list(re.finditer(pattern, text))
        if not found:
            continue
        hits.append((label, found))
        print("  ⚠️ %s：%d 处" % (label, len(found)))
        for m in found[:5]:
            print("       %s" % _snippet(text, m.start(), m.end()))
        if len(found) > 5:
            print("       …（其余 %d 处省略）" % (len(found) - 5))
    if not hits:
        print("  ✅ 日期 / 路径 / 版本号 / 环境块 / 模型宣称 / gitStatus / 计费头 全部 0 残留")
    return len(hits)


REMINDER_SPAN = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _in_reminder_container(text, pos):
    """命中位置是否落在 `<system-reminder>` 容器内。"""
    for m in REMINDER_SPAN.finditer(text):
        if m.start() <= pos < m.end():
            return True
    return False


def _audit_message_injections(clean):
    """messages 面：user 与 system 消息的独立文本块里是否还残留客户端注入标记。

    **注入实际落在这一面**（gitStatus 提醒走 user 消息进来；15KB 环境/模型宣称/日期/token 计数走
    `role:system` 消息进来），只扫顶层 system 会给出「全部 0 残留」的假保险。
    只认独立的 text 块：工具输出（tool_result）与助手回复里的同样字样是**引用**。

    **命中分两档**（判据只有一条：它落在哪种块里）：

    · **注入级**：`role:system` 消息（客户端的环境块/模型宣称/token 计数就装在这里，
      与网关 `map_request_texts` 的 legacy 判定同一套），或 user 消息里带
      `<system-reminder>` 容器的块（gitStatus 提醒走这条）—— **计入结论**；
    · **存疑级**：user 消息里的裸形态 —— 用户正文里恰好写了同样的字样（讨论这个仓库时
      随手一提就命中），**不计结论**。

    裸形态不等于安全：客户端若把注入挪进正文且不再带容器，这一档会降级 —— 所以存疑项照样
    全量打印，结论区也会点名要人复核，而不是静悄悄给一个 ✅。
    """
    injected, suspect = [], []
    print()
    print("─" * 78)
    print("③ messages 面（user / system 消息的纯文本块）")
    print("─" * 78)
    scanned = 0
    for msg in clean.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "system"):
            continue
        role = msg.get("role")
        content = msg.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for blk in blocks:
            # 只认独立的 text 块：tool_result 是工具输出（读文档就会带上这些字样），
            # 与「客户端注入」不是一回事。
            if not isinstance(blk, dict) or blk.get("type") != "text":
                continue
            text = blk.get("text")
            if not isinstance(text, str):
                continue
            scanned += 1
            for label, pattern in INJECTION_MARKERS.items():
                for m in re.finditer(pattern, text):
                    is_injection = (role == "system") or _in_reminder_container(text, m.start())
                    (injected if is_injection else suspect).append((label, text, m))
    if injected:
        print("  ⚠️ 注入残留（计入结论）：%d 处" % len(injected))
        for label, text, m in injected[:8]:
            print("     · %s：%s" % (label, _snippet(text, m.start(), m.end())))
        if len(injected) > 8:
            print("     …（其余 %d 处省略）" % (len(injected) - 8))
    if suspect:
        print("  ℹ️ 疑似正文引用（不计结论）：%d 处" % len(suspect))
        for label, text, m in suspect[:8]:
            print("     · %s：%s" % (label, _snippet(text, m.start(), m.end())))
        if len(suspect) > 8:
            print("     …（其余 %d 处省略）" % (len(suspect) - 8))
    if not injected and not suspect:
        print("  ✅ 扫描 %d 个文本块，无注入残留" % scanned)
    elif not injected:
        print("  ✅ 扫描 %d 个文本块，注入面无残留（下列 %d 处落在用户正文里，人工扫一眼即可）"
              % (scanned, len(suspect)))
    return len(injected), len(suspect)


def _has_identifier(text, token):
    """按标识符边界匹配工具名（前后不得紧邻 ASCII 字母/数字/下划线），防止短名命中普通长单词内部。"""
    return bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])", text))


def _audit_prompt_tool_pairing(raw, clean, tp_cfg=None):
    """提示词 / 工具搭配：两个方向都要看。

    · **该删的整段没删掉**（`missing_pair_paragraphs`）：工具全被剥了，解释它们的整段
      却还在 —— 客户端多半改了措辞导致锚点失配。**这是故障，计入结论**。
    · **工具剥了、正文里还提一句**：段落边界不清楚的已知取舍，**只提示、不计入结论**。

    只扫**顶层 system 面**，不扫 messages —— messages 是对话正文，讨论到某个工具名是常态。
    孤句那一侧只看**最终要发出去的**那份（clean）；失配那一侧要看客户端发来的原文（raw），
    因为清洗之后该段可能已经被删掉了。
    """
    cfg = tp_cfg if tp_cfg is not None else gateway.load_tool_pruning_config()
    stripped_names = gateway.get_stripped_tool_names(cfg)
    gone = [t.get("name") or "" for t in (raw.get("tools") or [])
            if isinstance(t, dict) and gateway.should_strip_tool(t.get("name") or "", stripped_names, cfg)]
    text = "\n".join(extract_system_texts(clean))
    hits = []
    for name in gone:
        # 短名也要查：正文里常常只写 `open_session_in` 而不带命名空间前缀。
        # 一个工具只报一次 —— 长短名同时命中是常态，按工具聚合。
        found = [t for t in ({name, name.split("__")[-1]}) if len(t) >= 6 and _has_identifier(text, t)]
        if found:
            hits.append((name, sorted(found)))
    missing = tool_rules.missing_pair_paragraphs(
        "\n".join(extract_system_texts(raw)), cfg, stripped_names,
        incoming_tools=extract_tool_names(raw))
    print()
    print("─" * 78)
    print("④ 提示词 / 工具搭配")
    print("─" * 78)
    if missing:
        print("  ⚠️ 这些配对的工具已全被剥离，但它们的说明段落一处都没找到 —— 客户端可能改了措辞：")
        for m in missing:
            print("       · %-20s 锚点 %r" % (m["id"], m["anchor"]))
        print("     → 工具没了、说明还在，模型会被引到一个不存在的工具上。改 tool_rules.PROMPT_PAIRS")
        print("       里这条的 anchor 即可（只取段首一小截，段落后面怎么改都还认得出）。")
    if not hits and not missing:
        print("  ✅ %s" % ("没有工具被剥（该面整面不剥），提示词与工具表不会脱节" if not gone
                          else "被剥掉的 %d 个工具：该删的整段都删到了，剩下的也没有被正文引用" % len(gone)))
    if hits:
        print("  ℹ️ 以下工具已被剥掉，但正文仍在引用：")
        for name, tokens in hits:
            print("       · %s   （正文里写作 %s）" % (name, "、".join("`%s`" % t for t in tokens)))
        print("    整段边界清楚的可以顺手剥掉整段；夹在句子中段的按约定不动，保留原样。")
        print("    不计入下方结论 —— 这是已知取舍，不是故障。")
    return hits, len(missing)


def cmd_audit(model, session=None, last=False):
    cur = _resolve_current(model, session, last=last)
    if not cur:
        _die_no_capture(model, session)

    raw = _load(cur)
    # 剥离口径走生产同一个函数（`_stabilize` 里那次调用的入口）：CoWork 面整面不剥。
    base_cfg = gateway.load_tool_pruning_config()
    tp_cfg = gateway.pruning_cfg_for_surface(base_cfg, gateway.classify_kind(raw))
    surface_skipped = tp_cfg is not base_cfg
    clean = cleaned_copy(raw, tp_cfg=tp_cfg)
    print("=" * 78)
    print("版本哨兵 · 注入面覆盖自检")
    print("=" * 78)
    print("  抓包    ：%s   %s" % (os.path.basename(cur), _mtime_str(cur)))
    # 同一时刻可能开着多个对话，全局那份是「最后写者赢」—— 必须让审的人看见这份到底是谁的
    key = gateway.session_capture_key(raw)
    print("  会话    ：%s" % (gateway.extract_session_id(raw) or "(未标注)"))
    print("  会话归档：%s" % (
        "sessions/%s.json%s" % (key, "" if os.path.isfile(os.path.join(_session_dir(), key + ".json"))
                                else "（已过期或被清理）") if key else "无（该请求没有会话 id）"))
    print("  客户端  ：cc_version=%s  entrypoint=%s"
          % (extract_client_version(raw), extract_client_entrypoint(raw)))
    print("  模型    ：%s" % (raw.get("model") or "(未标注)"))
    print("  system  ：%d 块 / %d 字节（清洗前）" % (
        len(extract_system_texts(raw)), sum(len(x) for x in extract_system_texts(raw))))

    findings = _audit_tools(raw, tp_cfg, surface_skipped=surface_skipped) + _audit_system(clean)
    injected, suspect = _audit_message_injections(clean)
    findings += injected
    _hits, pair_findings = _audit_prompt_tool_pairing(raw, clean, tp_cfg=tp_cfg)
    findings += pair_findings

    print()
    print("─" * 78)
    print("⑤ 结论")
    print("─" * 78)
    if findings == 0:
        print("  ✅ 当前注入面已被现有规则完全吸收，本次升级无需动作。")
    else:
        print("  ⚠️ 发现 %d 处待处理的注入面 —— 按上面各条的说明处理：工具没开关的补进类别表"
              "（默认开=剥、默认关=留），配对段落失配的改 PROMPT_PAIRS 的锚点。" % findings)
        print("     处理完重跑本命令确认转为 ✅。")
    if suspect:
        print("  ℹ️ 另有 %d 处形似注入但落在**用户正文**里（不计结论）：若你确认客户端把注入"
              "挪进了正文且不再带容器，这里会降级 —— 人工扫一眼再收工。" % suspect)
    print()
    print("  提示：确认无误后可关闭抓包以恢复零 I/O 开销：")
    print("        python3 claude-tuner/scripts/sentinel.py capture off")


def cmd_list():
    global_path = os.path.join(CAPTURE_DIR, GLOBAL_NAME)
    per_model = sorted(glob.glob(os.path.join(CAPTURE_DIR, PER_MODEL_PREFIX + "*.json")))
    sessions = sorted(glob.glob(os.path.join(_session_dir(), "*.json")))
    files = ([global_path] if os.path.isfile(global_path) else []) + per_model
    if not files and not sessions:
        print("抓包目录为空：%s" % CAPTURE_DIR)
        print("请先在 ~/.claude-tuner/config.json 里设 \"capture_raw_inbound\": true，并发一条消息。")
        return
    print("可用抓包（%s）：" % CAPTURE_DIR)
    for f in files:
        print("  %-52s %s" % (os.path.basename(f), _mtime_str(f)))
    model_files = [f for f in files if os.path.basename(f) != GLOBAL_NAME]
    if model_files:
        print()
        print("  --model 取值示例：")
        for f in model_files:
            print("    --model %s" % _model_of(f))
    # 按会话归档：同时开着多个对话时，只有这一栏不会被彼此顶掉
    if sessions:
        sessions.sort(key=_mtime_key, reverse=True)
        print()
        print("按会话归档（%s · 多对话并存时看这里）：" % _session_dir())
        for f in sessions:
            print("  %-52s %s" % (os.path.basename(f), _mtime_str(f)))
        print()
        print("  --session 取值示例：")
        for f in sessions[:3]:
            print("    --session %s" % os.path.basename(f)[:-len(".json")])
        print("  想省掉认键这一步：audit --last（取最近改动的会话归档）")


def _resolve_key(key):
    """按 `list` 给出的键找抓包：会话键 → 模型键 → 直接当路径。找不到返回 None。"""
    if os.path.isfile(key):
        return key
    p = os.path.join(_session_dir(), key + ".json")
    if os.path.isfile(p):
        return p
    p = os.path.join(CAPTURE_DIR, PER_MODEL_PREFIX + key + ".json")
    return p if os.path.isfile(p) else None


def _tool_map(payload):
    return {t["name"]: t for t in (payload.get("tools") or [])
            if isinstance(t, dict) and t.get("name")}


def _tool_def_diff(old_tool, new_tool, limit=MAX_TOOL_DIFF_LINES):
    """两个工具定义的逐行差异（排序键的 JSON 缩进形态，人能直接读）。

    compare 的立身之本是「客户端这次到底动了什么」—— 只报一个工具名等于没答，
    逼着审的人再手写一段脚本去看。
    """
    dump = lambda t: json.dumps(t, sort_keys=True, ensure_ascii=False, indent=2).splitlines()
    return list(difflib.unified_diff(dump(old_tool), dump(new_tool), lineterm="", n=1))[:limit]


def _diff_captures(old, new):
    """两份报文的差异：工具增删 / 定义改动 / system 逐块文本。

    与 audit 的分工：audit 答「这次注入面有没有被我们的规则吸收」，compare 答
    「客户端这次到底动了什么」—— 前者看的是规则覆盖，后者看的是客户端的改动本身。
    """
    ta, tb = _tool_map(old), _tool_map(new)
    dump = lambda d: json.dumps(d, sort_keys=True, ensure_ascii=False)
    return {
        "tools": {
            "added": sorted(set(tb) - set(ta)),
            "removed": sorted(set(ta) - set(tb)),
            "changed": sorted(n for n in set(ta) & set(tb) if dump(ta[n]) != dump(tb[n])),
        },
        "system_old": extract_system_texts(old),
        "system_new": extract_system_texts(new),
    }


def cmd_compare(key_a, key_b, allow_cross_surface=False):
    """两份抓包对一遍：客户端这次升级动了什么。

    跟版时最想先知道的就是这个：客户端这次到底动了哪几句。
    """
    pa, pb = _resolve_key(key_a), _resolve_key(key_b)
    if not pa or not pb:
        print("❌ 找不到抓包：%s" % ("、".join(k for k, p in ((key_a, pa), (key_b, pb)) if not p)))
        print("   用 `sentinel.py list` 查可用取值（会话键 / 模型键 / 也可直接给文件路径）")
        sys.exit(1)
    old, new = _load(pa), _load(pb)
    d = _diff_captures(old, new)
    ep_old, ep_new = extract_client_entrypoint(old), extract_client_entrypoint(new)
    print("=" * 78)
    print("版本哨兵 · 两份抓包对比")
    print("=" * 78)
    for tag, path, p, ep in (("旧", pa, old, ep_old), ("新", pb, new, ep_new)):
        print("  %s：cc_version=%-14s entrypoint=%-20s 工具 %2d 个 · system %d 字节   %s" % (
            tag, extract_client_version(p), ep, len(_tool_map(p)),
            sum(len(x) for x in extract_system_texts(p)), os.path.basename(path)))

    if (ep_old != ep_new) and "(未标注)" not in (ep_old, ep_new):
        print()
        if allow_cross_surface:
            print("  ℹ️ 客户端面不同（%s → %s）—— 已用 --allow-cross-surface 承认这是有意跨面比较。"
                  % (ep_old, ep_new))
        else:
            print("  ⚠️ 客户端面不同（%s → %s）：两个面的工具集与 system 提示词本就不一样，"
                  % (ep_old, ep_new))
            print("     下面的「新增 / 移除」多半是**面差异**而不是版本差异。")
            print("     跟版要比同一个面（两面各挑一份），确属有意跨面比就加 --allow-cross-surface。")

    print()
    print("① 工具面")
    for label, key in (("新增", "added"), ("移除", "removed"), ("定义有变", "changed")):
        items = d["tools"][key]
        print("  %s：%s" % (label, "、".join(items) if items else "无"))
    if d["tools"]["changed"]:
        ta, tb = _tool_map(old), _tool_map(new)
        print()
        print("  定义有变的工具（逐行差异）：")
        for name in d["tools"]["changed"]:
            print("    · %s" % name)
            for line in _tool_def_diff(ta[name], tb[name]):
                print("      %s" % line[:200])

    print()
    print("② system 提示词面")
    so, sn = d["system_old"], d["system_new"]
    print("  块数：%d → %d" % (len(so), len(sn)))
    for i in range(max(len(so), len(sn))):
        a = so[i] if i < len(so) else None
        b = sn[i] if i < len(sn) else None
        if a is None or b is None:
            print("  block %d：%s" % (i, "新增" if a is None else "消失"))
        elif a == b:
            print("  block %d：%d 字节 · 完全一致" % (i, len(a)))
        else:
            print("  block %d：%d → %d 字节 · 有差异" % (i, len(a), len(b)))
    diff = []
    for i in range(max(len(so), len(sn))):
        a = (so[i] if i < len(so) else "").splitlines()
        b = (sn[i] if i < len(sn) else "").splitlines()
        diff += list(difflib.unified_diff(a, b, "旧 block %d" % i, "新 block %d" % i,
                                          lineterm="", n=0))
    if not diff:
        print("  逐行比完：system 一字未动")
        return
    print()
    print("  逐行差异（%d 行）：" % len(diff))
    for line in diff[:MAX_COMPARE_DIFF_LINES]:
        print("    " + line[:200])
    if len(diff) > MAX_COMPARE_DIFF_LINES:
        print("    …（还有 %d 行，已截断）" % (len(diff) - MAX_COMPARE_DIFF_LINES))


def _die_no_capture(model, session=None):
    what = ("model=%s" % model) if model else (("session=%s" % session) if session else "")
    print("❌ 找不到原始报文抓包" + ("（%s）" % what if what else ""))
    print("   目录: %s" % (CAPTURE_DIR if not session else _session_dir()))
    print()
    print("   请确认：")
    print("     ① ~/.claude-tuner/config.json 中 \"capture_raw_inbound\": true")
    print("     ② 开启后向 Claude 发过至少一条消息（探针请求不会被抓）")
    print("     ③ 用 `sentinel.py list` 查可用的 --model / --session 取值")
    sys.exit(1)


def cmd_capture(action="status"):
    """开 / 关入口报文抓包（写 ~/.claude-tuner/config.json，保留权限位）。

    抓包开关是维护者动作（配置台上的采集开关是刻意不做的），所以给它一个 CLI 入口：
    跟版流程第一步不必再手改 JSON。
    """
    import common
    path = os.path.join(BASE_DIR, "config.json")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        print("❌ 找不到配置文件：%s（先跑 claude-tuner/scripts/manager.py ensure 把网关装好）" % path)
        sys.exit(1)
    except json.JSONDecodeError as exc:
        print("❌ 配置文件不是合法 JSON：%s（%s）" % (path, exc))
        sys.exit(1)

    if action == "status":
        state = bool(cfg.get("capture_raw_inbound"))
        print("抓包：%s（%s）" % ("开着" if state else "关着", path))
        return

    want = (action == "on")
    if bool(cfg.get("capture_raw_inbound")) == want:
        print("抓包本来就%s着，未改动：%s" % ("开" if want else "关", path))
    else:
        cfg["capture_raw_inbound"] = want
        common.atomic_write_json(path, cfg)
        print("抓包已%s：%s" % ("开" if want else "关", path))
    print("  开关由网关按 mtime 热读，改完不用重启网关。")
    if want:
        print("  现在去客户端发一条消息（探针请求不会被抓），再跑 sentinel.py audit --last。")
    else:
        print("  入口报文恢复零 I/O。")


def main():
    ap = argparse.ArgumentParser(
        description="客户端注入面覆盖自检",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("audit", help="抓一份报文后运行：自检注入面是否已被规则吸收")
    p.add_argument("--model", help="按模型区分（见 list）")
    p.add_argument("--session", help="按会话区分（见 list；接受短键、完整 uuid、或 <键>_subagent）")
    p.add_argument("--last", action="store_true",
                   help="取最近改动的会话归档（多对话并存时最省事，免去认会话键）")

    sub.add_parser("list", help="列出可用抓包")

    pc = sub.add_parser("compare", help="两份抓包对比：客户端这次升级动了什么")
    pc.add_argument("old", help="旧抓包（会话键 / 模型键 / 文件路径，见 list）")
    pc.add_argument("new", help="新抓包（同上）")
    pc.add_argument("--allow-cross-surface", action="store_true",
                    help="承认这是跨客户端面的比较（默认会告警：面差异不是版本差异）")

    pcap = sub.add_parser("capture", help="开关入口报文抓包（写 ~/.claude-tuner/config.json）")
    pcap.add_argument("action", nargs="?", choices=["on", "off", "status"], default="status")

    args = ap.parse_args()
    if args.cmd == "audit":
        cmd_audit(args.model, args.session, last=args.last)
    elif args.cmd == "list":
        cmd_list()
    elif args.cmd == "compare":
        cmd_compare(args.old, args.new, allow_cross_surface=args.allow_cross_surface)
    elif args.cmd == "capture":
        cmd_capture(args.action)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
