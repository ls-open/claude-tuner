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
HOOKS = PLUGIN_ROOT / "hooks" / "hooks.json"


def hook_commands():
    """(事件名, 命令串) —— 把 hooks.json 里所有可执行命令摊平。"""
    with open(HOOKS, "r", encoding="utf-8") as fh:
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
    def test_每条命令的解释器在本机可解析(self):
        commands = hook_commands()
        self.assertTrue(commands, "hooks.json 里一条命令都没解析到")
        for event, cmd in commands:
            interpreter = cmd.split(" ", 1)[0].strip("\"'")
            with self.subTest(event=event, interpreter=interpreter):
                self.assertIsNotNone(
                    shutil.which(interpreter),
                    "`%s` 在本机不存在 —— 这条 hook 会在客户端里静默失败" % interpreter)

    def test_每条命令都能被执行(self):
        """真起子进程跑一遍。会把会拉起网关的子命令换成只读的 status，
        验的是「命令能被这台机器解析」，不制造副作用。"""
        for event, cmd in hook_commands():
            probe = cmd.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT))
            probe = re.sub(r"\bensure\s+--quiet\b", "status", probe)
            with tempfile.TemporaryDirectory() as home:
                env = dict(os.environ, CLAUDE_TUNER_HOME=home)
                with self.subTest(event=event):
                    proc = subprocess.run(probe, shell=True, capture_output=True, text=True,
                                          timeout=120, env=env)
                    self.assertEqual(proc.returncode, 0,
                                     "命令执行失败：%s\n%s" % (probe, (proc.stderr or "")[-400:]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
