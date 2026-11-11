"""使用内存 HTTP 流和临时 Journal 验证协议，无 socket。"""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from mock_provider import __main__ as entry
from mock_provider import provider


def body(**overrides):
    request = {
        "model": provider.MODEL,
        "stream": True,
        "messages": [{"role": "user", "content": "synthetic prompt"}],
    }
    request.update(overrides)
    return json.dumps(request).encode("utf-8")


def wire(payload=None, *, method="POST", target=provider.ROUTE, content_type="application/json",
         length=None, extra=""):
    payload = body() if payload is None else payload
    length = f"Content-Length: {len(payload)}\r\n" if length is None else length
    headers = (
        f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Content-Type: {content_type}\r\n{length}{extra}\r\n"
    )
    return headers.encode("ascii") + payload


class MockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mock-provider-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.provider = provider.MockProvider(self.root)

    def exchange(self, request, *, subject=None, source=None, sink=None, deadline=None):
        subject = self.provider if subject is None else subject
        source = io.BytesIO(request) if source is None else source
        sink = io.BytesIO() if sink is None else sink
        subject.exchange(source, sink, deadline)
        return sink.getvalue()

    def journal(self):
        return json.loads(self.provider.journal.read_text(encoding="utf-8"))

    def reject(self, request, status, category):
        response = self.exchange(request)
        header, content = response.split(b"\r\n\r\n", 1)
        self.assertEqual(int(header.split(b" ", 2)[1]), status)
        self.assertIn(b"Content-Length: 0", header.split(b"\r\n"))
        self.assertEqual(content, b"")
        event = self.journal()["requests"][-1]
        self.assertEqual((event["status"], event["category"], event["chunks_sent"], event["done_sent"]),
                         (status, category, 0, False))

    def assert_sse(self, content):
        events = content.split(b"\n\n")
        self.assertEqual(events[-2:], [b"data: [DONE]", b""])
        self.assertEqual(len(events), 5)
        choices = []
        for event in events[:-2]:
            self.assertTrue(event.startswith(b"data: "))
            chunk = json.loads(event[len(b"data: "):])
            self.assertEqual(chunk["object"], "chat.completion.chunk")
            self.assertEqual(chunk["id"], "chatcmpl-dsh-mock")
            self.assertIs(type(chunk["created"]), int)
            self.assertEqual(chunk["model"], provider.MODEL)
            self.assertEqual(len(chunk["choices"]), 1)
            choice = chunk["choices"][0]
            self.assertIs(type(choice["index"]), int)
            self.assertEqual(choice["index"], 0)
            self.assertIsInstance(choice["delta"], dict)
            choices.append(choice)
        self.assertEqual(choices, [
            {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None},
            {"index": 0, "delta": {"content": "READY"}, "finish_reason": None},
            {"index": 0, "delta": {}, "finish_reason": "stop"},
        ])

    def assert_success(self, response):
        header, content = response.split(b"\r\n\r\n", 1)
        self.assertEqual(header.split(b"\r\n")[0], b"HTTP/1.0 200 OK")
        self.assertIn(b"Content-Type: text/event-stream; charset=utf-8", header.split(b"\r\n"))
        self.assertIn(f"Content-Length: {len(content)}".encode(), header.split(b"\r\n"))
        self.assertLessEqual(len(content), provider.MAX_RESPONSE_BYTES)
        self.assert_sse(content)

    def test_stream_response_wire_format_and_journal(self):
        self.assert_success(self.exchange(wire()))
        event = self.journal()["requests"][0]
        self.assertEqual((event["ordinal"], event["category"], event["delivery"], event["chunks_sent"]),
                         (1, "accepted", "complete", 3))
        self.assertTrue(event["route_match"] and event["stream_valid"] and event["done_sent"])

    def test_non_sse_json_regression(self):
        legacy = b'{"object":"chat.completion","choices":[{"message":{"content":"READY"},"finish_reason":"stop"}]}'
        for media_type in ("application/json", provider.CONTENT_TYPE):
            header = (
                f"HTTP/1.0 200 OK\r\nContent-Type: {media_type}\r\n"
                f"Content-Length: {len(legacy)}\r\n\r\n"
            ).encode("ascii")
            with self.subTest(media_type=media_type), self.assertRaises(AssertionError):
                self.assert_success(header + legacy)

    def test_sse_fixture_rejects_missing_finish_done_and_abrupt_close(self):
        content = self.exchange(wire()).split(b"\r\n\r\n", 1)[1]
        invalid = {
            "missing finish": content.replace(b',"finish_reason":"stop"', b""),
            "wrong finish": content.replace(b'"finish_reason":"stop"', b'"finish_reason":null'),
            "missing done": content.removesuffix(provider.DONE),
            "abrupt close": content[:-1],
            "unexpected event": content.replace(b"data: ", b"event: unknown\n", 1),
            "trailing frame": content + b"data: {}\n\n",
            "malformed JSON": content.replace(b"data: {", b"data: not-json{", 1),
        }
        for name, fixture in invalid.items():
            with self.subTest(name=name), self.assertRaises((AssertionError, KeyError, ValueError)):
                self.assert_sse(fixture)

    def test_method_route_and_content_type_rejected(self):
        self.reject(wire(method="GET"), 405, "invalid_method")
        self.reject(wire(target="/v1/chat/completions?extra=true"), 404, "invalid_route")
        self.reject(wire(content_type="text/plain"), 415, "invalid_content_type")
        self.reject(wire(extra="Content-Type: application/json\r\n"), 415, "invalid_content_type")

    def test_invalid_json_and_message_shape_rejected(self):
        self.reject(wire(b"{"), 400, "invalid_json")
        self.reject(wire(b'{"stream":true,"stream":false}'), 400, "invalid_json")
        self.reject(wire(b'{"stream":NaN}'), 400, "invalid_json")
        self.reject(wire(b"\xff"), 400, "invalid_json")
        self.reject(wire(b"[]"), 400, "invalid_body")
        self.reject(wire(body(messages=[])), 400, "invalid_messages")
        self.reject(wire(body(model="other-model")), 400, "unexpected_model")

    def test_stream_requires_boolean_true(self):
        self.reject(wire(b"{}"), 400, "stream_required")
        for stream in (False, 1, "true", None):
            self.reject(wire(body(stream=stream)), 400, "stream_required")

    def test_empty_tool_definitions_and_optional_stream_options(self):
        response = self.exchange(wire(body(
            tools=[], functions=[], tool_choice="none", parallel_tool_calls=False,
            stream_options={"include_usage": True},
            messages=[{"role": "user", "content": [{"type": "text", "text": "synthetic"}]}],
        ), content_type="application/json; charset=utf-8"))
        self.assert_sse(response.split(b"\r\n\r\n", 1)[1])
        event = self.journal()["requests"][0]
        self.assertTrue(event["tools_present"] and event["functions_present"])
        self.assertEqual((event["tool_count"], event["function_count"]), (0, 0))

    def test_unexpected_tools_and_functions_rejected(self):
        cases = (
            ({"tools": [{"type": "function", "function": {"name": "synthetic"}}]}, 1, 0),
            ({"functions": [{"name": "synthetic"}]}, 0, 1),
            ({"tools": None}, 0, 0),
            ({"tool_choice": "required"}, 0, 0),
            ({"parallel_tool_calls": True}, 0, 0),
            ({"function_call": {"name": "synthetic"}}, 0, 0),
            ({"messages": [{"role": "tool", "content": "synthetic"}]}, 0, 0),
            ({"messages": [{"role": "assistant", "tool_calls": [{}]}]}, 0, 0),
        )
        for overrides, tool_count, function_count in cases:
            with self.subTest(overrides=overrides):
                self.reject(wire(body(**overrides)), 400, "unexpected_tools")
                event = self.journal()["requests"][-1]
                self.assertEqual((event["tool_count"], event["function_count"]), (tool_count, function_count))
        self.assertEqual(self.journal()["accepted"], 0)

    def test_http_framing_and_request_limits(self):
        self.reject(wire(length=""), 411, "length_required")
        self.reject(wire(extra="Content-Length: 0\r\n"), 400, "invalid_length")
        self.reject(wire(extra="Transfer-Encoding: chunked\r\n"), 400, "unsupported_transfer_encoding")
        self.reject(wire(extra="Expect: 100-continue\r\n"), 417, "unsupported_expectation")
        self.reject(wire(length=f"Content-Length: {provider.MAX_BODY_BYTES + 1}\r\n"), 413, "body_limit")
        self.reject(wire(length="Content-Length: 65536\r\n"), 400, "truncated_body")
        self.reject(wire(extra="X-Large: " + "x" * provider.MAX_HEAD_BYTES + "\r\n"), 431, "head_limit")
        self.reject(wire(extra="X-Field: x\r\n" * 101), 431, "head_limit")

    def test_malformed_http_head_rejected(self):
        self.reject(b"POST broken\r\n\r\n", 400, "invalid_request_line")
        self.reject(wire().replace(b"Host:", b"Invalid Header:", 1), 400, "invalid_header")
        self.reject(b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n", 400, "incomplete_head")

    def test_oversized_response_rejected_before_budget_is_consumed(self):
        with mock.patch.object(provider, "MAX_RESPONSE_BYTES", 1):
            self.reject(wire(), 500, "response_limit")
        self.assertEqual(self.journal()["accepted"], 0)

    def test_one_shot_and_attempt_cap(self):
        self.reject(wire(body(stream=False)), 400, "stream_required")
        self.assertIn(b"HTTP/1.0 200 OK", self.exchange(wire()))
        for _ in range(provider.MAX_ATTEMPTS - 2):
            self.reject(wire(), 429, "over_budget")
        self.assertTrue(self.provider.exhausted)
        self.assertEqual(self.exchange(wire()), b"")
        state = self.journal()
        self.assertEqual((state["attempts"], state["accepted"], state["over_budget"]),
                         (provider.MAX_ATTEMPTS, 1, provider.MAX_ATTEMPTS - 2))
        self.assertEqual([row["ordinal"] for row in state["requests"]], list(range(1, provider.MAX_ATTEMPTS + 1)))

    def test_over_budget_does_not_read_body(self):
        self.exchange(wire())
        source = io.BytesIO(wire())
        with mock.patch.object(source, "read", side_effect=AssertionError("over-budget body read")):
            self.assertTrue(self.exchange(b"", source=source).startswith(b"HTTP/1.0 429"))

    def test_disconnect_mid_response_spends_budget_and_records_partial_delivery(self):
        class Disconnect(io.BytesIO):
            def write(self, data):
                if data.startswith(b"data: ") and b'"content"' in data:
                    raise BrokenPipeError
                return super().write(data)

        response = self.exchange(wire(), sink=Disconnect())
        self.assertNotIn(provider.DONE, response)
        event = self.journal()["requests"][0]
        self.assertEqual((event["delivery"], event["chunks_sent"], event["done_sent"]), ("disconnect", 1, False))
        self.reject(wire(), 429, "over_budget")

    def test_timeout_during_body_read(self):
        clock = mock.Mock(return_value=0)
        subject = provider.MockProvider(self.root, clock=clock)
        source = io.BytesIO(wire())
        read = source.read

        def slow_read(length):
            clock.return_value = provider.REQUEST_SECONDS
            return read(length)

        with mock.patch.object(source, "read", side_effect=slow_read):
            self.assertEqual(self.exchange(b"", subject=subject, source=source), b"")
        event = self.journal()["requests"][0]
        self.assertEqual((event["category"], event["delivery"]), ("timeout", "not_sent"))
        self.assertEqual(self.journal()["accepted"], 0)

    def test_header_eof_at_deadline_is_recorded_as_timeout(self):
        clock = mock.Mock(return_value=0)
        subject = provider.MockProvider(self.root, clock=clock)
        source = io.BytesIO()

        def deadline_eof(_):
            clock.return_value = provider.REQUEST_SECONDS
            return b""

        with mock.patch.object(source, "readline", side_effect=deadline_eof):
            self.assertEqual(self.exchange(b"", subject=subject, source=source), b"")
        event = self.journal()["requests"][0]
        self.assertEqual((event["category"], event["delivery"]), ("timeout", "not_sent"))

    def test_timeout_during_delivery_prevents_further_frames(self):
        clock = mock.Mock(return_value=0)
        subject = provider.MockProvider(self.root, clock=clock)
        sink = io.BytesIO()
        write = sink.write

        def slow_write(data):
            if data.startswith(b"data: "):
                clock.return_value = provider.REQUEST_SECONDS
            return write(data)

        with mock.patch.object(sink, "write", side_effect=slow_write):
            output = self.exchange(wire(), subject=subject, sink=sink)
        self.assertNotIn(provider.DONE, output)
        event = self.journal()["requests"][0]
        self.assertEqual((event["delivery"], event["chunks_sent"], event["done_sent"]), ("timeout", 1, False))

    def test_journal_privacy_and_sync_before_first_response_byte(self):
        payload = body(messages=[{"role": "user", "content": "private-synthetic-probe"}],
                       user="synthetic-user-probe")
        sink = io.BytesIO()
        write = sink.write

        def check_journal(data):
            self.assertEqual(self.journal()["accepted"], 1)
            return write(data)

        with mock.patch.object(provider.os, "fsync", wraps=os.fsync) as fsync, \
                mock.patch.object(sink, "write", side_effect=check_journal):
            self.exchange(wire(payload, extra="Authorization: Bearer synthetic-header-probe\r\n"), sink=sink)
        self.assertEqual(fsync.call_count, 3)
        self.reject(wire(target="/synthetic-query-probe"), 429, "over_budget")
        journal = self.provider.journal.read_bytes()
        for sensitive in (
            b"private-synthetic-probe", b"synthetic-user-probe", b"synthetic-header-probe",
            b"synthetic-query-probe", b"Authorization", b"READY", b"chatcmpl-", payload,
        ):
            self.assertNotIn(sensitive, journal)
        self.assertEqual(list(self.root.iterdir()), [self.provider.journal])

    def test_journal_sync_failure_prevents_success_response(self):
        sink = io.BytesIO()
        with mock.patch.object(provider.os, "fsync", side_effect=(None, OSError("synthetic I/O failure"))):
            with self.assertRaises(OSError):
                self.exchange(wire(), sink=sink)
        self.assertEqual(sink.getvalue(), b"")
        self.assertEqual(self.journal()["accepted"], 0)
        self.assertEqual(self.journal()["requests"][0]["category"], "pending")
        self.assertEqual(list(self.root.iterdir()), [self.provider.journal])


class ListenerContractTests(unittest.TestCase):
    def test_binding_argument_is_fixed_loopback_without_constructing_socket(self):
        with mock.patch.object(entry.socketserver.TCPServer, "__init__", return_value=None) as constructor:
            entry._Server(8765, mock.Mock())
        constructor.assert_called_once_with(("127.0.0.1", 8765), entry._Handler)

    def test_watchdog_caps_request_at_server_deadline_without_socket(self):
        handler = object.__new__(entry._Handler)
        handler.server = mock.Mock(deadline=102)
        handler.request = mock.Mock()
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        with mock.patch.object(entry.time, "monotonic", return_value=100), \
                mock.patch.object(entry.threading, "Timer") as timer:
            handler.handle()
        handler.request.settimeout.assert_called_once_with(2)
        timer.assert_called_once_with(2, handler._expire)
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()
        handler.server.mock.exchange.assert_called_once_with(handler.rfile, handler.wfile, 102)
        timer.call_args.args[1]()
        handler.request.shutdown.assert_called_once_with(entry.socket.SHUT_RDWR)
