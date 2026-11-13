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
        self.cfg = dict(common.DEFAULT_TOOL_PRUNING_CONFIG)
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
            "mcp__session_info__read_transcript",
            "mcp__ccd_session__list",
            "mcp__ccd_settings__list_settings",
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
        # 内置那三个（WebSearch / WebFetch / NotebookEdit）走客户端层，网关不剥 ——
        # 它们不发是因为客户端名单，不是因为这里（见 DECISIONS #58）。
        self.assertIn("prune_tools_4", changed)
        self.assertEqual(len(req["tools"]), 13)
        names = {t["name"] for t in req["tools"]}
        self.assertEqual(names, {
            "Bash", "Read", "Write", "Edit", "Glob", "Grep", "Skill", "AskUserQuestion",
            "WebSearch", "WebFetch", "NotebookEdit", "mcp__terminal__read",
            "mcp__scheduled-tasks__list_scheduled_tasks",
        })

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

    def test_other_surfaces_still_prune(self):
        """按面跳过只认 CoWork：同一份 cfg 在 Code 面照旧剥"""
        tp = gateway.pruning_cfg_for_surface(dict(common.DEFAULT_TOOL_PRUNING_CONFIG), "main")
        req = {"tools": [{"name": n} for n in ("mcp__ccd_sidebar__list_groups", "Bash")],
               "messages": []}
        changed = gateway.prepare_request(req, session_id="test-code", tool_pruning_cfg=tp, kind="main")
        self.assertIn("prune_tools_1", changed)
        self.assertEqual([t["name"] for t in req["tools"]], ["Bash"])


