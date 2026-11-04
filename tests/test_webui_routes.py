#!/usr/bin/env python3
"""配置台的端到端冒烟：在子进程里真起网关，用真 HTTP 请求打它。

为什么需要这个文件
------------------
配置台挂在网关自己的端口上，和 `/v1/messages` 是同一个 socket。这里要钉住两件事：

1. **门禁真的拦得住** —— 页面手里有用户的 API Key，而 127.0.0.1 上任何本地程序、
   任何网页都能发请求过来。没 token、Host 是外域名、Origin 跨源，都必须被拒。
2. **没有把原来的路由挡掉** —— `/v1/models` 与 `/v1/messages` 是客户端的生命线，
   配置台接进 `do_GET` / `do_POST` 之后必须仍然原样工作。

**必须子进程真跑**（本仓铁律：命令行入口必须在子进程验）：路由在 `Handler` 的方法里，import 后调函数
够不到 `do_GET` / `do_POST` 的分派。HOME / CLAUDE_TUNER_3P_DIR / CLAUDE_TUNER_HOME 全部指向
临时目录，端口用随机空闲口，不碰用户真实的网关与配置。

跑法：
    python3 -m unittest discover -s claude-tuner/tests -p "test_*.py"
    python3 tools/test_webui_routes.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PLUGIN_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import config_ops  # noqa: E402  路径口径与生产同一份（platform_paths）
GATEWAY = SCRIPTS / "gateway.py"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随跳转：要断言 302 与 Set-Cookie，就得看见这一跳本身。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WebUITestCase(unittest.TestCase):
    """一个测试起一个真网关进程，用完杀干净。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls._tmp.name)
        # 路径由生产口径算（见 config_ops.platform_paths），不写死 macOS 布局
        cls._env = {"HOME": str(cls.home), "USERPROFILE": str(cls.home),
                    "APPDATA": str(cls.home / "AppData" / "Roaming"),
                    "XDG_CONFIG_HOME": str(cls.home / ".config")}
        with mock.patch.dict(os.environ, cls._env):
            _paths = config_ops.platform_paths()
        cls.config_lib = Path(_paths["config_library"])
        cls.config_lib.mkdir(parents=True)
        cls.bridge_home = cls.home / ".claude-tuner"
        cls.bridge_home.mkdir(parents=True)
        cls.port = free_port()

        # 一份最小的 3P 配置：模型映射与 Key 都有，好让读接口有东西可脱敏
        (cls.config_lib / "p.json").write_text(json.dumps({
            "inferenceProvider": "gateway",
            "inferenceCredentialKind": "static",
            "inferenceGatewayBaseUrl": "http://127.0.0.1:%d" % cls.port,
            "inferenceGatewayApiKey": "sk-super-secret-key-123456",
            "inferenceModels": [{"name": "claude-opus-5-5", "labelOverride": "deepseek-flash"}],
        }), encoding="utf-8")
        (cls.config_lib / "_meta.json").write_text(json.dumps({"appliedId": "p"}), encoding="utf-8")

        cls.token = "t" * 64
        (cls.bridge_home / "webui.token").write_text(cls.token, encoding="utf-8")

        env = dict(os.environ)
        env.update({"HOME": str(cls.home), "USERPROFILE": str(cls.home),
                    "APPDATA": str(cls.home / "AppData" / "Roaming"),
                    "XDG_CONFIG_HOME": str(cls.home / ".config"),
                    "CLAUDE_TUNER_3P_DIR": str(cls.config_lib),
                    "CLAUDE_TUNER_HOME": str(cls.bridge_home), "CLAUDE_TUNER_PORT": str(cls.port)})
        # 把网关的输出留下来：起不来时它是唯一的线索（曾经在 CI 上只看到
        # 「15 秒内没起来」而没有任何原因，只能靠猜）。
        cls._log_path = cls.bridge_home / "gateway.test.log"
        cls._log = open(cls._log_path, "ab")
        cls.addClassCleanup(cls._log.close)
        kwargs = {}
        if os.name == "nt":
            # POSIX 的 start_new_session 在 Windows 上会被忽略；那边用 creationflags
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        cls.proc = subprocess.Popen([sys.executable, str(GATEWAY)], env=env,
                                    stdout=cls._log, stderr=subprocess.STDOUT, **kwargs)
        cls._wait_ready()

    @classmethod
    def _wait_ready(cls):
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/v1/models" % cls.port, timeout=1):
                    return
            except Exception:
                time.sleep(0.2)
        tail = ""
        try:
            tail = cls._log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        except OSError:
            pass
        if not tail.strip():
            # 一个字节都没写 —— 说明它没跑到打印那一步。再起一份把输出接进管道，
            # 等 10 秒后收走它说的话：这样 CI 上也能看到真实原因，而不是「（无输出）」。
            try:
                probe = subprocess.Popen([sys.executable, str(GATEWAY)], env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True)
                try:
                    out, _ = probe.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    probe.kill()
                    out, _ = probe.communicate()
                tail = "（重跑一次的完整输出，退出码 %s）\n%s" % (probe.returncode, (out or "")[-1500:])
            except Exception as exc:
                tail = "（重跑也失败：%s）" % exc
        raise RuntimeError("网关没能在 15 秒内起来。网关输出：\n%s" % (tail or "（无输出）"))

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls._tmp.cleanup()

    # ── 请求帮手 ──

    def request(self, path, method="GET", body=None, headers=None, token=True, follow=True):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        hdrs = dict(headers or {})
        if token and "X-Claude-Tuner-Token" not in hdrs:
            hdrs["X-Claude-Tuner-Token"] = self.token
        data = json.dumps(body).encode() if body is not None else None
        if data:
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        # 默认 opner 会自己跟随 302 且不带 cookie，那样断言到的就是跳转后的结果了
        opener = urllib.request.build_opener(_NoRedirect if not follow
                                             else urllib.request.HTTPRedirectHandler)
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace"), dict(exc.headers)


class TestGate(WebUITestCase):
    def test_page_needs_token(self):
        status, _, _ = self.request("/", token=False)
        self.assertEqual(status, 401)

    def test_page_renders_with_token(self):
        status, body, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("claude-tuner 配置台", body)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))

    def cookie_from_redirect(self):
        """拿一次跳转返回的 Set-Cookie，原样取出「名字=值」。

        刻意**不写死名字** —— 从响应里取。写死过一次，改名之后测试就红了，
        而红的原因是「名字变了」而不是「行为坏了」，那对排查毫无帮助。
        """
        status, _, headers = self.request("/?token=%s" % self.token, token=False, follow=False)
        self.assertEqual(status, 302)
        cookie = headers.get("Set-Cookie", "").split(";")[0].strip()
        self.assertIn(self.token, cookie, "cookie 里应当带上 token")
        return cookie

    def test_token_in_query_redirects_and_sets_cookie(self):
        status, _, headers = self.request("/?token=%s" % self.token, token=False, follow=False)
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/")
        set_cookie = headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", set_cookie)
        # 带 Max-Age 才是「长期」cookie。不带就是个会话级的，浏览器一关就没了 ——
        # 用户会觉得「为什么不能直接打开」，每次都得重新去找那条带 token 的链接。
        self.assertIn("Max-Age=", set_cookie)
        self.assertNotIn(self.token, headers.get("Location", ""))

    def test_cookie_from_that_redirect_opens_the_page(self):
        """跳转后浏览器带着 cookie 再请求 —— 这条路必须真的通，
        否则用户点一次链接之后看到的还是 401。"""
        cookie = self.cookie_from_redirect()
        status, body, _ = self.request("/", token=False, headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("配置台", body)

    def test_wrong_token_is_rejected(self):
        status, _, _ = self.request("/api/config", headers={"X-Claude-Tuner-Token": "wrong"})
        self.assertEqual(status, 403)

    def test_api_needs_token(self):
        status, _, _ = self.request("/api/config", token=False)
        self.assertEqual(status, 403)

    def test_foreign_host_is_rejected(self):
        """Host 校验挡的是 DNS rebinding：恶意域名解析到 127.0.0.1 后，
        浏览器会带上它自己的 Host 来打这个端口。"""
        status, _, _ = self.request("/api/config", headers={"Host": "evil.example.com"})
        self.assertEqual(status, 421)

    def test_cross_origin_write_is_rejected(self):
        status, _, _ = self.request("/api/config", method="POST", body={},
                                    headers={"Origin": "http://evil.example.com"})
        self.assertEqual(status, 403)

    def test_localhost_host_is_accepted(self):
        status, _, _ = self.request("/api/config", headers={"Host": "localhost:%d" % self.port})
        self.assertEqual(status, 200)


class TestReadConfig(WebUITestCase):
    def test_api_key_is_masked(self):
        status, body, _ = self.request("/api/config")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertNotIn("sk-super-secret-key-123456", body)
        self.assertEqual(payload["data"]["upstream"]["api_key_masked"][:4], "sk-s")
        self.assertTrue(payload["data"]["upstream"]["api_key_set"])

    def test_reports_models_and_options(self):
        payload = json.loads(self.request("/api/config")[1])["data"]
        self.assertEqual(payload["models"][0]["name"], "claude-opus-5-5")
        self.assertEqual(payload["models"][0]["label"], "deepseek-flash")
        self.assertIn("claude-opus-5-5", payload["options"]["client_models"])
        # 工具面的主指标由后端算（读日志），没有请求时如实为 None —— 键必须在，
        # 否则页面那条 chip 会永远静默退回数开关。
        self.assertIn("tool_stats", payload)
        self.assertTrue(payload["three_p"]["activated"])

    def test_never_leaks_token(self):
        self.assertNotIn(self.token, self.request("/api/config")[1])


class TestWriteConfig(WebUITestCase):
    def test_upstream_and_models_are_saved(self):
        status, body, _ = self.request("/api/config", method="POST", body={
            "upstream_url": "https://api.deepseek.com/anthropic",
            "models": [{"name": "claude-opus-5-5", "label": "deepseek-v4-pro"}],
        }, headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200, body)
        data = json.loads(body)["data"]
        self.assertTrue(any("上游地址" in n for n in data["notes"]))
        # 上游是网关侧改的 -> 下次请求自动读到；模型映射 3P 侧改的 -> 要重启客户端
        self.assertTrue(data["effects"]["restart_client"])

        bridge = json.loads((self.bridge_home / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(bridge["upstream_url"], "https://api.deepseek.com/anthropic/v1/messages")
        three_p = json.loads((self.config_lib / "p.json").read_text(encoding="utf-8"))
        self.assertEqual(three_p["inferenceModels"][0]["labelOverride"], "deepseek-v4-pro")

    def test_models_deletion_and_clear_are_saved(self):
        # 验证删除模型列表（清空为 []）也能正确保存落盘
        status, body, _ = self.request("/api/config", method="POST", body={
            "models": [],
        }, headers={"Origin": "http://127.0.0.1:%d" % self.port})
        self.assertEqual(status, 200, body)
        three_p = json.loads((self.config_lib / "p.json").read_text(encoding="utf-8"))
        self.assertEqual(three_p["inferenceModels"], [])

    def test_masked_key_means_unchanged(self):
        masked = json.loads(self.request("/api/config")[1])["data"]["upstream"]["api_key_masked"]
        self.request("/api/config", method="POST", body={"api_key": masked})
        three_p = json.loads((self.config_lib / "p.json").read_text(encoding="utf-8"))
        self.assertEqual(three_p["inferenceGatewayApiKey"], "sk-super-secret-key-123456")

    def test_界面标签页开关已不再由我们写(self):
        """标签页（Chat / Cowork / Code）不再归我们管。

        客户端 v2.31226.0 起有 `desktopHome`（Unified Claude）：一旦它生效，
        `chatTabEnabled` / `coworkTabEnabled` 就完全不起作用 —— 页面上留着两个
        勾了没反应的开关，比没有更糟。所以整组撤掉，写入路径也不再碰这些键。
        """
        before = json.loads((self.config_lib / "p.json").read_text(encoding="utf-8"))
        self.request("/api/config", method="POST", body={"surfaces": {
            "chatTabEnabled": False, "coworkTabEnabled": False,
            "isClaudeCodeForDesktopEnabled": False}})
        after = json.loads((self.config_lib / "p.json").read_text(encoding="utf-8"))
        for key in ("chatTabEnabled", "coworkTabEnabled", "isClaudeCodeForDesktopEnabled"):
            self.assertEqual(before.get(key), after.get(key), key)

    def test_unknown_rule_keys_are_ignored(self):
        self.request("/api/config", method="POST",
                     body={"rules": {"strip_date": False, "not_a_rule": True}})
        bridge = json.loads((self.bridge_home / "config.json").read_text(encoding="utf-8"))
        self.assertIs(bridge["rules"]["strip_date"], False)
        self.assertNotIn("not_a_rule", bridge["rules"])

    def test_随附技能开关两个键各写各的文件(self):
        """一个开关、两个键，而且**两个键住的地方不一样** —— 这正是上次读错的地方。

        `disableBundledSkills` 是 3P 配置的键（客户端自己的设置页也写那儿），
        `enableWorkflows` 是 Claude Code 那层的设置。以前两个都往
        ~/.claude/settings.json 写，于是用户在客户端关掉之后，页面还显示「没关」——
        两边的读写根本不在同一个文件上。
        """
        (self.home / ".claude").mkdir(exist_ok=True)
        status, body, _ = self.request("/api/config", method="POST", body={
            "bundled_skills": True,
            "settings": {"enableWorkflows": False},
        })
        self.assertEqual(status, 200, body)

        three_p = json.loads((self.config_lib / "p.json").read_text(encoding="utf-8"))
        self.assertIs(three_p["disableBundledSkills"], True)
        settings = json.loads((self.home / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertIs(settings["enableWorkflows"], False)
        self.assertNotIn("disableBundledSkills", settings, "这个键不该再写进用户设置")

        data = json.loads(self.request("/api/config")[1])["data"]
        self.assertTrue(data["client"]["bundled_skills"], "读回来要认得这个状态")


class TestOriginalRoutesStillWork(WebUITestCase):
    """配置台接进同一个 Handler 之后，客户端的路不能被挡。"""

    def test_models_endpoint(self):
        status, body, _ = self.request("/v1/models", token=False)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"][0]["id"], "claude-opus-5-5")

    def test_messages_endpoint_still_routes_to_upstream(self):
        # 上游是假的：只断言它没被配置台拦下（会走到转发并因连不上上游而失败），
        # 换句话说不是 404 not found
        status, body, _ = self.request("/v1/messages", method="POST", token=False, body={
            "model": "claude-opus-5-5", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertNotEqual(status, 404, body)

    def test_unknown_path_is_404(self):
        status, _, _ = self.request("/nope", token=False)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
