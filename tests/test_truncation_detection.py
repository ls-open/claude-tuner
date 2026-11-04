import unittest
import os
import sys
import json

# 引入本地网关模块
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LOCAL_SCRIPTS = os.path.join(BASE_DIR, "scripts")

if LOCAL_SCRIPTS not in sys.path:
    sys.path.insert(0, LOCAL_SCRIPTS)

# 家目录隔离必须在 import gateway 之前（gateway 在 import 时就定下 BASE_DIR）
import _isolated_home  # noqa: E402,F401

import gateway

class TestTruncationDetection(unittest.TestCase):
    def test_usage_capture_non_stream_complete(self):
        """非流式响应：Content-Length 匹配且为有效 JSON -> 判定为完整"""
        cap = gateway.UsageCapture()
        body = json.dumps({"id": "msg_123", "content": [{"type": "text", "text": "hello"}], "usage": {"input_tokens": 10, "output_tokens": 5}}).encode("utf-8")
        cap.set_expected_length(len(body))
        cap.feed(body)
        self.assertTrue(cap.stream_complete(), "Content-Length 匹配且 JSON 完整应判定为 stream_complete")
        self.assertEqual(cap.get_usage(), {"input_tokens": 10, "output_tokens": 5})

    def test_usage_capture_non_stream_truncated_length(self):
        """非流式响应：Content-Length: 100，但实际只收到 30 字节 -> 判定为截断 (False)"""
        cap = gateway.UsageCapture()
        cap.set_expected_length(100)
        # 只喂 30 字节
        cap.feed(b'{"id": "msg_123", "content": [')
        self.assertFalse(cap.stream_complete(), "收到的字节数少于 Content-Length 应判定为未完成")

    def test_usage_capture_non_stream_invalid_json(self):
        """非流式响应：即使长度碰巧对上，但若被破坏不是合法的 JSON -> 判定为未完成 (False)"""
        cap = gateway.UsageCapture()
        body = b'{"id": "msg_123", "content": [broken_json_without_closing'
        cap.set_expected_length(len(body))
        cap.feed(body)
        self.assertFalse(cap.stream_complete(), "非法残缺 JSON 必须判定为未完成")

    def test_usage_capture_zero_bytes(self):
        """一个字节都没收到的空响应 -> 判定为未完成 (False)"""
        cap = gateway.UsageCapture()
        self.assertFalse(cap.stream_complete())

    def test_usage_capture_stream_sse_complete(self):
        """SSE 流式响应：包含 message_stop 或 [DONE] -> 判定为完成"""
        cap = gateway.UsageCapture()
        cap.feed(b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":100}}}\n\n')
        cap.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n')
        cap.feed(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        self.assertTrue(cap.stream_complete())
        self.assertEqual(cap.get_usage(), {"input_tokens": 100})

    def test_usage_capture_stream_sse_truncated(self):
        """SSE 流式响应：上游中途掐断未见 message_stop -> 判定为未完成 (False)"""
        cap = gateway.UsageCapture()
        cap.feed(b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":100}}}\n\n')
        cap.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n')
        # 中途截断
        self.assertFalse(cap.stream_complete())

if __name__ == "__main__":
    unittest.main()
