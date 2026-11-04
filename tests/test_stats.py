#!/usr/bin/env python3
"""`claude-tuner/scripts/stats.py` 读底稿时的两个口径。

这两条都是**数字错了没人会发现**的类型 —— 面板照常打印、格式也好看，
只是把 content block 数当成了调用次数、把压缩当成了缓存被破坏。

底稿的真实形态（实测）：一次 API 响应被写成**多行**，每个 content block 一行，
`usage` 逐行原样重复，同属一个 `message.id`。

运行：python3 tools/test_stats.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "claude-tuner" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import stats  # noqa: E402


def usage(inp, cr):
    return {"input_tokens": inp, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": 0, "output_tokens": 1}


def block(resp_id, inp, cr, index=0, model="deepseek-flash"):
    """一行 assistant 底稿（一个 content block）。"""
    return {"type": "assistant",
            "uuid": "%s-%d" % (resp_id, index),
            "apiBlockIndex": index,
            "message": {"id": resp_id, "model": model, "usage": usage(inp, cr)}}


def compact():
    return {"type": "system", "subtype": "compact_boundary"}


def write(rows):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


class StatsTestCase(unittest.TestCase):
    def setUp(self):
        self._paths = []
        self.addCleanup(lambda: [os.remove(p) for p in self._paths if os.path.exists(p)])

    def parse(self, rows):
        path = write(rows)
        self._paths.append(path)
        return stats.parse_session_transcript(path)


class TestResponseDedup(StatsTestCase):
    """一次响应算一次调用，不管它被拆成几个 content block。"""

    def test_multi_block_response_counts_once(self):
        """同一个 message.id 的多行底稿只能算 1 次调用，token 也不能按块数重复累加。"""
        rows = [block("resp-1", 1000, 0, i) for i in range(4)]
        s = self.parse(rows)
        self.assertEqual(s["main_upstream"], 1, "4 个块应只算 1 次调用")
        self.assertEqual(s["main_in"], 1000, "token 也不能跟着块数翻倍")
        self.assertEqual(s["main_cr"], 0)

    def test_blocks_of_different_responses_are_all_counted(self):
        """不同响应当然要各算一次（别把去重写成「只留一条」）。"""
        rows = [block("resp-1", 1000, 0, 0), block("resp-1", 1000, 0, 1),
                block("resp-2", 200, 5000, 0), block("resp-3", 100, 7000, 0)]
        s = self.parse(rows)
        self.assertEqual(s["main_upstream"], 3)
        self.assertEqual(s["main_in"], 1300)
        self.assertEqual(s["main_cr"], 12000)

    def test_synthetic_rows_are_skipped(self):
        """客户端本地合成的消息没有发生过 API 调用，不能进统计。"""
        rows = [block("resp-1", 1000, 0, 0),
                block("syn-1", 0, 0, 0, model="<synthetic>")]
        s = self.parse(rows)
        self.assertEqual(s["main_upstream"], 1)


class TestDropDetection(StatsTestCase):
    """「偶发重算」只该记缓存被破坏，不该记压缩。"""

    def test_compaction_shrink_is_not_a_recompute(self):
        """压缩把上下文整体换掉，cache_read 必然大幅回落（已由「压缩 N 次」单独报出，不计入重算）。"""
        rows = [block("resp-1", 100, 540800, 0),
                compact(),
                block("resp-2", 30000, 14208, 0)]
        s = self.parse(rows)
        self.assertEqual(s["compact_count"], 1)
        self.assertEqual(s["drops"], [], "压缩导致的回落被当成了重算")

    def test_genuine_drop_is_still_reported(self):
        """没有压缩时，真的回落仍要报出来（别把判据改过头）。"""
        rows = [block("resp-1", 100, 540800, 0),
                block("resp-2", 30000, 14208, 0)]
        s = self.parse(rows)
        self.assertEqual(len(s["drops"]), 1)
        self.assertEqual(s["drops"][0]["delta"], 540800 - 14208)

    def test_small_wobble_is_not_a_drop(self):
        """回落不足门槛的视为记录抖动。"""
        rows = [block("resp-1", 100, 100000, 0),
                block("resp-2", 100, 100000 - stats.DROP_MIN_DELTA + 1, 0)]
        self.assertEqual(self.parse(rows)["drops"], [])

    def test_compaction_flag_does_not_leak_to_later_turns(self):
        """压缩只影响紧随其后的那一次比较，之后的回落照常判。"""
        rows = [block("resp-1", 100, 540800, 0),
                compact(),
                block("resp-2", 30000, 14208, 0),
                block("resp-3", 30000, 1000, 0)]
        s = self.parse(rows)
        self.assertEqual(len(s["drops"]), 1, "压缩之后的那次真实回落漏报了")


class TestHydrationScope(unittest.TestCase):
    """Thinking 补全次数必须按 session_id 过滤，只统计当前会话（网关日志由所有会话共用）。"""

    SID = "sid-mine"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        logs = Path(self._tmp.name) / "logs"
        logs.mkdir()
        rows = [
            {"session_id": self.SID, "event": "canonicalized",
             "changes": ["restore_thinking_3", "strip_date"]},
            {"session_id": "sid-other", "event": "canonicalized",
             "changes": ["restore_thinking_100", "restore_thinking_50"]},
            {"session_id": "sid-other", "event": "canonicalized",
             "changes": ["restore_thinking_20"]},
        ]
        with open(logs / "gateway.log", "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        self._saved_base = stats.BASE
        stats.BASE = self._tmp.name
        self.addCleanup(lambda: setattr(stats, "BASE", self._saved_base))

    def test_only_own_session_is_counted(self):
        got = stats.get_session_hydration_details(self.SID)["thinking_hydrations"]
        self.assertEqual(got, 1, "把别的会话的补全算进来了")

    def test_unknown_session_counts_zero(self):
        got = stats.get_session_hydration_details("sid-nobody")["thinking_hydrations"]
        self.assertEqual(got, 0)


class TestLocalProbeRecords(StatsTestCase):
    """客户端把连通性探针以 `model:"probe"` 写进底稿，它不是一次上游调用。"""

    def test_probe_is_not_an_upstream_call(self):
        rows = [block("resp-1", 100, 8000, 0),
                block("msg_probe", 1, 0, 0, model="probe"),
                block("resp-2", 100, 8100, 0)]
        s = self.parse(rows)
        self.assertEqual(s["main_upstream"], 2, "探针被当成上游调用了")
        self.assertEqual(s["drops"], [], "探针 cache_read=0 会凭空造出一次「重算」")


class TestHydrationLogDedup(unittest.TestCase):
    """同一行事件在多个日志入口里只算一次。

    `gateway.log` 是当天日文件的软链，`gateway.out.log` 是 stdout 副本 ——
    按文件名 glob 会把同一条 `canonicalized` 读两三遍，补全次数跟着翻倍。
    """

    SID = "sid-dedup"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        logs = Path(self._tmp.name) / "logs"
        logs.mkdir()
        line = json.dumps({"event": "canonicalized", "session_id": self.SID,
                           "changes": ["restore_thinking_2"]}, ensure_ascii=False) + "\n"
        for name in ("gateway.2026-10-13.log", "gateway.out.log"):
            (logs / name).write_text(line, encoding="utf-8")
        os.symlink("gateway.2026-10-13.log", logs / "gateway.log")
        self._saved = stats.BASE
        stats.BASE = self._tmp.name
        self.addCleanup(lambda: setattr(stats, "BASE", self._saved))

    def test_same_event_counted_once(self):
        got = stats.get_session_hydration_details(self.SID)["thinking_hydrations"]
        self.assertEqual(got, 1, "日文件 / 软链 / stdout 副本是同一行事件，只能算一次")


if __name__ == "__main__":
    unittest.main(verbosity=2)
