"""The local HTTP proxy `launch claude` and `launch codex` run.

One per `twcli launch`, bound to `127.0.0.1` only, torn down when the child process exits. It
holds the real Tileward client; the child CLI only ever sees a random per-run bearer token, never
the actual `tw_live_...` key (see `runner.py`).

An "adapter" is just a module with four functions — `anthropic.py` and `responses.py` both have
this exact shape, and nothing here imports either by name, so a third protocol is a third module,
not a change here:

    to_chat_request(body: dict, *, model: str) -> dict   # must include "messages"; may include
                                                          # "stream" (default False), "tools", ...
    from_chat_response(completion: dict, *, model: str) -> dict
    stream_events(chunks: Iterator[dict], *, model: str) -> Iterator[bytes]
    error_body(exc: Exception) -> tuple[int, dict]
    count_tokens(body: dict) -> dict                      # optional; routed only if `routes` maps
                                                          # a path to "count_tokens"

Codex polls `GET /v1/models` for model metadata in the background; this proxy only speaks `POST`
(the stdlib's default 501 answers a GET the same as any other unmapped method), and that's
deliberate -- Codex's models-manager expects its own undocumented `{"models": [...]}` schema, not
Tileward's real `/v1/models` shape, and answering with the wrong shape produced a noisier decode
error than the plain 501 it started as. The warning it logs ("Model metadata ... not found") is
cosmetic: Codex falls back to generic assumptions and the session runs correctly either way.
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
            # Always close rather than keep-alive. A single-purpose loopback proxy has nothing to
            # gain from connection reuse, and a client that pipelines a retry immediately behind a
            # non-2xx response is exactly the case where keep-alive parsing has the least margin
            # for error -- one byte-accounting mismatch turns the next request line into garbage.
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
            # `chunks` is a lazy generator: nothing has happened on the wire yet. Force the first
            # item BEFORE committing to a 200 + SSE response, so an upstream failure (bad key, the
            # model 404ing, a guard refusal that raises) still comes back as a proper status code
            # instead of a stream that opens then silently dies.
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

            # No Content-Length (the length isn't known up front) and no chunked encoding, so
            # under HTTP/1.1 keep-alive the client has no way to know the body ended short of a
            # closed connection -- it would otherwise block on the next read forever. `close`
            # is honest about that instead of pretending this connection can be reused.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for piece in adapter.stream_events(rest_of_stream(), model=model):
                self.wfile.write(piece)
                self.wfile.flush()

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
