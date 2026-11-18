#!/usr/bin/env python3
"""客户端侧工具拦截（3P 配置的 `disabledBuiltinTools`）的单测。

为什么需要这个文件
------------------
这条链路把「网关要剥什么」翻译成「客户端根本不发什么」。翻译错了有两种后果，都很隐蔽：

1. **写多了**：把配置层根本不管用的规则也写进去（MCP 工具就是），配置看起来比实际能做到的
   多 —— 用户以为省了，其实一个没省。这条边界是实测撞出来的，写在 tool_rules 的注释里，
   这里用断言钉住：**产出里不许出现 mcp__ 开头的名字**。
2. **回收错了**：用户自己在客户端 Setup 窗口里加的条目（例如 `ClaudeDesign`），不能因为我们
   改了配置就被删掉。

另外钉住一条容易写反的判定：工具被挡掉之后，客户端那段**写死的**提示词还留着，
所以配对段落仍然必须剥（见 ParagraphStillStrippedTest）。

跑法：
    python3 -m unittest discover -s claude-tuner/tests -p "test_*.py"
    python3 claude-tuner/tests/test_client_disabled.py
"""
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
    """**所有开关都打开** + 覆盖项；真实默认态（全关）用 `dflt()`。"""
    base = {k: (True if isinstance(v, bool) and k != "enabled" else v)
            for k, v in tr.DEFAULT_TOOL_PRUNING_CONFIG.items()}
    base.update(over)
    return {**base, "custom_strip_tools": over.get("custom_strip_tools", [])}


def dflt(**over):
    """真实默认配置（全关）+ 覆盖项。"""
    base = dict(tr.DEFAULT_TOOL_PRUNING_CONFIG)
    base.update(over)
    return {**base, "custom_strip_tools": over.get("custom_strip_tools", [])}


class ClientDisabledToolsTest(unittest.TestCase):
    """从配置算出「该让客户端根本不发」的工具名。"""

    def test_builtin_tools_come_from_config(self):
        rules = tr.client_disabled_tools(cfg(strip_web_search_tools=True))
        self.assertIn("WebSearch", rules)
        self.assertIn("NotebookEdit", rules)

    def test_includes_mcp_tools_from_registry(self):
        """MCP 工具**也在**名单里 —— 实测 57 条精确名让客户端把 85 个工具压到 28 个。

        来源是 `CATEGORY_KNOWN_TOOLS` 的已登记名字（前缀只负责认人，不负责执行）。
        """
        rules = tr.client_disabled_tools(cfg(strip_pr_tools=True, strip_session_ui_tools=True, strip_web_search_tools=True, strip_terminal_tools=True))
        self.assertIn("mcp__ccd_pr__get_status", rules)
        self.assertIn("mcp__ccd_sidebar__set_view", rules)
        self.assertIn("mcp__ccd_session_mgmt__set_session_title", rules)

    def test_disabled_category_adds_nothing(self):
        """关掉一组不漏出它的工具 —— 别的组按各自的默认值照常出。"""
        rules = tr.client_disabled_tools(cfg(strip_web_search_tools=False,
                                             strip_web_fetch_tools=False,
                                             strip_notebook_tools=False))
        for name in ("WebSearch", "WebFetch", "NotebookEdit"):
            self.assertNotIn(name, rules)

    def test_master_switch_off_keeps_only_the_constant_list(self):
        """总开关关掉 = 一个开关都不生效，但恒定名单照写 —— 它不是「精简」。

        判定侧（`get_stripped_tool_names`）任何配置下都认它，名单侧却一条不写的话，
        就出现「判定说永远该挡、实际谁也没挡」的两种说法。
        """
        self.assertEqual(tr.client_disabled_tools(cfg(enabled=False, strip_web_search_tools=True)),
                         ["WaitForMcpServers"])

    def test_protected_tools_are_never_denied(self):
        """PROTECTED_TOOLS 是底线：就算进了自定义清单也不挡。"""
        rules = tr.client_disabled_tools(cfg(custom_strip_tools=["Bash", "Read", "WebSearch"]))
        self.assertNotIn("Bash", rules)
        self.assertNotIn("Read", rules)

    def test_custom_list_is_included(self):
        """自定义清单里的名字一律进名单，MCP 与非 MCP 没有区别了。"""
        rules = tr.client_disabled_tools(cfg(strip_web_search_tools=False,
                                  custom_strip_tools=["BrandNewTool", "mcp__ccd_unknown__whatever"]))
        self.assertIn("BrandNewTool", rules)
        self.assertIn("mcp__ccd_unknown__whatever", rules)


class MergeClientDisabledTest(unittest.TestCase):
    """把名单并进 3P 配置 —— 只增删自己写过的那些条目。"""

    def test_adds_entries_and_keeps_user_entries(self):
        existing = {"disabledBuiltinTools": ["ClaudeDesign"], "inferenceModels": []}
        new, changed, written = config_ops.merge_client_disabled(existing, ["WebSearch"], [])
        self.assertEqual(changed, ["disabledBuiltinTools"])
        self.assertIn("ClaudeDesign", new["disabledBuiltinTools"])
        self.assertIn("WebSearch", new["disabledBuiltinTools"])
        self.assertEqual(written, ["WebSearch"])
        self.assertEqual(new["inferenceModels"], [])

    def test_removes_only_previously_written(self):
        """关掉某个类别时收回条目，但用户自己在界面里加的那条必须原样留着。"""
        existing = {"disabledBuiltinTools": ["ClaudeDesign", "NotebookEdit", "WebSearch"]}
        new, changed, _ = config_ops.merge_client_disabled(
            existing, ["WebSearch"], previously_written=["NotebookEdit", "WebSearch"])
        self.assertEqual(new["disabledBuiltinTools"], ["ClaudeDesign", "WebSearch"])
        self.assertEqual(changed, ["disabledBuiltinTools"])

    def test_idempotent(self):
        existing = {"disabledBuiltinTools": ["WebSearch"]}
        new, changed, _ = config_ops.merge_client_disabled(existing, ["WebSearch"], ["WebSearch"])
        self.assertEqual(changed, [])
        self.assertEqual(new, existing)

    def test_empty_result_leaves_no_shell(self):
        """名单清空后不留空键 —— 别在客户端的配置里留一个空数组。"""
        existing = {"disabledBuiltinTools": ["WebSearch"]}
        new, changed, _ = config_ops.merge_client_disabled(existing, [], ["WebSearch"])
        self.assertEqual(changed, ["disabledBuiltinTools"])
        self.assertNotIn("disabledBuiltinTools", new)


class SyncClientDisabledTest(unittest.TestCase):
    """配置 → 名单 → 3P 配置的整条路（与网关剥离同一份配置）。"""

    def test_uses_same_config_as_gateway(self):
        pruning = cfg(strip_web_search_tools=True)
        new, changed, written = config_ops.sync_client_disabled({}, pruning, [])
        self.assertEqual(changed, ["disabledBuiltinTools"])
        self.assertEqual(written, tr.client_disabled_tools(pruning))
        self.assertEqual(new["disabledBuiltinTools"], written)

    def test_turning_category_off_withdraws_rule(self):
        pruning = cfg(strip_web_search_tools=False, strip_notebook_tools=False)
        existing = {"disabledBuiltinTools": ["WebSearch", "NotebookEdit"]}
        new, changed, written = config_ops.sync_client_disabled(
            existing, pruning, ["WebSearch", "NotebookEdit"])
        # 关掉之后这两条必须收回；别的组仍按各自的默认值写着，所以名单不空。
        self.assertNotIn("WebSearch", written)
        self.assertNotIn("NotebookEdit", written)
        self.assertNotIn("WebSearch", new.get("disabledBuiltinTools") or [])
        self.assertIn("disabledBuiltinTools", changed)


class ParagraphStillStrippedTest(unittest.TestCase):
    """段落跟**请求实况**走：这一对的工具一个都没发过来，段落就必须走。

    判据从「配置说该剥」改成了「本轮请求里没有」，理由是工具现在由客户端在源头挡掉，
    同一份配置在不同人那里保留的工具不同，只有请求能说明这一面到底发了什么。
    """

    def _parsed(self, tools):
        return {"system": "You are Claude Code.\n\n" + PR_HEAD,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": tools}

    def test_kept_when_client_still_sends_tool(self):
        """工具还在请求里 → 它真的能用 → 段落原样保留（此时删段才会变成悬空指引）。"""
        parsed = self._parsed([{"name": "mcp__ccd_pr__get_status", "description": "x"}])
        changed = gateway.canonicalize(
            parsed, rules=gateway.load_rules_config(),
            tool_pruning_cfg=tr.pruning_cfg_for_surface(cfg(strip_pr_tools=True), "main"))
        self.assertIn(PR_HEAD, parsed["system"])
        self.assertNotIn("strip_ghost_tool_instructions", changed)

    def test_still_stripped_when_client_no_longer_sends_it(self):
        """客户端侧已经挡住（请求里有别的工具、但没有这一对）—— 段落照样得走。"""
        parsed = self._parsed([{"name": "Bash", "description": "x"}])
        changed = gateway.canonicalize(
            parsed, rules=gateway.load_rules_config(),
            tool_pruning_cfg=tr.pruning_cfg_for_surface(cfg(strip_pr_tools=True), "main"))
        self.assertNotIn(PR_HEAD, parsed["system"])
        self.assertIn("strip_ghost_tool_instructions", changed)

    def test_kept_when_category_off(self):
        """开关关掉时段落必须原样保留（此时工具也该留，两边一致）。"""
        parsed = self._parsed([{"name": "mcp__ccd_pr__get_status", "description": "x"}])
        gateway.canonicalize(
            parsed, rules=gateway.load_rules_config(),
            tool_pruning_cfg=tr.pruning_cfg_for_surface(cfg(strip_pr_tools=False), "main"))
        self.assertIn(PR_HEAD, parsed["system"])


if __name__ == "__main__":
    unittest.main()
