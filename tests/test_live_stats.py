#!/usr/bin/env python3
"""live_stats.py 的单测：把日志里的真实用量算成战绩时，别算错。

为什么需要这个文件
------------------
这块是页面上**唯一一处展示上游自己报回来的数字**（其余都是我们按体积估算）。
用户看到「命中 99.6%」会信它，所以我们算错就是骗人。

最容易被算错的是命中率的分母。上游这一轮读进去的全部输入 = 命中的 + 新写入缓存的 +
完全没走缓存的。只拿 cache_read / input_tokens 会把命中率算得虚高（把分母里的
命中部分扔掉了），甚至算出大于 100% 的数。

跑法：
    python3 -m unittest discover -s tools -p "test_*.py"
    python3 tools/test_live_stats.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "claude-tuner" / "scripts"


def load(name, filename):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ls = load("live_stats_under_test", "live_stats.py")


def ok_line(usage, ts="2026-10-13T10:00:00+0800", model="deepseek-flash"):
    return json.dumps({"event": "upstream_ok", "usage": usage, "ts": ts,
                       "model_mapped": model})


def canon_line(changes):
    return json.dumps({"event": "canonicalized", "changes": changes})


class TestEmptyAndBroken(unittest.TestCase):
    def test_no_lines_reports_not_seen(self):
        """没跑过请求时必须说「没有」，不能拿默认值充数。"""
        data = ls.summarize([])
        self.assertFalse(data["seen"])
        self.assertEqual(data["requests"], 0)
        self.assertEqual(data["hit_rate"], 0.0)
        self.assertIsNone(data["last"])

    def test_skips_garbage_lines(self):
        lines = ["", "   ", "not json at all", "{ broken", "[1,2,3]", ok_line(
            {"input_tokens": 10, "output_tokens": 1})]
        self.assertEqual(ls.summarize(lines)["requests"], 1)

    def test_ignores_events_without_usage(self):
        lines = [json.dumps({"event": "upstream_ok"}), json.dumps({"event": "probe_local"})]
        self.assertEqual(ls.summarize(lines)["requests"], 0)

    def test_rejects_negative_and_non_numeric_usage(self):
        lines = [ok_line({"input_tokens": -5, "output_tokens": "many"})]
        data = ls.summarize(lines)
        self.assertEqual(data["requests"], 1)
        self.assertEqual(data["tokens"]["input"], 0)
        self.assertEqual(data["tokens"]["output"], 0)


class TestHitRate(unittest.TestCase):
    def test_denominator_includes_everything_read(self):
        """命中率 = 命中 /（命中 + 未命中 + 新写缓存）。分母少一项就虚高。"""
        lines = [ok_line({"input_tokens": 100, "output_tokens": 50,
                          "cache_read_input_tokens": 800,
                          "cache_creation_input_tokens": 100})]
        data = ls.summarize(lines)
        self.assertEqual(data["billed_input"], 1000)
        self.assertAlmostEqual(data["hit_rate"], 0.8)

    def test_never_exceeds_one(self):
        lines = [ok_line({"input_tokens": 1, "cache_read_input_tokens": 999})]
        self.assertLessEqual(ls.summarize(lines)["hit_rate"], 1.0)

    def test_all_cache_miss(self):
        lines = [ok_line({"input_tokens": 500, "cache_read_input_tokens": 0})]
        self.assertEqual(ls.summarize(lines)["hit_rate"], 0.0)

    def test_sums_across_requests(self):
        lines = [ok_line({"input_tokens": 10, "cache_read_input_tokens": 90}),
                 ok_line({"input_tokens": 10, "cache_read_input_tokens": 90})]
        data = ls.summarize(lines)
        self.assertEqual(data["requests"], 2)
        self.assertAlmostEqual(data["hit_rate"], 0.9)

    def test_last_request_is_kept_verbatim(self):
        lines = [ok_line({"input_tokens": 10, "cache_read_input_tokens": 90}, ts="t1"),
                 ok_line({"input_tokens": 1, "cache_read_input_tokens": 2}, ts="t2",
                         model="deepseek-v4-pro")]
        data = ls.summarize(lines)
        self.assertEqual(data["last"]["ts"], "t2")
        self.assertEqual(data["last"]["model"], "deepseek-v4-pro")
        self.assertEqual(data["last"]["usage"]["cache_read"], 2)


class TestPruningCounters(unittest.TestCase):
    def test_counts_stripped_tools(self):
        lines = [canon_line(["prune_tools_65", "strip_date"]),
                 canon_line(["prune_tools_10"])]
        data = ls.summarize(lines)
        self.assertEqual(data["stripped_events"], 2)
        self.assertEqual(data["stripped_tools"], 75)

    def test_ignores_malformed_prune_marker(self):
        data = ls.summarize([canon_line(["prune_tools_", "prune_tools_x"])])
        self.assertEqual(data["stripped_tools"], 0)


class TestNoMoneyClaim(unittest.TestCase):
    """**刻意不折算金额**，这条要钉住。

    本项目支持任意上游，每家的价目都不一样还随时会调。硬编码一张表，用户看到的
    金额就是编的（他接的是另一家）—— 数字看着精确、实际没有依据，比不给更糟。
    要算钱应该由客户端自己的 inferenceModelPricing 机制来，由用户填他那家的价。
    """

    def test_summary_carries_no_cost_fields(self):
        data = ls.summarize([ok_line({"input_tokens": 100, "cache_read_input_tokens": 900})])
        self.assertNotIn("cost", data)
        self.assertNotIn("price", data)

    def test_module_has_no_price_table(self):
        for name in ("PRICES", "PRICE_MATCH", "price_for"):
            self.assertFalse(hasattr(ls, name), "不该再有 %s" % name)


class TestTailAndFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 日志在 <home>/logs/ 下（与网关 BASE_DIR 布局一致）
        os.makedirs(os.path.join(self.tmp.name, "logs"), exist_ok=True)
        self.path = os.path.join(self.tmp.name, "logs", "gateway.log")

    def test_missing_file_reports_missing(self):
        data = ls.read_recent_usage(home=self.tmp.name)
        self.assertFalse(data["seen"])
        self.assertTrue(data.get("log_missing"))

    def test_reads_real_file(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(ok_line({"input_tokens": 5, "cache_read_input_tokens": 95}) + "\n")
        data = ls.read_recent_usage(home=self.tmp.name)
        self.assertTrue(data["seen"])
        self.assertAlmostEqual(data["hit_rate"], 0.95)

    def test_drops_truncated_first_line(self):
        """只读尾部时首行必然是半截，必须丢掉 —— 否则那次统计凭空少一条，
        更糟的是可能解析出一个字段残缺的事件。"""
        with open(self.path, "w", encoding="utf-8") as fh:
            for i in range(50):
                fh.write(ok_line({"input_tokens": 1}) + "\n")
        lines = ls.tail_lines(self.path, max_bytes=400)
        self.assertTrue(lines)
        for line in lines:
            if line.strip():
                json.loads(line)   # 每一行都必须是完整 JSON

    def test_environment_home_is_honored(self):
        os.environ["CLAUDE_TUNER_HOME"] = self.tmp.name
        self.addCleanup(lambda: os.environ.pop("CLAUDE_TUNER_HOME", None))
        self.assertEqual(ls.log_path(), self.path)
        # 显式传 home 时也走同一套布局
        self.assertEqual(ls.log_path(self.tmp.name), self.path)


class TestLogFileEnumeration(unittest.TestCase):
    """统计窗口必须真的覆盖归档，且同一行事件只能算一次。

    反例（真实存在过）：`glob(base + "*")` 里 base 是 `gateway.log`，只命中那个软链本身，
    于是页面写着「近 7 天」而数据只有当天；而另一份实现用 `gateway*.log*`，又把
    `gateway.out.log`（stdout 副本）和日文件一起读了，同一行被计两到三次。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.logs)
        self.day1 = os.path.join(self.logs, "gateway.2026-10-11.log")
        self.day2 = os.path.join(self.logs, "gateway.2026-10-12.log")
        for path, ts in ((self.day1, 1000), (self.day2, 2000)):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(ok_line({"input_tokens": 1}) + "\n")
            os.utime(path, (ts, ts))
        self.active = os.path.join(self.logs, "gateway.2026-10-13.log")
        with open(self.active, "w", encoding="utf-8") as fh:
            fh.write(ok_line({"input_tokens": 2}) + "\n")
        os.utime(self.active, (3000, 3000))
        os.symlink("gateway.2026-10-13.log", os.path.join(self.logs, "gateway.log"))
        # stdout 副本：每次事件都多一份，读它会双倍
        with open(os.path.join(self.logs, "gateway.out.log"), "w", encoding="utf-8") as fh:
            fh.write(ok_line({"input_tokens": 2}) + "\n")

    def test_covers_archives_and_skips_stdout_copy(self):
        paths = [os.path.basename(p) for p in ls.log_paths(self.tmp.name)]
        self.assertEqual(paths, ["gateway.2026-10-11.log",
                                 "gateway.2026-10-12.log",
                                 "gateway.2026-10-13.log"],
                         "归档必须读到、stdout 副本必须排除、软链与日文件不能算两份")

    def test_symlink_and_dayfile_are_one_file(self):
        """软链与它指向的日文件是同一份：真实路径去重后只能出现一次。"""
        reals = [os.path.realpath(p) for p in ls.log_paths(self.tmp.name)]
        self.assertEqual(len(reals), len(set(reals)))

    def test_summary_span_matches_all_archives(self):
        lines = []
        for path in ls.log_paths(self.tmp.name):
            lines.extend(ls.tail_lines(path))
        data = ls.summarize(lines)
        self.assertEqual(data["requests"], 3, "三份日志各一条 upstream_ok")

    def test_event_cap_keeps_the_newest_days(self):
        """撞上 MAX_EVENTS 上限时必须保最新、丢最老。

        反例（真实存在过）：从最老的归档正序往下读，十几份历史文件先把两万条额度
        吃光，最近几天一条都进不来 —— 页面上的数字就永远停在最老那天，看着像「不更新」。
        """
        orig = ls.MAX_EVENTS
        ls.MAX_EVENTS = 1
        try:
            data = ls.read_recent_usage(self.tmp.name)
        finally:
            ls.MAX_EVENTS = orig
        self.assertEqual(data["tokens"]["input"], 2,
                         "额度只有一个事件时，留下的必须是那份最新日志里的事件")


class TestLastPruning(unittest.TestCase):
    """工具面的主指标：只认实际请求里的那两个数，读不到就说没有。

    这两个数（发了几个 / 剥了几个）分别躺在请求行与 canonicalized 行，靠 request_id
    串起来。页面拿它当主指标，所以一旦串错，用户看到的「剥掉 61 / 88」就可能是
    两次不同请求拼出来的。
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, "logs"))
        self.addCleanup(__import__("shutil").rmtree, self.home, True)

    def _write(self, events):
        with open(os.path.join(self.home, "logs", "gateway.log"), "w", encoding="utf-8") as fh:
            for e in events:
                fh.write(json.dumps(e, ensure_ascii=False) + "\n")

    def test_按_request_id_把两个数串起来(self):
        self._write([
            {"event": "request", "request_id": "r1", "tools": 88},
            {"event": "canonicalized", "request_id": "r1",
             "changes": ["prune_tools_61", "strip_date"]},
        ])
        got = ls.last_pruning(self.home)
        self.assertEqual((got["incoming"], got["stripped"], got["kept"]), (88, 61, 27))

    def test_取最近一次而不是拼两次请求(self):
        self._write([
            {"event": "request", "request_id": "r1", "tools": 88},
            {"event": "canonicalized", "request_id": "r1", "changes": ["prune_tools_61"]},
            {"event": "request", "request_id": "r2", "tools": 90},
            {"event": "canonicalized", "request_id": "r2", "changes": ["prune_tools_62"]},
        ])
        got = ls.last_pruning(self.home)
        self.assertEqual((got["incoming"], got["stripped"]), (90, 62))

    def test_没剥任何工具时按_0_算(self):
        self._write([
            {"event": "request", "request_id": "r1", "tools": 5},
            {"event": "canonicalized", "request_id": "r1", "changes": ["strip_date"]},
        ])
        got = ls.last_pruning(self.home)
        self.assertEqual((got["incoming"], got["stripped"], got["kept"]), (5, 0, 5))

    def test_读不到就返回_None(self):
        self._write([])
        self.assertIsNone(ls.last_pruning(self.home))
        # 只有探针请求（没有 tools 字段）时同样没有可报的数
        self._write([{"event": "request", "request_id": "r9"},
                     {"event": "canonicalized", "request_id": "r9", "changes": []}])
        self.assertIsNone(ls.last_pruning(self.home))

if __name__ == "__main__":
    unittest.main(verbosity=2)
