"""合成请求验证、SSE 和本地 Journal。可直接用内存流调用。"""

from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.client import HTTPException, parse_headers
import io
import json
import os
from pathlib import Path
import re
import time


ROUTE = "/v1/chat/completions"
MODEL = "dsh-mock"
CONTENT_TYPE = "text/event-stream; charset=utf-8"
MAX_ATTEMPTS = 8
MAX_HEAD_BYTES = 8192
MAX_BODY_BYTES = 65536
MAX_RESPONSE_BYTES = 4096
REQUEST_SECONDS = 5
DONE = b"data: [DONE]\n\n"
JOURNAL_NAME = "summary-mock-journal.json"


@dataclass
class RequestRecord:
    ordinal: int
    over_budget: bool = False
    route_match: bool = False
    stream_valid: bool = False
    tools_present: bool = False
    functions_present: bool = False
    tool_count: int = 0
    function_count: int = 0
    status: int | None = None
    chunks_sent: int = 0
    done_sent: bool = False
    category: str = "pending"
    delivery: str = "pending"
    duration_ms: int = 0


class Rejected(Exception):
    def __init__(self, status, category):
        self.status = status
        self.category = category


def _read_head(source):
    remaining = MAX_HEAD_BYTES

    def line():
        nonlocal remaining
        raw = source.readline(remaining + 1)
        remaining -= len(raw)
        if remaining < 0:
            raise Rejected(431, "head_limit")
        if not raw.endswith(b"\r\n"):
            raise Rejected(400, "incomplete_head")
        return raw

    request_line = re.fullmatch(rb"([!-~]+) ([!-~]+) HTTP/1\.[01]\r\n", line())
    if request_line is None:
        raise Rejected(400, "invalid_request_line")
    method, target = (part.decode("ascii") for part in request_line.groups())
    headers = bytearray()
    while (raw := line()) != b"\r\n":
        if not re.fullmatch(rb"[!#$%&'*+\-.^_\x60|~0-9A-Za-z]+:[\t\x20-\x7e\x80-\xff]*\r\n", raw):
            raise Rejected(400, "invalid_header")
        headers.extend(raw)
    try:
        return method, target, parse_headers(io.BytesIO(headers))
    except HTTPException:
        raise Rejected(431, "head_limit") from None


def _body_length(headers):
    if headers.get_all("Transfer-Encoding"):
        raise Rejected(400, "unsupported_transfer_encoding")
    lengths = headers.get_all("Content-Length", [])
    if not lengths:
        raise Rejected(411, "length_required")
    length = lengths[0].strip()
    if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", length):
        raise Rejected(400, "invalid_length")
    if len(length) > len(str(MAX_BODY_BYTES)) or int(length) > MAX_BODY_BYTES:
        raise Rejected(413, "body_limit")
    return int(length)


def _unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("duplicate JSON member")
    return obj


def _invalid_constant(_):
    raise ValueError("invalid JSON constant")


def _validate_body(body, record):
    try:
        request = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
        )
    except (ValueError, RecursionError):
        raise Rejected(400, "invalid_json") from None
    if not isinstance(request, dict):
        raise Rejected(400, "invalid_body")

    record.stream_valid = request.get("stream") is True
    tools = request.get("tools", [])
    functions = request.get("functions", [])
    record.tools_present = "tools" in request
    record.functions_present = "functions" in request
    record.tool_count = len(tools) if isinstance(tools, list) else 0
    record.function_count = len(functions) if isinstance(functions, list) else 0
    if (tools != [] or functions != [] or request.get("tool_choice", "none") != "none"
            or request.get("function_call", "none") != "none"
            or request.get("parallel_tool_calls", False) is not False):
        raise Rejected(400, "unexpected_tools")
    if not record.stream_valid:
        raise Rejected(400, "stream_required")
    if request.get("model") != MODEL:
        raise Rejected(400, "unexpected_model")

    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise Rejected(400, "invalid_messages")
    for message in messages:
        if not isinstance(message, dict):
            raise Rejected(400, "invalid_messages")
        if (message.get("role") in ("tool", "function")
                or any(key in message for key in ("tool_calls", "tool_call_id", "function_call"))):
            raise Rejected(400, "unexpected_tools")
        if message.get("role") not in ("system", "developer", "user", "assistant"):
            raise Rejected(400, "invalid_messages")
        content = message.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list) or not content or not all(
            isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
            for part in content
        ):
            raise Rejected(400, "invalid_messages")


def _sse_frames():
    frames = []
    for delta, finish in (({"role": "assistant"}, None), ({"content": "READY"}, None), ({}, "stop")):
        chunk = {
            "id": "chatcmpl-dsh-mock", "object": "chat.completion.chunk", "created": 0, "model": MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        frames.append(b"data: " + json.dumps(chunk, separators=(",", ":")).encode("utf-8") + b"\n\n")
    frames.append(DONE)
    if sum(map(len, frames)) > MAX_RESPONSE_BYTES:
        raise Rejected(500, "response_limit")
    return frames


class MockProvider:
    """单线程监听器的单次成功预算和固定大小 Journal。"""

    def __init__(self, directory: Path, clock=time.monotonic):
        self.journal = directory / JOURNAL_NAME
        self.clock = clock
        self.records = []
        self.accepted = False
        self._save()

    @property
    def exhausted(self):
        return len(self.records) >= MAX_ATTEMPTS

    def _save(self):
        snapshot = {
            "attempts": len(self.records),
            "accepted": int(self.accepted),
            "over_budget": sum(record.over_budget for record in self.records),
            "requests": [asdict(record) for record in self.records],
        }
        pending = self.journal.with_suffix(".tmp")
        handle = pending.open("x", encoding="utf-8")
        try:
            with handle:
                json.dump(snapshot, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, self.journal)
        finally:
            pending.unlink(missing_ok=True)

    def _check_deadline(self, deadline):
        if self.clock() >= deadline:
            raise TimeoutError

    def exchange(self, source, sink, deadline=None):
        """处理一次 HTTP/1.x 交换。source/sink 为二进制文件式对象。"""
        if self.exhausted:
            return
        started = self.clock()
        deadline = started + REQUEST_SECONDS if deadline is None else deadline
        record = RequestRecord(ordinal=len(self.records) + 1, over_budget=self.accepted)
        self.records.append(record)
        self._save()
        frames = []
        try:
            self._check_deadline(deadline)
            method, target, headers = _read_head(source)
            record.route_match = target == ROUTE
            if record.over_budget:
                raise Rejected(429, "over_budget")
            if method != "POST":
                raise Rejected(405, "invalid_method")
            if not record.route_match:
                raise Rejected(404, "invalid_route")
            types = headers.get_all("Content-Type", [])
            if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
                raise Rejected(415, "invalid_content_type")
            if headers.get_all("Expect"):
                raise Rejected(417, "unsupported_expectation")
            length = _body_length(headers)
            self._check_deadline(deadline)
            body = source.read(length)
            self._check_deadline(deadline)
            if len(body) != length:
                raise Rejected(400, "truncated_body")
            _validate_body(body, record)
            frames = _sse_frames()
            self._check_deadline(deadline)
            record.status = 200
            record.category = "accepted"
            self.accepted = True
        except (Rejected, OSError) as error:
            if isinstance(error, TimeoutError) or self.clock() >= deadline:
                record.category, record.delivery = "timeout", "not_sent"
            elif isinstance(error, Rejected):
                record.status, record.category = error.status, error.category
            else:
                record.category, record.delivery = "disconnect", "not_sent"
        # 先持久化预算和结果，再尝试发送，断线也消耗已接受的预算。
        self._save()
        try:
            if record.status is not None:
                self._write_response(sink, record, frames, deadline)
                record.delivery = "complete"
        except OSError as error:
            record.delivery = "timeout" if isinstance(error, TimeoutError) or self.clock() >= deadline else "disconnect"
        finally:
            record.duration_ms = int((self.clock() - started) * 1000)
            self._save()

    def _write_response(self, sink, record, frames, deadline):
        lines = [
            f"HTTP/1.0 {record.status} {HTTPStatus(record.status).phrase}",
            "Connection: close",
            f"Content-Length: {sum(map(len, frames))}",
        ]
        if frames:
            lines.extend((f"Content-Type: {CONTENT_TYPE}", "Cache-Control: no-cache"))
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")
        for index, data in enumerate((head, *frames)):
            self._check_deadline(deadline)
            if sink.write(data) != len(data):
                raise OSError("incomplete response write")
            sink.flush()
            if data == DONE:
                record.done_sent = True
            elif index:
                record.chunks_sent += 1
            self._check_deadline(deadline)
