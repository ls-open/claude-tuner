#!/usr/bin/env python3
"""hooks.json 的命令必须能在**当前这台机器**上解析并执行，且形态必须是 exec form。

为什么这是一条测试，不是一段 CI 脚本
------------------------------------
hooks 是插件唯一的启动触发器：命令解析不了，网关就永远不起来，而客户端不会报错，
用户看到的现象是「装了没反应」。这台机器上 `python3` 叫不叫这个名字，只有跑起来才知道
—— 所以把它做成用例，CI 在 macOS / Windows / Linux 上各跑一遍，用户机器上也能跑。

为什么必须是 exec form
---------------------
客户端支持两种形态（见 Claude Code hooks 文档）：

- **exec form**（带 `args`）：`command` 当可执行文件直接 spawn，**任何平台都没有 shell**，
  `${CLAUDE_PLUGIN_ROOT}` 按普通字符串替换进 `command` 与每个 `args`。
- **shell form**（不带 `args`）：整串交给 shell —— macOS/Linux 是 `sh -c`，
  **Windows 是 Git Bash，没装 Git Bash 就退到 PowerShell**。

而 `"${CLAUDE_PLUGIN_ROOT}/scripts/manager.py"` 在 PowerShell 里是**它自己的变量语法**
（`${变量名}`）：那个 PowerShell 变量没设，展开成空串，命令变成
`python3 "/scripts/manager.py"` —— 静默失败，用户看到的就是「Windows 上装了没反应」。
exec form 之下这个失败模式从构造上不存在，不需要有台 Windows 机器才能保证。

2026-11 实机验证（客户端 4.16336.0，macOS）：exec form 的 `argv` 里 `${CLAUDE_PLUGIN_ROOT}`
被替换成真实路径、替换后的 `manager.py ensure --quiet` 真的执行成功、退出码 0。

跑法：
    python3 -m unittest discover -s claude-tuner/tests -p "test_*.py"
"""
import json
import os
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


def hooks_data(path=None):
    with open(path or HOOKS, "r", encoding="utf-8") as fh:
        return json.load(fh)


def iter_hooks(data):
    """(事件名, hook 字典) —— 把 hooks.json 摊平。"""
    for event, groups in (data.get("hooks") or {}).items():
        for group in groups or []:
            for hook in (group or {}).get("hooks") or []:
                yield event, hook


def interpreter_of(hook):
    """这条 hook 用什么解释器启动。

    exec form 的 `command` 就是可执行文件本身；shell form 的命令串要取第一个词
    （本仓已不用 shell form，这里留着是为了让断言能对错形态给出可读的失败信息）。
    """
    cmd = (hook.get("command") or "").strip()
    if isinstance(hook.get("args"), list):
        return cmd.strip("\"'")
    return cmd.split(" ", 1)[0].strip("\"'")


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

    def test_模板必须是_exec_form(self):
        """形态本身要被钉住：退回 shell form 不会在本机报错，只会在 Windows 上静默死。"""
        for event, hook in iter_hooks(hooks_data()):
            with self.subTest(event=event):
                self.assertIsInstance(hook.get("args"), list,
                                      "这条 hook 没有 args —— 客户端会把它交给 shell，"
                                      "Windows 上（无 Git Bash → PowerShell）静默失败")
                self.assertIn("${CLAUDE_PLUGIN_ROOT}", " ".join(hook["args"]),
                              "exec form 靠占位符定位脚本，别写死绝对路径")

    def test_对齐之后每条命令的解释器都能解析(self):
        with tempfile.TemporaryDirectory() as tmp:
            hooks = self._aligned_tree(tmp)
            seen = 0
            for event, hook in iter_hooks(hooks_data(hooks)):
                interpreter = interpreter_of(hook)
                seen += 1
                with self.subTest(event=event, interpreter=interpreter):
                    self.assertIsNotNone(
                        shutil.which(interpreter),
                        "`%s` 在本机不存在 —— 这条 hook 会在客户端里静默失败" % interpreter)
            self.assertTrue(seen, "hooks.json 里一条命令都没解析到")

    def test_对齐之后每条命令都能被执行(self):
        """真起子进程跑一遍：把会拉起网关的子命令换成只读的 status，只验命令能被执行。

        **按 exec form 的方式跑（不经 shell）**：这正是客户端会走的那条路，
        用 `shell=True` 验等于验了另一条路，Windows 上的问题就藏在这一步里。
        """
        with tempfile.TemporaryDirectory() as tmp:
            hooks = self._aligned_tree(tmp)
            for event, hook in iter_hooks(hooks_data(hooks)):
                argv = [interpreter_of(hook)] + [
                    a.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT))
                    for a in hook["args"]]
                argv = ["status" if a == "ensure" else a for a in argv]
                argv = [a for a in argv if a != "--quiet"] + ["--quiet"]
                with tempfile.TemporaryDirectory() as home:
                    env = dict(os.environ, CLAUDE_TUNER_HOME=home)
                    with self.subTest(event=event):
                        proc = subprocess.run(argv, capture_output=True, text=True,
                                              timeout=120, env=env)
                        self.assertEqual(proc.returncode, 0,
                                         "命令执行失败：%s\n%s"
                                         % (" ".join(argv), (proc.stderr or "")[-400:]))

    def test_对齐能改写_exec_form里的解释器名(self):
        """Windows 上最常见的那种局面：机器上叫 `python`，模板里写的是 `python3`。

        这条用例把 `_pick_interpreter` 钉成 `python` 来复现它 —— 不加这条，
        「align 能处理 exec form」就只在 macOS 上验过，而 macOS 本就不需要改写，
        等于什么都没验。（本仓吃过好几次「本机永远绿」的亏。）
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "claude-tuner"
            (root / "hooks").mkdir(parents=True)
            (root / "scripts").mkdir(parents=True)
            shutil.copy2(HOOKS, root / "hooks" / "hooks.json")
            saved = bootstrap._pick_interpreter
            bootstrap._pick_interpreter = lambda: "python"
            try:
                result = bootstrap.align_runtime(dest=str(root / "scripts"))
            finally:
                bootstrap._pick_interpreter = saved
            self.assertTrue(result.get("changed"), "align 没有改写 exec form 的 command：%s" % result)
            data = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
            for event, hook in iter_hooks(data):
                with self.subTest(event=event):
                    self.assertEqual(hook["command"], "python")

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
