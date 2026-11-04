#!/usr/bin/env python3
"""每条清洗规则的「触发物」判据 —— 用来发现**规则哑火**。

规则是模式匹配：客户端把措辞改一个字，规则匹配不到就什么都不做、也不报错。
现有两个观测都盖不住这一种：

- `rule_failed` 只兜**抛异常**，匹配不到不算异常；
- `changed` 只记「动了什么」，**不记「该动却没动」**。

于是「触发物还在报文里、规则却没动手」没有任何信号。而它会毒化 `sys_hash` 那条判据：
跨天时前缀变了，看起来像客户端或上游的事，实际是我们的规则失效了。

**判据不是「这条规则 0 次触发」**：`strip_git_status` 只在首轮有活干，三条 guideline
只在客户端真发那几段时才动手 —— 按次数报警会天天误报，而假警报等于把真警报静音。
这里记的是 2×2 的另一格：**触发物在报文里 → 规则必须动手**。

两个面分开看：
- `SYSTEM_SCOPE`：顶层 `system` 字段与 `role: system` 的注入消息。这里的裸字符串就是注入。
- `BODY_SCOPE`：其余对话正文。**只认注入容器形态**（`<system-reminder>` 里那份），
  绝不收裸字符串 —— 正文里「Today's date is」「<total_tokens>」是正常讨论与引用，
  按裸串判会满屏假命中（哨兵踩过一次：裸 `<total_tokens>` 单次审计假命中 6 处）。

本模块是纯函数层（同 `tool_rules.py`）：不读写磁盘、不看配置值，只回答
「这份报文里有东西该被这条规则处理吗」。网关每轮拿它和 `changed` 比对，哨兵拿它做
单份抓包的深检 —— 一份判据两个消费者，免得两边慢慢长歪。
"""
import re

SYSTEM_SCOPE = "system"
BODY_SCOPE = "body"

# 正文面只在这里面找注入标记：容器之外的一律不认。
REMINDER_OPEN = "<system-reminder>"

# 规则键 → [(面, 正则)]。每个正则都必须**只匹配该规则真会处理的形态**：
# 宽一个词，就会换来源源不断的假警报。
RULE_TRIGGERS = {
    "strip_billing_header": [
        (SYSTEM_SCOPE, r"x-anthropic-billing-header:"),
    ],
    "strip_date": [
        (SYSTEM_SCOPE, r"#\s*currentDate\b|Today's date is \d{4}-\d{2}-\d{2}"),
        (BODY_SCOPE, r"#\s*currentDate\b|Today's date is \d{4}-\d{2}-\d{2}"),
    ],
    "strip_git_status": [
        (SYSTEM_SCOPE, r"(?m)^#\s*gitStatus\b|\"gitStatus\"\s*:"),
        (BODY_SCOPE, r"(?m)#\s*gitStatus\b"),
    ],
    "strip_model_declarations": [
        (SYSTEM_SCOPE, r"(?im)You are powered by the model named"
                       r"|^This iteration of Claude is Claude"
                       r"|^#\s+Environment\s*$"),
    ],
    "strip_total_tokens_tag": [
        (SYSTEM_SCOPE, r"<total_tokens>\s*\d"),
    ],
    # 三条行为准则：判据故意只取「段首锚点」，不取 requires / any_of 那些特征词。
    # 客户端把 requires 的措辞换掉时，正段还在、规则却剥不动 —— 那正是要报的哑火；
    # 拿整套特征词当判据就把它一起盖住了。
    "strip_security_guidelines": [
        (SYSTEM_SCOPE, r"(?m)^IMPORTANT:\s*Assist with authorized security testing"),
    ],
    "strip_pronoun_guidelines": [
        (SYSTEM_SCOPE, r"(?m)^When you use a pronoun for someone"),
    ],
    "strip_caution_guidelines": [
        (SYSTEM_SCOPE, r"(?m)^For actions that are hard to reverse or outward-facing"),
    ],
}

_COMPILED = {rule: [(scope, re.compile(pattern)) for scope, pattern in triggers]
             for rule, triggers in RULE_TRIGGERS.items()}

# 第二类信号：**清洗之后**仍留在报文里的易变痕。它与上面那张表互补 ——
# 上面只能发现「锚点还在、规则没动」，客户端把锚点本身改了措辞时它是瞎的；
# 这一张不问措辞、只问结果，所以正好接住那种情况（代价是假阳性风险更高，
# 因此每个图案都必须窄到「只可能来自客户端注入」，实测真实报文 0 命中）。
RESIDUE_CHECKS = {
    "strip_date": [(SYSTEM_SCOPE, r"\b\d{4}-\d{2}-\d{2}\b"),
                   (BODY_SCOPE, r"\b\d{4}-\d{2}-\d{2}\b")],
    "strip_billing_header": [(SYSTEM_SCOPE, r"x-anthropic-billing-header:")],
    "strip_total_tokens_tag": [(SYSTEM_SCOPE, r"<total_tokens>\s*\d")],
    "strip_git_status": [(SYSTEM_SCOPE, r"(?m)^#\s*gitStatus\b"),
                         (BODY_SCOPE, r"(?m)^#\s*gitStatus\b")],
    "strip_model_declarations": [(SYSTEM_SCOPE, r"(?im)You are powered by the model named")],
}

_RESIDUE = {rule: [(scope, re.compile(pattern)) for scope, pattern in checks]
            for rule, checks in RESIDUE_CHECKS.items()}


def collect_texts(parsed):
    """把报文切成三堆文本：顶层 `system`、`role: system` 的消息、正文。

    三者必须分开：顶层 `system` 是客户端自己的系统提示词；`role: system` 的消息里
    **既有客户端注入，也有把整份文件当上下文塞进来的转储**（实测见过 24 KB 的
    仓库文档原文）。正文里则只有 `<system-reminder>` 容器内的那部分才可能来自注入。
    """
    system_field = []
    system_msgs = []
    body = []

    def feed(value, bucket):
        if isinstance(value, str):
            bucket.append(value)
        elif isinstance(value, list):
            for blk in value:
                if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                    bucket.append(blk["text"])

    feed(parsed.get("system"), system_field)
    for msg in parsed.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        feed(msg.get("content"), system_msgs if msg.get("role") == "system" else body)
    return "\n".join(system_field), system_msgs, body


def _match(patterns, system_field, system_msg_texts, body_texts, msg_limit=None):
    """按面逐条匹配。`msg_limit` 给残留扫描用：超过这个长度的 role:system 消息是转储，
    不是注入 —— 拿它对残留报假警报，一天能报几十条（实测 24 KB 的文档转储里就有日期）。"""
    guarded = [t for t in body_texts if REMINDER_OPEN in t]
    msgs = system_msg_texts if msg_limit is None else [t for t in system_msg_texts if len(t) <= msg_limit]
    for rule, checks in patterns.items():
        for scope, regex in checks:
            if scope == SYSTEM_SCOPE:
                if regex.search(system_field) or any(regex.search(t) for t in msgs):
                    yield rule
                    break
            elif any(regex.search(t) for t in guarded):
                yield rule
                break


# role:system 里超过这个长度的消息按「转储」处理，不参与残留判定。
# 实测客户端注入都在数百字节（139 / 354 / 366），而文档转储在 5 KB 以上。
RESIDUE_MSG_LIMIT = 2048


def present_rules(parsed):
    """这份报文里，哪些规则的触发物出现了？返回规则键集合。"""
    system_field, system_msgs, body = collect_texts(parsed)
    return set(_match(_COMPILED, system_field, system_msgs, body))


def silent_rules(parsed_after_cleaning, rules_cfg, changed):
    """洗完触发物还在、`changed` 里却没有这条规则 —— 返回这些规则的键。

    `rules_cfg` 用生产同一口径取开关值（`r.get(key, DEFAULT_RULES[key])` 由调用方保证），
    这里只回答「开了且该动却没动」。
    """
    fired = {c for c in changed or [] if isinstance(c, str)}
    return [rule for rule in sorted(present_rules(parsed_after_cleaning))
            if rules_cfg.get(rule, False)
            and not any(c == rule or c.startswith(rule + "_") for c in fired)]


def residue_rules(cleaned_parsed):
    """清洗完之后，报文里还留着哪几类易变痕 —— 返回规则键集合。

    这是「锚点被改措辞」那条漏网的补丁：不管客户端怎么改写，剥干净了就是干净了。
    """
    system_field, system_msgs, body = collect_texts(cleaned_parsed)
    return set(_match(_RESIDUE, system_field, system_msgs, body, msg_limit=RESIDUE_MSG_LIMIT))


def rule_markers(cleaned_parsed, rules_cfg, changed):
    """把两类信号折成写进 `changes` 的标记：`rule_silent_<规则>` / `rule_residue_<规则>`。

    只在规则**开着**时记 —— 关掉的规则本来就不该动手，报它等于把用户的配置选择说成故障。

    `rule_silent` 的判据刻意是「**清洗完之后**触发物还在」而不是「清洗前在」：
    前者自动排除了「触发物被另一条规则顺手带走」的假警报（例如计费头与模型宣称挤在
    同一个 system 块里，计费头那条整块摘掉后，宣称也跟着没了 —— 那时没人漏事）。
    """
    fired = {c for c in changed or [] if isinstance(c, str)}

    def _fired(rule):
        return any(c == rule or c.startswith(rule + "_") for c in fired)

    markers = []
    for rule in sorted(present_rules(cleaned_parsed)):
        if rules_cfg.get(rule, False) and not _fired(rule):
            markers.append("rule_silent_" + rule)
    for rule in sorted(residue_rules(cleaned_parsed)):
        if rules_cfg.get(rule, False):
            markers.append("rule_residue_" + rule)
    return markers
