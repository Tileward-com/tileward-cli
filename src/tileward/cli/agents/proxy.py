"""The local HTTP proxy `launch claude` and `launch codex` run.

One per `twcli launch`, bound to `127.0.0.1` only, torn down when the child process exits. The
child CLI only ever sees a random per-run bearer token, never the real `tw_live_...` key.

An "adapter" is a module with five functions -- `anthropic.py` and `responses.py` both have this
shape, so a third protocol is a third module, not a change here:

    to_chat_request(body: dict, *, model: str) -> dict
    from_chat_response(completion: dict, *, model: str) -> dict
    stream_events(chunks: Iterator[dict], *, model: str) -> Iterator[bytes]
    error_body(exc: Exception) -> tuple[int, dict]
    stream_error_event(exc: Exception) -> bytes  # a mid-stream failure, after headers are sent
    count_tokens(body: dict) -> dict  # optional, routed only if `routes` maps a path to it

GET is handled only for `/v1/models`, which returns an OpenAI-compatible model list so Codex's
background metadata poll gets a clean response instead of a 501 error.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType
from typing import Any, Dict, Optional

from ... import errors
from ...client import Tileward
from ...resources.models import summarize


def _make_handler(
    *, client: Tileward, adapter: ModuleType, model: str, token: str, routes: Dict[str, str]
) -> type:
    """Build a handler class closed over this launch's client/adapter/token/routes.

    A class rather than an instance: `ThreadingHTTPServer` instantiates the handler itself, once
    per connection, so per-launch state has to live in the closure instead of `__init__` args.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib's signature
            pass  # a request log line per turn would compete with the child CLI's own terminal UI

        def _authorized(self) -> bool:
            auth = self.headers.get("Authorization", "")
            bearer = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
            api_key = self.headers.get("x-api-key", "")
            return secrets.compare_digest(bearer, token) or secrets.compare_digest(api_key, token)

        def _write_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # Keep-alive parsing broke under a client that pipelines a retry right behind a
            # non-2xx response; always closing sidesteps it, and a loopback proxy loses nothing.
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(body)

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def do_POST(self) -> None:  # noqa: N802 - required name in BaseHTTPRequestHandler
            route = routes.get(self.path.split("?", 1)[0])
            if route is None:
                msg = f"twcli launch: no route for {self.path}"
                self._write_json(404, {"error": {"message": msg}})
                return
            if not self._authorized():
                status, payload = adapter.error_body(
                    errors.AuthenticationError("Bad or missing local proxy token.", status=401)
                )
                self._write_json(status, payload)
                return

            body = self._read_json()
            if route == "count_tokens":
                self._write_json(200, adapter.count_tokens(body))
                return

            try:
                chat_body = adapter.to_chat_request(body, model=model)
            except Exception as exc:  # a malformed request from the CLI, not a Tileward failure
                self._write_json(400, {"error": {"message": f"could not translate request: {exc}"}})
                return

            messages = chat_body.pop("messages")
            stream = bool(chat_body.pop("stream", False))
            if stream:
                self._handle_stream(messages, chat_body)
            else:
                self._handle_once(messages, chat_body)

        def do_GET(self) -> None:  # noqa: N802 - required name in BaseHTTPRequestHandler
            if self.path.split("?", 1)[0] == "/v1/models":
                self._handle_get_models()
            else:
                self._write_json(404, {
                    "error": {"message": f"twcli launch: no route for {self.path}"}
                })

        def _handle_get_models(self) -> None:
            try:
                rows = client.models.list()
            except errors.TilewardError as exc:
                self._write_json(502, {"error": {"message": f"upstream failed: {exc}"}})
                return
            self._write_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": row.get("id"),
                            "object": "model",
                            "owned_by": "tileward",
                            **summarize(row),
                        }
                        for row in rows
                        if row.get("id")
                    ],
                },
            )

        def _handle_once(self, messages: Any, rest: Dict[str, Any]) -> None:
            try:
                completion = client.chat.completions.create(
                    messages, model=model, stream=False, **rest
                )
            except errors.TilewardError as exc:
                status, payload = adapter.error_body(exc)
                self._write_json(status, payload)
                return
            self._write_json(200, adapter.from_chat_response(completion, model=model))

        def _handle_stream(self, messages: Any, rest: Dict[str, Any]) -> None:
            chunks = client.chat.completions.create(
                messages, model=model, stream=True, stream_options={"include_usage": True}, **rest
            )
            # `chunks` is a lazy generator; force the first item before committing to 200 + SSE,
            # so an upstream failure comes back as a real status code instead of a dead stream.
            try:
                first = next(chunks)
            except errors.TilewardError as exc:
                status, payload = adapter.error_body(exc)
                self._write_json(status, payload)
                return
            except StopIteration:
                self._write_json(200, adapter.from_chat_response({"choices": []}, model=model))
                return

            def rest_of_stream():
                yield first
                yield from chunks

            # No Content-Length up front, so the client can't tell the body ended without a
            # closed connection -- it would otherwise block on the next read forever.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                for piece in adapter.stream_events(rest_of_stream(), model=model):
                    self.wfile.write(piece)
                    self.wfile.flush()
            except errors.TilewardError as exc:
                # Headers are already sent, so this can't become a status code -- send the
                # protocol's own mid-stream error event instead of just dropping the connection,
                # which otherwise looks identical to a silent hang on the client side.
                try:
                    self.wfile.write(adapter.stream_error_event(exc))
                    self.wfile.flush()
                except OSError:
                    pass  # the client already hung up; nothing left to write to

    return Handler


class Proxy:
    """Lifecycle wrapper: construct, `.start()`, read `.base_url`/`.token`, `.stop()` when done."""

    def __init__(
        self,
        *,
        client: Tileward,
        adapter: ModuleType,
        model: str,
        routes: Dict[str, str],
        port: int = 0,
    ) -> None:
        self.token = secrets.token_urlsafe(24)
        handler = _make_handler(
            client=client, adapter=adapter, model=model, token=self.token, routes=routes
        )
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
