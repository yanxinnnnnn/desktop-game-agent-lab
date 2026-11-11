"""供后续获准的本地联调用的入口。导入模块不会监听。"""

import argparse
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import time

from .provider import JOURNAL_NAME, REQUEST_SECONDS, MockProvider


LOOPBACK = "127.0.0.1"
LIFETIME_SECONDS = 30


class _Handler(socketserver.StreamRequestHandler):
    def _expire(self):
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def handle(self):
        now = time.monotonic()
        deadline = min(now + REQUEST_SECONDS, self.server.deadline)
        remaining = max(0, deadline - now)
        self.request.settimeout(remaining)
        # 单次 socket 超时可能被零碎输入延后，额外用墙钟截止时间切断整条连接。
        watchdog = threading.Timer(remaining, self._expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            self.server.mock.exchange(self.rfile, self.wfile, deadline)
        finally:
            watchdog.cancel()


class _Server(socketserver.TCPServer):
    def __init__(self, port, mock):
        self.mock = mock
        self.deadline = time.monotonic() + LIFETIME_SECONDS
        super().__init__((LOOPBACK, port), _Handler)

    def handle_error(self, request, client_address):
        # 让入口报告固定错误类别，避免 socketserver 打印异常和客户端地址。
        raise


def main():
    parser = argparse.ArgumentParser(description="Synthetic one-shot loopback SSE mock")
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    try:
        journal_dir = Path(tempfile.mkdtemp(prefix="dsh-mock-"))
        provider = MockProvider(journal_dir)
        with _Server(args.port, provider) as server:
            print(f"Listening on {LOOPBACK}:{args.port} for at most {LIFETIME_SECONDS}s", flush=True)
            print(f"Journal under OS temp: {journal_dir.name}/{JOURNAL_NAME}", flush=True)
            while not provider.exhausted:
                server.timeout = server.deadline - time.monotonic()
                if server.timeout <= 0:
                    break
                server.handle_request()
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Mock stopped: local processing failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
