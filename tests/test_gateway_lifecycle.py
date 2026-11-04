#!/usr/bin/env python3
"""网关的退出握手：写 `stop.request`，它自己退。

为什么值得单独一组用例
----------------------
退出这件事在 Windows 上不能走信号（`os.kill` 对非 CTRL 信号一律是 TerminateProcess，
进程没有机会收尾），所以改成了「谁想让我退，就写一个停止标记」。这条路径要是坏了，
症状是「点停止停不下来」或「停止后端口还占着」—— 两个平台都会中招，本机还不一定复现。

跑法：
    python3 -m unittest discover -s claude-tuner/tests -p "test_*.py"
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

import _isolated_home

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY = os.path.join(PLUGIN_ROOT, "scripts", "gateway.py")
STOP_FILE = "stop.request"
RUNTIME_FILE = "gateway.runtime.json"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class GatewayLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = self._tmp.name
        self.port = free_port()
        self.env = _isolated_home.subprocess_env(
            self.home,
            CLAUDE_TUNER_HOME=os.path.join(self.home, ".claude-tuner"),
            CLAUDE_TUNER_PORT=self.port,
        )
        self.proc = None
        self.addCleanup(self._kill)

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)
        if self.proc and self.proc.stderr:
            self.proc.stderr.close()

    def _start(self):
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen([sys.executable, GATEWAY], env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **kwargs)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/v1/models" % self.port, timeout=1)
                return
            except Exception:
                if self.proc.poll() is not None:
                    err = (self.proc.stderr.read() or b"").decode("utf-8", "replace")
                    self.fail("网关启动即退出（%s）：%s" % (self.proc.returncode, err[-800:]))
                time.sleep(0.2)
        self.fail("网关没能在 20 秒内起来")

    def test_stop_marker_makes_it_exit_by_itself(self):
        self._start()
        base = self.env["CLAUDE_TUNER_HOME"]
        with open(os.path.join(base, STOP_FILE), "w", encoding="utf-8") as fh:
            fh.write(str(self.proc.pid))
        code = self.proc.wait(timeout=20)
        self.assertEqual(code, 0, "自己退出的进程应当正常收尾（退出码 0），实际 %s" % code)
        with self.assertRaises(Exception, msg="停止之后端口应该已经关掉了"):
            urllib.request.urlopen("http://127.0.0.1:%d/v1/models" % self.port, timeout=1)

    def test_stop_marker_is_cleared_on_boot(self):
        """上一次残留的停止标记不能让新进程一起来就退（那会变成「永远起不来」）。"""
        base = self.env["CLAUDE_TUNER_HOME"]
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, STOP_FILE), "w", encoding="utf-8") as fh:
            fh.write("99999")
        self._start()          # 能起来就说明残留标记被清掉了
        self.assertIsNone(self.proc.poll())

    def test_runtime_dir_records_what_is_running(self):
        """网关自己不写运行时记录（那是 manager 的活），但它必须能起来并正常服务 ——
        这条用例同时钉住「不写记录也不影响启动」这个前提。"""
        self._start()
        self.assertFalse(os.path.exists(os.path.join(self.env["CLAUDE_TUNER_HOME"], RUNTIME_FILE)),
                         "运行时记录应由 manager 在 spawn 时写，网关不该自己写一份")
    def test_starts_even_when_reverse_dns_hangs(self):
        """反向 DNS 卡住也必须照常启动。

        标准库的 `HTTPServer.server_bind()` 会顺手 `socket.getfqdn(host)`（只为日志印个
        主机名），而它在 bind 之后、`serve_forever()` 之前 —— 解析器一卡，网关就「活着、
        占着端口、一个字节都不回」。这就是公开仓 macOS runner 上那次三平台 CI 变红的全部
        原因：本机有网永远复现不出来，Windows / Ubuntu 的解析器是现成的。

        这里把那一次 `getfqdn` 变成 60 秒，用**真实入口**起一遍：修复前必然超时。
        """
        overlay = os.path.join(self.home, "dns_overlay")
        os.makedirs(overlay, exist_ok=True)
        with open(os.path.join(overlay, "sitecustomize.py"), "w", encoding="utf-8") as fh:
            fh.write("import socket, time\n"
                     "_real = socket.getfqdn\n"
                     "def _hang(*a, **k):\n"
                     "    time.sleep(60)\n"
                     "    return _real(*a, **k)\n"
                     "socket.getfqdn = _hang\n")
        old = self.env.get("PYTHONPATH")
        self.env["PYTHONPATH"] = overlay + (os.pathsep + old if old else "")

        self._start()          # 20 秒内起得来 —— 说明启动路径上没有任何一次解析
        with urllib.request.urlopen("http://127.0.0.1:%d/v1/models" % self.port, timeout=5) as r:
            self.assertEqual(r.status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
