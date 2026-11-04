#!/usr/bin/env python3
"""规则哑火：触发物还在、规则却没动手时，日志里必须看得出来。

为什么值得一组专门的用例
------------------------
`rule_failed` 只兜异常，`changed` 只记「动了什么」—— 于是「该动却没动」是这套体系里
唯一没有信号的失败形态。而它会让 `sys_hash` 那条判据说反话：跨天前缀变了，看着像
客户端或上游的事，实际是我们的规则失效了。

判据不是「这条规则 0 次触发」（`strip_git_status` 只在首轮有活干，按次数报会天天
误报），而是两个面：
- `rule_silent_<规则>`：触发物在报文里，规则却什么都没动 —— 规则实现坏了 / 只改了一半；
- `rule_residue_<规则>`：清洗之后易变痕还在 —— 客户端把锚点本身改了措辞。

跑法：
    python3 -m unittest discover -s claude-tuner/tests -p "test_*.py"
    python3 claude-tuner/tests/test_rule_silence.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _isolated_home  # noqa: E402,F401  必须在 import gateway 之前：它改 CLAUDE_TUNER_HOME

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import gateway  # noqa: E402
import volatile_markers as vm  # noqa: E402


def payload(system_text, reminder=None):
    """一份最小报文：注入面放 system 文本，正文面按需塞一个注入容器。"""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    if reminder:
        msgs.insert(0, {"role": "user", "content": [{"type": "text", "text": reminder}]})
    return {"system": [{"type": "text", "text": system_text}], "messages": msgs}


def markers(changed):
    return [c for c in changed if c.startswith(("rule_silent_", "rule_residue_"))]


class TriggerTableTest(unittest.TestCase):
    """判据表本身：每个文本规则都得有触发物，且不能宽到误伤。"""

    def test_每个文本规则都有触发物(self):
        text_rules = set(gateway.DEFAULT_RULES) - {"probe_interception",
                                                   "split_mixed_user_messages",
                                                   "tool_block_ordering"}
        missing = sorted(text_rules - set(vm.RULE_TRIGGERS))
        self.assertEqual(missing, [], "这些规则没有触发物判据，哑火时无人报警")

    def test_触发物只认注入面_不误伤正文(self):
        """正文里的裸日期不算触发物 —— 用户正文天天有日期，收裸串会满屏假报。"""
        body = {"system": [{"type": "text", "text": "no anchors here"}],
                "messages": [{"role": "user", "content": "我昨天 2026-11-04 试过，日期没问题"}]}
        self.assertEqual(vm.present_rules(body), set())

    def test_容器里的注入仍会被认出来(self):
        body = payload("no anchors here",
                       reminder="<system-reminder>\nToday's date is 2026-11-04.\n</system-reminder>")
        self.assertIn("strip_date", vm.present_rules(body))


class SilentTest(unittest.TestCase):
    """触发物在 → 规则必须动手，否则记 rule_silent。"""

    def test_正常剥干净不留标记(self):
        parsed = payload("x-anthropic-billing-header: cc_version=1\n正文")
        changed = gateway.canonicalize(parsed, rules=dict(gateway.DEFAULT_RULES))
        self.assertIn("strip_billing_header", changed)
        self.assertEqual(markers(changed), [])

    def test_规则实现偷懒时记为哑火(self):
        """锚点在、规则却原样放行 —— 这就是「实现坏了」，必须报。"""
        parsed = payload("You are powered by the model named Claude Opus.\n正文")
        saved = gateway.strip_model_declaration_text
        gateway.strip_model_declaration_text = lambda text: (text, False)
        try:
            changed = gateway.canonicalize(parsed, rules=dict(gateway.DEFAULT_RULES))
        finally:
            gateway.strip_model_declaration_text = saved
        self.assertIn("rule_silent_strip_model_declarations", changed)

    def test_规则被关掉时不报(self):
        """关掉是用户的配置选择，不是故障 —— 报它等于把选择说成 bug。"""
        parsed = payload("You are powered by the model named Claude Opus.\n正文")
        saved = gateway.strip_model_declaration_text
        gateway.strip_model_declaration_text = lambda text: (text, False)
        try:
            changed = gateway.canonicalize(
                parsed, rules=dict(gateway.DEFAULT_RULES, strip_model_declarations=False))
        finally:
            gateway.strip_model_declaration_text = saved
        self.assertEqual(markers(changed), [])


class ResidueTest(unittest.TestCase):
    """锚点被改写时第一类信号是瞎的 —— 由「洗完还有残留」补上。"""

    def test_客户端改写日期措辞后靠残留报出来(self):
        parsed = payload("# Environment update\n\nCurrent date: 2026-11-04.\n")
        changed = gateway.canonicalize(parsed, rules=dict(gateway.DEFAULT_RULES))
        # 规则确实没认出来（锚点变了），但它留下的日期痕必须被看到
        self.assertNotIn("strip_date", changed)
        self.assertIn("rule_residue_strip_date", changed)

    def test_正常报文不该有残留标记(self):
        """真实形态的注入：剥干净之后一条残留都不许有（假警报比不报警更糟）。"""
        parsed = payload(
            "x-anthropic-billing-header: cc_version=1\n"
            "You are powered by the model named Claude.\n"
            "<system-reminder>\nToday's date is 2026-11-04.\n# gitStatus\nOn branch main\n"
            "</system-reminder>\n正文")
        changed = gateway.canonicalize(parsed, rules=dict(gateway.DEFAULT_RULES))
        self.assertEqual(markers(changed), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
