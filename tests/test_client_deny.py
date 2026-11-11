#!/usr/bin/env python3
"""客户端侧工具拦截（`permissions.deny`）的单测。

为什么需要这个文件
------------------
这条链路把「网关要剥什么」翻译成「客户端根本不发什么」。翻译错了有两种后果，都很隐蔽：

1. **写多了**：把设置层根本不管用的规则也写进去（MCP 工具就是），配置看起来比实际能做到的
   多 —— 用户以为省了，其实一个没省。这条边界是实测撞出来的，写在 tool_rules 的注释里，
   这里用断言钉住：**产出里不许出现 mcp__ 开头的名字**。
2. **回收错了**：用户自己在 `/permissions` 里加的规则，不能因为我们改了配置就被删掉。

另外钉住一条容易写反的判定：工具被挡掉之后，客户端那段**写死的**提示词还留着，
所以配对段落仍然必须剥（见 ParagraphStillStrippedTest）。

跑法：
    python3 -m unittest discover -s claude-tuner/tests -p "test_*.py"
    python3 tests/test_client_deny.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config_ops  # noqa: E402
import gateway  # noqa: E402
import tool_rules as tr  # noqa: E402

# 运行目录指向临时目录（模块级，覆盖整个文件）：canonicalize 里的告警会落盘，
# 不隔开就会写进真实 HOME（铁律：测试不碰真实 HOME）
_ISOLATED_HOME = tempfile.mkdtemp(prefix="claude-tuner-test-")
gateway.BASE_DIR = _ISOLATED_HOME
gateway.CONFIG_FILE = os.path.join(_ISOLATED_HOME, "config.json")
import atexit  # noqa: E402

atexit.register(lambda: __import__("shutil").rmtree(_ISOLATED_HOME, ignore_errors=True))

PR_HEAD = ("After opening a PR, use the ccd_pr tools: call `get_status` and, if it does not "
           "report that PR, bind it with `bind_pr`; then read its CI and offer Auto-fix.")


def cfg(**over):
    """默认配置 + 覆盖项（与生产同一个默认表）。"""
    base = dict(tr.DEFAULT_TOOL_PRUNING_CONFIG)
    base.update(over)
    return {**base, "custom_strip_tools": over.get("custom_strip_tools", [])}


class DenyRulesTest(unittest.TestCase):
    """从配置算出「该让客户端根本不发」的工具名。"""

    def test_builtin_tools_come_from_config(self):
        rules = tr.deny_rules(cfg(strip_web_search_tools=True, strip_notebook_tools=True))
        self.assertIn("WebSearch", rules)
        self.assertIn("NotebookEdit", rules)

    def test_never_returns_mcp_tools(self):
        """MCP 工具一律不产出：设置层对桌面端的 hosted MCP 无效（实测两次，见 tool_rules 注释）。

        写进去不会生效，只会让配置看起来比实际能做到的多 —— 比不写更坏。
        """
        rules = tr.deny_rules(cfg(strip_pr_ci_tools=True, strip_session_tools=True,
                                  strip_web_fetch_tools=True, strip_terminal_tools=True))
        self.assertTrue(rules)
        self.assertEqual([r for r in rules if r.startswith("mcp__")], [])

    def test_disabled_category_adds_nothing(self):
        rules = tr.deny_rules(cfg(strip_web_search_tools=False, strip_web_fetch_tools=False,
                                  strip_notebook_tools=False))
        self.assertEqual(rules, [])

    def test_master_switch_off_disables_everything(self):
        self.assertEqual(tr.deny_rules(cfg(enabled=False, strip_web_search_tools=True)), [])

    def test_protected_tools_are_never_denied(self):
        """PROTECTED_TOOLS 是底线：就算进了自定义清单也不挡。"""
        rules = tr.deny_rules(cfg(custom_strip_tools=["Bash", "Read", "WebSearch"]))
        self.assertNotIn("Bash", rules)
        self.assertNotIn("Read", rules)

    def test_custom_list_takes_non_mcp_only(self):
        """自定义清单里的非 MCP 工具会挡，MCP 工具照样排除（同一条边界）。"""
        rules = tr.deny_rules(cfg(strip_web_search_tools=False,
                                  custom_strip_tools=["BrandNewTool", "mcp__ccd_pr__get_status"]))
        self.assertIn("BrandNewTool", rules)
        self.assertNotIn("mcp__ccd_pr__get_status", rules)


class MergeDenyRulesTest(unittest.TestCase):
    """把规则并进 settings —— 只增删自己写过的那几条。"""

    def test_adds_rules_and_keeps_user_entries(self):
        existing = {"permissions": {"deny": ["Bash(rm -rf *)"]}, "other": 1}
        new, changed, written = config_ops.merge_deny_rules(existing, ["WebSearch"], [])
        self.assertEqual(changed, ["permissions.deny"])
        self.assertIn("Bash(rm -rf *)", new["permissions"]["deny"])
        self.assertIn("WebSearch", new["permissions"]["deny"])
        self.assertEqual(written, ["WebSearch"])
        self.assertEqual(new["other"], 1)

    def test_removes_only_previously_written(self):
        """关掉某个类别时收回规则，但用户自己写的那条必须原样留着。"""
        existing = {"permissions": {"deny": ["Bash(rm -rf *)", "mcp__ccd_pr", "WebSearch"]}}
        new, changed, _ = config_ops.merge_deny_rules(
            existing, ["WebSearch"], previously_written=["mcp__ccd_pr", "WebSearch"])
        self.assertEqual(new["permissions"]["deny"], ["Bash(rm -rf *)", "WebSearch"])
        self.assertEqual(changed, ["permissions.deny"])

    def test_idempotent(self):
        existing = {"permissions": {"deny": ["WebSearch"]}}
        new, changed, _ = config_ops.merge_deny_rules(existing, ["WebSearch"], ["WebSearch"])
        self.assertEqual(changed, [])
        self.assertEqual(new, existing)

    def test_empty_result_leaves_no_shell(self):
        """规则清空后不留空壳 —— 空数组会被客户端当成"用户设置过权限"展示。"""
        existing = {"permissions": {"deny": ["WebSearch"]}}
        new, changed, _ = config_ops.merge_deny_rules(existing, [], ["WebSearch"])
        self.assertEqual(changed, ["permissions.deny"])
        self.assertNotIn("permissions", new)

    def test_object_entries_are_left_alone(self):
        obj = {"toolName": "Bash", "ruleContent": "rm -rf *"}
        existing = {"permissions": {"deny": [obj]}}
        new, _, _ = config_ops.merge_deny_rules(existing, ["WebSearch"], [])
        self.assertIn(obj, new["permissions"]["deny"])


class SyncClientDenyTest(unittest.TestCase):
    """配置 → 规则 → settings 的整条路（与网关剥离同一份配置）。"""

    def test_uses_same_config_as_gateway(self):
        pruning = cfg(strip_web_search_tools=True, strip_web_fetch_tools=True,
                      strip_notebook_tools=True)
        new, changed, written = config_ops.sync_client_deny({}, pruning, [])
        self.assertEqual(changed, ["permissions.deny"])
        self.assertEqual(written, tr.deny_rules(pruning))
        self.assertEqual(new["permissions"]["deny"], written)

    def test_turning_category_off_withdraws_rule(self):
        pruning = cfg(strip_web_search_tools=False, strip_web_fetch_tools=False,
                      strip_notebook_tools=False)
        existing = {"permissions": {"deny": ["WebSearch", "NotebookEdit"]}}
        new, changed, written = config_ops.sync_client_deny(
            existing, pruning, ["WebSearch", "NotebookEdit"])
        self.assertEqual(written, [])
        self.assertEqual(changed, ["permissions.deny"])
        self.assertNotIn("permissions", new)


class ParagraphStillStrippedTest(unittest.TestCase):
    """客户端不再发工具时，桌面端那几段写死的提示词**仍然**要剥。

    这段保护的正是「用户在客户端界面里把某个 MCP 工具关掉」那条路：段落是客户端无条件
    拼接的字符串常量，跟工具在不在无关。少了这条，「工具挡了、段落留着」就是一句指向
    不存在工具的悬空指引 —— 比不剥更糟。
    """

    def _parsed(self, tools):
        return {"system": "You are Claude Code.\n\n" + PR_HEAD,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": tools}

    def test_stripped_when_client_still_sends_tool(self):
        parsed = self._parsed([{"name": "mcp__ccd_pr__get_status", "description": "x"}])
        changed = gateway.canonicalize(
            parsed, rules=gateway.load_rules_config(),
            tool_pruning_cfg=tr.pruning_cfg_for_surface(cfg(strip_pr_ci_tools=True), "main"))
        self.assertNotIn(PR_HEAD, parsed["system"])
        self.assertIn("strip_ghost_tool_instructions", changed)

    def test_still_stripped_when_client_no_longer_sends_it(self):
        """客户端侧已经挡住（请求里没有这个工具）—— 段落照样得走。"""
        parsed = self._parsed([])
        changed = gateway.canonicalize(
            parsed, rules=gateway.load_rules_config(),
            tool_pruning_cfg=tr.pruning_cfg_for_surface(cfg(strip_pr_ci_tools=True), "main"))
        self.assertNotIn(PR_HEAD, parsed["system"])
        self.assertIn("strip_ghost_tool_instructions", changed)

    def test_kept_when_category_off(self):
        """开关关掉时段落必须原样保留（此时工具也该留，两边一致）。"""
        parsed = self._parsed([{"name": "mcp__ccd_pr__get_status", "description": "x"}])
        gateway.canonicalize(
            parsed, rules=gateway.load_rules_config(),
            tool_pruning_cfg=tr.pruning_cfg_for_surface(cfg(strip_pr_ci_tools=False), "main"))
        self.assertIn(PR_HEAD, parsed["system"])


if __name__ == "__main__":
    unittest.main()
