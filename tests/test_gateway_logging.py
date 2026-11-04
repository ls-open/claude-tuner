#!/usr/bin/env python3
"""网关结构化日志的几条不变量（`claude-tuner/scripts/gateway.py`）。

- `request_id` 只能由 `log_event` 注入，调用点不得自己写这个键（写一个空值就会把它顶掉）。
- 上游失败事件必须带 `headers_sent`：响应头一旦发出就无法重发，只有它为 false 的失败
  还有重试余地，而这个标志不落盘就区分不出这两类失败。
- 每轮都要留下 system / tools 的指纹，见 `TestPrefixFingerprint`。

这两条都是踩过坑才定下来的；这里只留规则与断言。

运行：python3 tools/test_gateway_logging.py
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_PY = PLUGIN_ROOT / "scripts" / "gateway.py"

# 上游失败事件：这些都要能回答「当时响应头发出去了吗」
UPSTREAM_FAILURE_EVENTS = [
    "upstream_error",
    "upstream_stream_error",
    "upstream_http_error",
    "upstream_truncated",
]


# 家目录隔离必须在首次加载 gateway/webui 之前（模块导入时就定下 BASE_DIR）
import _isolated_home  # noqa: E402,F401

def load_gateway():
    """按路径加载 gateway.py。它 import 同目录的 common，故需先把 scripts/ 加进 sys.path。"""
    scripts_dir = GATEWAY_PY.parent
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location("gateway_under_test", GATEWAY_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def handler_stub(gw, **attrs):
    """构造绑定了真实 `Handler._log` 方法的轻量级测试桩。"""
    import types
    ns = types.SimpleNamespace(**attrs)
    ns._log = lambda **f: gw.Handler._log(ns, **f)
    return ns


def capture_log_event(mod, event, req_id=None):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.log_event(event, req_id=req_id)
    return json.loads(buf.getvalue())


class TestRequestIdInjection(unittest.TestCase):
    """request_id 必须由 log_event 统一注入，绝不能被调用点的空值顶掉。"""

    def setUp(self):
        self.gw = load_gateway()

    def test_injected_when_absent(self):
        rec = capture_log_event(self.gw, {"event": "upstream_ok"}, req_id="req_abc123")
        self.assertEqual(rec["request_id"], "req_abc123")

    def test_explicit_key_would_mask_the_injected_id(self):
        """验证当 event 已含 request_id 键时 log_event 不会覆盖（因此调用点不得显式写入空值）。"""
        rec = capture_log_event(self.gw, {"event": "x", "request_id": ""}, req_id="req_abc123")
        self.assertEqual(rec["request_id"], "", "键已存在时 log_event 不覆盖 —— 故调用点不能写它")

    def test_no_call_site_sets_request_id_explicitly(self):
        """静态守卫：回归即红。"""
        src = GATEWAY_PY.read_text(encoding="utf-8")
        offenders = [
            (i, line.strip())
            for i, line in enumerate(src.splitlines(), 1)
            # 只看字典字面量里的键；注释行不算（说明里会引用这个写法）
            if re.search(r'"request_id"\s*:', line) and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            offenders, [],
            f"{GATEWAY_PY.name} 的调用点又自己写了 request_id —— 会遮蔽内部 req_id，"
            f"成功请求将无法与请求特征关联。请改用 upstream_request_id。违规行：{offenders}")


class TestHeadersSentFlag(unittest.TestCase):
    """上游失败事件必须落盘「响应头当时是否已发出」。"""

    def setUp(self):
        self.gw = load_gateway()

    def test_flag_reflects_attribute(self):
        """助手直接读 self._headers_sent，缺失时按 False 处理（不抛）。"""
        import types
        self.assertFalse(self.gw.Handler._headers_sent_flag(types.SimpleNamespace()))
        self.assertFalse(self.gw.Handler._headers_sent_flag(types.SimpleNamespace(_headers_sent=False)))
        self.assertTrue(self.gw.Handler._headers_sent_flag(types.SimpleNamespace(_headers_sent=True)))

    def test_every_upstream_failure_event_records_headers_sent(self):
        """静态守卫：每个上游失败事件都要带这个字段，漏一个就等于那类失败无法判读。"""
        lines = GATEWAY_PY.read_text(encoding="utf-8").splitlines()
        for name in UPSTREAM_FAILURE_EVENTS:
            idx = next((i for i, l in enumerate(lines) if f'event="{name}"' in l), None)
            self.assertIsNotNone(idx, f"找不到事件 {name} 的写入点，事件表变了？")
            window = "\n".join(lines[idx:idx + 6])
            self.assertIn(
                'headers_sent=self._headers_sent_flag()', window,
                f"{name} 没有记录 headers_sent —— 事后无法判断这类失败能否重试")


class TestUpstreamErrorBody(unittest.TestCase):
    """验证上游 4xx/5xx 失败日志必须记录截断后的 error_body。"""

    def setUp(self):
        self.gw = load_gateway()

    def _handle(self, raw_body, code=400):
        """走真实的 _handle_http_error 路径，捕获它写出的那条日志。

        `_headers_sent=True` 让它记录后立刻返回，因此不需要真的 socket。
        """
        import types
        exc = types.SimpleNamespace(code=code, read=lambda: raw_body)
        fake = handler_stub(
            self.gw,
            client_address=("127.0.0.1", 1),
            path="/v1/messages",
            _req_id="req_test",
            _headers_sent=True,
            _headers_sent_flag=lambda: True,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.gw.Handler._handle_http_error(fake, exc, {"model": "m"}, time.time(), "sid", "main")
        return json.loads(buf.getvalue())

    def test_error_body_is_logged(self):
        rec = self._handle(b'{"error":{"message":"tools must not be empty"}}')
        self.assertEqual(rec["status"], 400)
        self.assertIn("tools must not be empty", rec["error_body"])

    def test_error_body_is_truncated(self):
        """超长错误体只记前 N 字符：日志按 5MB 轮转，不能让单个错误体把它撑爆。"""
        rec = self._handle(b"x" * 10000)
        self.assertEqual(len(rec["error_body"]), self.gw.ERROR_BODY_LOG_CHARS)

    def test_non_utf8_body_does_not_raise(self):
        """上游可能回非 UTF-8 字节：解码必须容错，不能因为记日志把请求搞挂。"""
        rec = self._handle(b"\xff\xfe not utf-8")
        self.assertIn("error_body", rec)

    def test_write_site_records_error_body(self):
        """静态守卫：漏了这行，4xx 就又只剩一个状态码。"""
        lines = GATEWAY_PY.read_text(encoding="utf-8").splitlines()
        idx = next((i for i, l in enumerate(lines) if 'event="upstream_http_error"' in l), None)
        self.assertIsNotNone(idx, "找不到 upstream_http_error 的写入点，事件表变了？")
        window = "\n".join(lines[idx:idx + 8])
        self.assertIn('error_body=detail[:ERROR_BODY_LOG_CHARS]', window)


class TestPrefixFingerprint(unittest.TestCase):
    """验证每轮请求（含未触发任何规则的轮次）均记录 system / tools 前缀哈希。"""

    def setUp(self):
        self.gw = load_gateway()

    def test_hashes_are_stable_and_sensitive(self):
        """同内容同摘要；system 或 tools 任一处变了，摘要就要变。"""
        base = {"system": [{"type": "text", "text": "s"}], "tools": [{"name": "Read"}]}
        same = {"system": [{"type": "text", "text": "s"}], "tools": [{"name": "Read"}]}
        other_sys = {"system": [{"type": "text", "text": "s2"}], "tools": [{"name": "Read"}]}
        other_tools = {"system": [{"type": "text", "text": "s"}], "tools": [{"name": "Bash"}]}

        self.assertEqual(self.gw.prefix_hashes(base), self.gw.prefix_hashes(same))
        self.assertNotEqual(self.gw.prefix_hashes(base), self.gw.prefix_hashes(other_sys),
                            "system 变了摘要必须跟着变，否则这条判据是死的")
        self.assertNotEqual(self.gw.prefix_hashes(base), self.gw.prefix_hashes(other_tools))

        sys_hash, tools_hash = self.gw.prefix_hashes(base)
        self.assertEqual(len(sys_hash), 12)
        self.assertEqual(len(tools_hash), 12)

    def test_key_order_changes_the_hash(self):
        """摘要必须跟着**实际发出去的字节**：换键顺序线上前缀就变了，摘要也得变。

        加了 sort_keys 就会漏掉这一类，把本地原因误判成上游 TTL。
        """
        a = {"tools": [{"name": "Read", "description": "d", "input_schema": {}}]}
        b = {"tools": [{"description": "d", "name": "Read", "input_schema": {}}]}
        self.assertNotEqual(self.gw.prefix_hashes(a), self.gw.prefix_hashes(b),
                            "键顺序变了摘要却不变 —— 这类前缀漂移会被归因到上游")

    def test_messages_are_deliberately_excluded(self):
        """messages 每轮都在长，参与比对就会永远报「变了」—— 必须有这条，否则有人会「补上」。"""
        a = {"system": [], "tools": [], "messages": [{"role": "user", "content": "x"}]}
        b = {"system": [], "tools": [], "messages": [{"role": "user", "content": "x"},
                                                     {"role": "assistant", "content": "y"}]}
        self.assertEqual(self.gw.prefix_hashes(a), self.gw.prefix_hashes(b))

    def test_stabilize_logs_a_fingerprint_even_when_nothing_changed(self):
        """回归守卫：本轮未触发任何清洗规则时，`canonicalized` 事件仍须落盘。"""
        # _stabilize 只碰这几个属性；够用即可，不需要起真的 HTTP handler。
        stub = handler_stub(self.gw, _req_id="req_fingerprint", _raw_body=b"{}",
                            path="/v1/messages", client_address=("127.0.0.1", 12345))

        parsed = {"system": [{"type": "text", "text": "hi"}], "tools": [], "messages": []}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.gw.Handler._stabilize(stub, parsed, "sid-x", "main", "m")

        events = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip().startswith("{")]
        canon = [e for e in events if e.get("event") == "canonicalized"]
        self.assertEqual(len(canon), 1,
                         f"没落盘 canonicalized —— 前缀指纹又退回「只在改动时才有」了。事件：{events}")
        self.assertIn("sys_hash", canon[0])
        self.assertIn("tools_hash", canon[0])


class TestLogRetention(unittest.TestCase):
    """验证日志 7 天自动淘汰机制：删除旧归档，守住活跃主日志。"""

    def setUp(self):
        self.gw = load_gateway()

    def test_purge_old_logs_cleans_expired_archives(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            now = time.time()
            # 1. 活跃主日志（即使时间久远也绝不删除）
            gw_active = Path(tmpdir) / "gateway.log"
            gw_active.write_text("active gateway\n", encoding="utf-8")
            os.utime(gw_active, (now - 10 * 86400, now - 10 * 86400))

            mgr_active = Path(tmpdir) / "manager.log"
            mgr_active.write_text("active manager\n", encoding="utf-8")
            os.utime(mgr_active, (now - 10 * 86400, now - 10 * 86400))

            # 2. 8 天前的归档日志（必须被清理）—— 文件名用生产那套 gateway.YYYY-MM-DD.log
            gw_old = Path(tmpdir) / "gateway.2026-10-01.log"
            gw_old.write_text("old archive\n", encoding="utf-8")
            os.utime(gw_old, (now - 8 * 86400, now - 8 * 86400))

            mgr_old = Path(tmpdir) / "manager.2026-10-01.log"
            mgr_old.write_text("old manager archive\n", encoding="utf-8")
            os.utime(mgr_old, (now - 8 * 86400, now - 8 * 86400))

            # 3. 2 天前的新归档日志（必须保留）
            gw_recent = Path(tmpdir) / "gateway.2026-10-15.log"
            gw_recent.write_text("recent archive\n", encoding="utf-8")
            os.utime(gw_recent, (now - 2 * 86400, now - 2 * 86400))

            # 4. 执行 7 天清理
            self.gw.purge_old_logs(tmpdir, retention_days=7)

            self.assertTrue(gw_active.exists(), "gateway.log 是活跃主日志，绝不能被清理")
            self.assertTrue(mgr_active.exists(), "manager.log 是活跃主日志，绝不能被清理")
            self.assertFalse(gw_old.exists(), "超过 7 天的归档日志应该被物理删除")
            self.assertFalse(mgr_old.exists(), "manager 归档走同一套清理，不能只删网关那一半")
            self.assertTrue(gw_recent.exists(), "未满 7 天的归档日志必须保留")

    def test_log_event_writes_daily_file(self):
        """log_event 自动按当天自然日写入 gateway.YYYY-MM-DD.log 并维护 gateway.log 软链。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            old_base = self.gw.BASE_DIR
            try:
                self.gw.BASE_DIR = tmpdir
                os.makedirs(os.path.join(tmpdir, "logs"), exist_ok=True)
                self.gw.log_event({"event": "test_event", "msg": "hello"})

                today_str = time.strftime("%Y-%m-%d")
                expected_file = Path(tmpdir) / "logs" / f"gateway.{today_str}.log"
                self.assertTrue(expected_file.exists(), "必须直接生成当天的日志文件")
                self.assertIn("test_event", expected_file.read_text(encoding="utf-8"))

                symlink = Path(tmpdir) / "logs" / "gateway.log"
                self.assertTrue(symlink.exists(), "gateway.log 软链必须存在")
                self.assertTrue(symlink.is_symlink(), "gateway.log 必须是软链接")
            finally:
                self.gw.BASE_DIR = old_base

    def test_purge_failure_is_reported_not_fatal(self):
        """清理抛错时：不能把调用方带崩（启动期那一处会让网关起不来），也不能静默。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            gw = self.gw
            old_base, old_purge = gw.BASE_DIR, gw.purge_old_logs
            gw.BASE_DIR = tmpdir
            os.makedirs(os.path.join(tmpdir, "logs"), exist_ok=True)

            def boom(*args, **kwargs):
                raise NameError("name 'time' is not defined")

            gw.purge_old_logs = boom
            gw._purge_error_memo[0] = None
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    gw.purge_logs_quietly()     # 不许抛
            finally:
                gw.BASE_DIR, gw.purge_old_logs = old_base, old_purge

        events = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip().startswith("{")]
        self.assertTrue(any(e.get("event") == "log_purge_failed" for e in events),
                        f"清理失败必须落一条 log_purge_failed，实际：{events}")


class TestLogRetentionConfig(unittest.TestCase):
    """日志保留天数只认 config.json 顶层的一个入口（配置台写在哪，网关就读哪里）。"""

    def setUp(self):
        self.gw = load_gateway()
        self._orig_read = self.gw._read_config

    def tearDown(self):
        self.gw._read_config = self._orig_read

    def _days(self, cfg):
        self.gw._read_config = lambda: cfg
        return self.gw.log_retention_days()

    def test_reads_top_level_key(self):
        self.assertEqual(self._days({"log_retention_days": 30}), 30,
                         "配置台写在顶层，网关必须读顶层；读 rules 里那份等于永远用默认值")

    def test_defaults_and_bounds(self):
        self.assertEqual(self._days({}), 7)
        self.assertEqual(self._days({"log_retention_days": 0}), 1)
        self.assertEqual(self._days({"log_retention_days": 9999}), 365)
        self.assertEqual(self._days({"log_retention_days": "abc"}), 7)

    def test_nested_under_rules_is_not_the_source(self):
        """守卫：`rules` 里的同名键不生效 —— 它曾经是那个用不上的位置。"""
        self.assertEqual(self._days({"rules": {"log_retention_days": 30}}), 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
