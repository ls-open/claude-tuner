#!/usr/bin/env python3
"""hooks.json 的命令必须能在**当前这台机器**上解析并执行。

为什么这是一条测试，不是一段 CI 脚本
------------------------------------
hooks 是插件唯一的启动触发器：命令解析不了，网关就永远不起来，而客户端不会报错，
用户看到的现象是「装了没反应」。这台机器上 `python3` 叫不叫这个名字，只有跑起来才知道
—— 所以把它做成用例，CI 在 macOS / Windows / Linux 上各跑一遍，用户机器上也能跑。

跑法：
    python3 -m unittest discover -s claude-tuner/tests -p "test_*.py"
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PLUGIN_ROOT / "scripts"
HOOKS = PLUGIN_ROOT / "hooks" / "hooks.json"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import bootstrap  # noqa: E402  幂等对齐步骤就在它里面


def hook_commands(path=None):
    """(事件名, 命令串) —— 把 hooks.json 里所有可执行命令摊平。"""
    with open(path or HOOKS, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for event, groups in (data.get("hooks") or {}).items():
        for group in groups or []:
            for hook in group.get("hooks") or []:
                cmd = hook.get("command")
                if isinstance(cmd, str) and cmd.strip():
                    out.append((event, cmd))
    return out


class HooksCommandTest(unittest.TestCase):
    """模板里写的是 `python3`，而机器上叫什么不一定 —— 这正是 `bootstrap.py align`
    存在的理由。所以这里验的是**机制**：对齐之后，每条命令的解释器都能解析、也能执行。
    """

    def _aligned_tree(self, tmp):
        """造一份临时插件树（scripts/ + hooks/），跑一次 align，返回 hooks.json 路径。"""
        root = Path(tmp) / "claude-tuner"
        (root / "hooks").mkdir(parents=True)
        (root / "scripts").mkdir(parents=True)
        shutil.copy2(HOOKS, root / "hooks" / "hooks.json")
        bootstrap.align_runtime(dest=str(root / "scripts"))
        return root / "hooks" / "hooks.json"

    def test_对齐之后每条命令的解释器都能解析(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks = self._aligned_tree(tmp)
            commands = hook_commands(hooks)
            self.assertTrue(commands, "hooks.json 里一条命令都没解析到")
            for event, cmd in commands:
                interpreter = cmd.split(" ", 1)[0].strip("\"'")
                with self.subTest(event=event, interpreter=interpreter):
                    self.assertIsNotNone(
                        shutil.which(interpreter),
                        "`%s` 在本机不存在 —— 这条 hook 会在客户端里静默失败" % interpreter)

    def test_对齐之后每条命令都能被执行(self):
        """真起子进程跑一遍：把会拉起网关的子命令换成只读的 status，只验命令能被解析。"""
        with tempfile.TemporaryDirectory() as tmp:
            hooks = self._aligned_tree(tmp)
            for event, cmd in hook_commands(hooks):
                # 解释器取对齐后的那份，路径指回真实插件根 —— 临时树里没有 manager.py，
                # 这里要验的是「对齐选出的解释器能不能跑我们的入口」，不是文件在不在。
                probe = cmd.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT))
                probe = re.sub(r"\bensure\s+--quiet\b", "status", probe)
                with tempfile.TemporaryDirectory() as home:
                    env = dict(os.environ, CLAUDE_TUNER_HOME=home)
                    with self.subTest(event=event):
                        proc = subprocess.run(probe, shell=True, capture_output=True,
                                              text=True, timeout=120, env=env)
                        self.assertEqual(proc.returncode, 0,
                                         "命令执行失败：%s\n%s" % (probe, (proc.stderr or "")[-400:]))

    def test_仓库里的模板必须是幂等的起点(self):
        """模板本身不保证本机能用（它写的是 python3），但对齐步骤必须是幂等的：
        连续跑两次，第二次必须什么都不改。"""
        with tempfile.TemporaryDirectory() as tmp:
            hooks = self._aligned_tree(tmp)
            first = hooks.read_text(encoding="utf-8")
            bootstrap.align_runtime(dest=str(Path(tmp) / "claude-tuner" / "scripts"))
            self.assertEqual(hooks.read_text(encoding="utf-8"), first, "对齐不是幂等的")


if __name__ == "__main__":
    unittest.main(verbosity=2)
