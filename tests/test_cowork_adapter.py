import unittest
import os
import shutil
import sys
import tempfile

# 引入 gateway 模块
SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# 家目录隔离必须在 import gateway 之前（gateway 在 import 时就定下 BASE_DIR）
import _isolated_home  # noqa: E402,F401

import gateway
import common

class TestCoWorkAdapter(unittest.TestCase):
    def setUp(self):
        # 全部开关打开：这些用例问的是「打开时规则管不管用」，不是默认值
        self.cfg = {k: (True if isinstance(v, bool) and k != "enabled" else v)
                    for k, v in common.DEFAULT_TOOL_PRUNING_CONFIG.items()}
        self.stripped_names = gateway.get_stripped_tool_names(self.cfg)
        # 端到端用例调用 prepare_request()，其内部经 load_tool_pruning_config() 读真实
        # ~/.claude-tuner/config.json —— 用例结果会随开发机的本地开关漂移（改过配置的机器
        # 上必然变红）。把配置源指向不存在的文件，强制回落默认值，保证与机器无关。
        self._saved_cfg_file = gateway.CONFIG_FILE
        self._saved_cache = gateway._config_cache
        # 运行目录指向不存在的路径：抓包（save_session_capture）会往 BASE_DIR 落盘，
        # 不隔开就会写进真实 HOME 或插件目录
        self._saved_base_dir = gateway.BASE_DIR
        self._tmp_home = tempfile.mkdtemp(prefix="claude-tuner-cowork-")
        gateway.BASE_DIR = self._tmp_home
        gateway.CONFIG_FILE = os.path.join(SCRIPTS_DIR, "__absent_config__.json")
        gateway._config_cache = None
        self.addCleanup(self._restore_config)

    def _restore_config(self):
        gateway.CONFIG_FILE = self._saved_cfg_file
        gateway._config_cache = self._saved_cache
        gateway.BASE_DIR = self._saved_base_dir
        shutil.rmtree(self._tmp_home, ignore_errors=True)

    def test_cowork_tools_preserved(self):
        """验证 CoWork 核心协作工具（交付物/文件操作）与终端执行命令工具自然保留"""
        cowork_tools = [
            "mcp__cowork__create_artifact",
            "mcp__cowork__update_artifact",
            "mcp__cowork__list_artifacts",
            "mcp__cowork__present_files",
            "mcp__cowork__allow_cowork_file_delete",
            "mcp__cowork__request_cowork_directory",
            "mcp__cowork__read_widget_context",
            "mcp__cowork__save_skill",
            "mcp__workspace__bash",
        ]
        for t in cowork_tools:
            should_strip = gateway.should_strip_tool(t, self.stripped_names, self.cfg)
            self.assertFalse(should_strip, f"CoWork tool {t} should NOT be stripped")

    def test_dead_mcp_tools_stripped(self):
        """验证无用或死重 MCP 工具被精准命名空间规则剥离"""
        # 只列**默认就会被剥**的命名空间。WebSearch / WebFetch / 工作区抓取 / 终端面板
        # 现在默认保留（见 common.DEFAULT_TOOL_PRUNING_CONFIG），故不在此列。
        dead_tools = [
            "mcp__session_info__list_sessions",
            "mcp__ccd_session_mgmt__detach_session",
            "mcp__ccd_session_mgmt__get_usage",
            "mcp__ccd_settings__get_settings",
        ]
        for t in dead_tools:
            should_strip = gateway.should_strip_tool(t, self.stripped_names, self.cfg)
            self.assertTrue(should_strip, f"Dead tool {t} SHOULD be stripped")

    def test_native_code_tools_preserved(self):
        """验证原生 Code 核心工具保持保留"""
        core_tools = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "Task", "Skill", "AskUserQuestion"]
        for t in core_tools:
            should_strip = gateway.should_strip_tool(t, self.stripped_names, self.cfg)
            self.assertFalse(should_strip, f"Core tool {t} should NOT be stripped")

    def test_end_to_end_code_pruning(self):
        """验证端到端真实 Code 模式工具请求：原生工具毫发无损，桌面冗余精准剥离"""
        code_tools = [
            {"name": "Bash"}, {"name": "Read"}, {"name": "Write"}, {"name": "Edit"},
            {"name": "Glob"}, {"name": "Grep"}, {"name": "Skill"}, {"name": "AskUserQuestion"},
            {"name": "WebSearch"}, {"name": "WebFetch"}, {"name": "NotebookEdit"},
            {"name": "mcp__terminal__read"}, {"name": "mcp__ccd_pr__list"}, {"name": "mcp__ccd_session__list"},
            {"name": "mcp__ccd_directory__list"}, {"name": "mcp__ccd_sidebar__toggle"},
            {"name": "mcp__scheduled-tasks__list_scheduled_tasks"}
        ]
        req = {"tools": list(code_tools), "messages": []}
        changed = gateway.prepare_request(req, session_id="test-code")
        # 精简已经整层搬给客户端（`disabledBuiltinTools`），网关不再从报文里删工具：
        # 「不发」只有客户端一个来源。
        self.assertFalse([c for c in changed if c.startswith("prune_tools_")])
        self.assertEqual(len(req["tools"]), len(code_tools))
        names = {t["name"] for t in req["tools"]}
        self.assertEqual(names, {t["name"] for t in code_tools})

    def test_cowork_surface_skips_pruning(self):
        """CoWork 面整面不剥：工具表原样发出（含商店、技能、搜索）

        传参形态与 `_stabilize` 一致：按面定型后的 cfg 打进来，正是生产的那条路。
        """
        names = ["mcp__plugins__list_plugins", "mcp__skills__suggest_skills", "WebSearch",
                 "mcp__workspace__web_fetch", "mcp__session_info__list_sessions",
                 "mcp__cowork__create_artifact", "Bash"]
        tp = gateway.pruning_cfg_for_surface(dict(common.DEFAULT_TOOL_PRUNING_CONFIG), "cowork")
        req = {"tools": [{"name": n} for n in names], "messages": []}
        changed = gateway.prepare_request(req, session_id="test-cowork", tool_pruning_cfg=tp, kind="cowork")
        self.assertFalse([c for c in changed if c.startswith("prune_tools_")], changed)
        self.assertEqual([t["name"] for t in req["tools"]], names)

    def test_cowork_surface_leaves_prompt_untouched(self):
        """CoWork 面只做模型名转换：计费头、日期、模型宣称一律不洗，原样透传。

        这个面用得少，不值得为它维护一套要跟着客户端跟版的规则；真在那儿失配也只影响那一面。
        与「同一条路」的 Code 面对照见下一条。
        """
        sys_text = ("x-anthropic-billing-header: cc_version=2.1.325.9d3; "
                    "cc_entrypoint=local-agent;\n\n"
                    "# Environment\n - Today's date is 2026-11-16.\n\n"
                    "You are a Claude agent.")
        parsed = {
            "system": [{"type": "text", "text": sys_text}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "tools": [{"name": "mcp__plugins__list_plugins"}, {"name": "Bash"}],
        }
        changed = gateway.stabilize_request(
            parsed, "sid", kind="cowork", rules=dict(common.DEFAULT_RULES),
            tool_pruning_cfg=dict(common.DEFAULT_TOOL_PRUNING_CONFIG))
        self.assertEqual(changed, [])
        self.assertEqual(gateway._system_text(parsed), sys_text, "CoWork 面的提示词必须一字不动")
        self.assertEqual(len(parsed["tools"]), 2)

    def test_cowork_still_splits_mixed_user_message(self):
        """结构修正不跟着一起关：混合 user 消息上游会回 400，这一条是「不做就用不了」。"""
        parsed = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": "x"},
            {"type": "tool_result", "tool_use_id": "t1", "content": "r"},
        ]}]}
        changed = gateway.stabilize_request(
            parsed, "sid", kind="cowork", rules=dict(common.DEFAULT_RULES),
            tool_pruning_cfg=dict(common.DEFAULT_TOOL_PRUNING_CONFIG))
        self.assertIn("split_mixed_user_msg", changed)
        self.assertEqual(len(parsed["messages"]), 2)

    def test_no_surface_prunes_any_more(self):
        """任何面都不再从报文里删工具 —— 精简整层在客户端，面之间不再有差异。"""
        tp = gateway.pruning_cfg_for_surface(dict(common.DEFAULT_TOOL_PRUNING_CONFIG), "main")
        req = {"tools": [{"name": n} for n in ("mcp__ccd_sidebar__list_groups", "Bash")],
               "messages": []}
        changed = gateway.prepare_request(req, session_id="test-code", tool_pruning_cfg=tp, kind="main")
        self.assertFalse([c for c in changed if c.startswith("prune_tools_")])
        self.assertEqual([t["name"] for t in req["tools"]], ["mcp__ccd_sidebar__list_groups", "Bash"])


