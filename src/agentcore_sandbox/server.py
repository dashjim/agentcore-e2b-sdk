"""Minimal AgentCore Runtime HTTP entry point, with no model dependency."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time

from .adapter import Adapter, DEFAULT_BOOT_FILE, DEFAULT_WORKSPACE, MAX_BODY_BYTES, initialize_boot_file


def handler_for(adapter: Adapter):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AgentCoreSandbox/0.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            # Production operators may add structured operation metadata. Never
            # log request bodies, commands, file bytes, or environment values.
            return

        def _respond(self, status: int, value: dict):
            payload = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def _error(self, status: int, code: str, message: str):
            self._respond(status, {"version": 1, "request_id": None, "boot_id": adapter.boot_id,
                                   "error": {"code": code, "message": message}})

        def do_GET(self):
            if self.path != "/ping":
                self._error(404, "NOT_FOUND", "The HTTP route does not exist")
                return
            self._respond(200, {"status": "Healthy", "time_of_last_update": int(time.time())})

        def do_POST(self):
            if self.path != "/invocations":
                self._error(404, "NOT_FOUND", "The HTTP route does not exist")
                return
            if self.headers.get("Transfer-Encoding"):
                self._error(400, "INVALID_REQUEST", "The adapter requires a fixed Content-Length")
                return
            content_lengths = self.headers.get_all("Content-Length", [])
            try:
                if len(content_lengths) != 1:
                    raise ValueError("missing or repeated Content-Length")
                length = int(content_lengths[0])
                if length < 0:
                    raise ValueError("negative Content-Length")
            except ValueError:
                self._error(400, "INVALID_REQUEST", "The adapter requires one valid Content-Length")
                return
            if length > MAX_BODY_BYTES:
                self._error(413, "REQUEST_TOO_LARGE", "The adapter request exceeds its size limit")
                return
            if self.headers.get_content_type() != "application/json":
                self._error(415, "INVALID_REQUEST", "The adapter requires application/json")
                return
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("incomplete body")
                request = json.loads(body)
            except (ValueError, UnicodeError, TimeoutError, RecursionError):
                self._error(400, "INVALID_REQUEST", "The adapter could not decode the JSON request")
                return
            result = adapter.handle(request)
            self._respond(200, result)

    return Handler


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="Use 0.0.0.0 inside a Runtime container")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--workspace", default=os.environ.get("AGENTCORE_SANDBOX_WORKSPACE", DEFAULT_WORKSPACE))
    parser.add_argument("--boot-file", default=os.environ.get("AGENTCORE_SANDBOX_BOOT_FILE", DEFAULT_BOOT_FILE))
    args = parser.parse_args(argv)
    if os.path.commonpath([os.path.realpath(args.workspace), os.path.realpath(args.boot_file)]) == os.path.realpath(args.workspace):
        parser.error("the boot marker must be outside the potentially persisted workspace")
    os.environ["AGENTCORE_SANDBOX_WORKSPACE"] = args.workspace
    os.environ["AGENTCORE_SANDBOX_BOOT_FILE"] = args.boot_file
    initialize_boot_file(args.boot_file)
    adapter = Adapter(args.workspace, args.boot_file)
    server = ThreadingHTTPServer((args.host, args.port), handler_for(adapter))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
