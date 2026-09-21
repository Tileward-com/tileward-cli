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
    REQUEST_SCHEMA: voluptuous.Schema  # optional, checked against every request body first

A request that is not a JSON object of the shape the adapter reads is answered with a 400 in the
adapter's own error format, before anything is translated, counted or sent upstream.

GET is handled only for `/v1/models`, which returns an OpenAI-compatible model list so Codex's
background metadata poll gets a clean response instead of a 501 error.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType
from typing import Any, Dict, Optional

import voluptuous as vol

from ... import errors
from ...client import Tileward
from ...resources.models import summarize

# A coding agent resends its whole context every turn, so a launch's requests get large fast. When
# the bytes of one are truncated on the hop between the Tileward gateway and the model host -- a
# transient transport failure -- the model host's JSON parser rejects what arrived and the gateway
# relays that as a 4xx. It reads like the caller's fault (a bad request), but twcli only ever sends
# well-formed JSON, so an upstream "malformed JSON" verdict can only mean a body that did not arrive
# whole. Left alone it ends the agent's session mid-task; retried, the same request almost always
# goes straight through. These are JSON parsers' own words for "the input stopped early", matched
# case-insensitively as a substring of the upstream error message.
_TRUNCATION_SIGNATURES = (
    "unterminated string",
    "expecting value",
    "expecting ',' delimiter",
    "expecting ':' delimiter",
    "expecting property name",
    "extra data",
    "invalid control character",
    "eof while parsing",
    "unexpected end",
    "json decode",
)

# Transient upstream/gateway failures. `retry_5xx` is on ONLY for the streaming path: the SDK
# transport already retries these statuses on the non-streaming path (see `_http.RETRY_STATUS`),
# and retrying them here too would multiply the two budgets together; the streaming path retries
# nothing on its own, so there the proxy is the only line of defence.
_RETRYABLE_STATUS = frozenset({500, 502, 503, 504})

_RETRY_BACKOFFS = (0.2, 0.6)  # two retries, three attempts in all

# The largest request body the proxy will read. A coding agent resends its whole context every turn,
# so real requests run to megabytes; this only keeps a bogus Content-Length from sizing the read.
_MAX_BODY_BYTES = 128 * 1024 * 1024

# Digits only. `int()` also takes a sign, underscores and non-ASCII digits; HTTP takes none of them.
_CONTENT_LENGTH = vol.Schema(vol.All(vol.Match(r"[0-9]{1,15}\Z"), vol.Coerce(int)))


def _where(exc: vol.Invalid) -> str:
    """`messages[2].content: ` -- where in the request the first problem is, or nothing."""
    path = ""
    for key in exc.path:
        path += f"[{key}]" if isinstance(key, int) else (f".{key}" if path else str(key))
    return f"{path}: " if path else ""


def _is_retryable_backend_error(exc: Exception, retry_5xx: bool) -> bool:
    status = getattr(exc, "status", None)
    if retry_5xx and status in _RETRYABLE_STATUS:
        return True
    # A truncated request body is never retried by the transport (a 4xx reads as the caller's
    # fault), so the proxy always owns this case -- on both paths, with no double-retry risk.
    if isinstance(status, int) and 400 <= status < 500:
        message = (getattr(exc, "message", "") or "").lower()
        return any(sig in message for sig in _TRUNCATION_SIGNATURES)
    return False


def _with_backend_retry(call: Any, *, retry_5xx: bool) -> Any:
    """Run `call()`, retrying a transient backend failure a couple of times before giving up.

    `call` must open a FRESH upstream request each time and must not have written anything to the
    client yet -- both proxy handlers force the first upstream byte before committing a response,
    which is what keeps a retry here invisible to the agent on the other end.
    """
    for backoff in _RETRY_BACKOFFS:
        try:
            return call()
        except errors.TilewardError as exc:
            if not _is_retryable_backend_error(exc, retry_5xx):
                raise
            time.sleep(backoff)
    return call()  # last attempt; its result or its exception is the caller's


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

        def _read_body(self) -> Dict[str, Any]:
            """The request body as a dict of the shape the adapter reads, or an `APIError` naming
            what is wrong with it. Nothing is read past a Content-Length that is not believable."""
            try:
                length = _CONTENT_LENGTH(self.headers.get("Content-Length", "0"))
            except vol.Invalid:
                raise errors.APIError(
                    "Content-Length must be a whole number of bytes.", status=400
                ) from None
            if length > _MAX_BODY_BYTES:
                raise errors.APIError("Request body is too large.", status=413)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                raise errors.APIError("Request body is empty.", status=400)
            try:
                parsed = json.loads(raw)
            except RecursionError:  # not a ValueError, so a deeply nested body needs its own arm
                raise errors.APIError("Request body is nested too deeply.", status=400) from None
            except ValueError:
                raise errors.APIError("Request body is not valid JSON.", status=400) from None
            if not isinstance(parsed, dict):
                raise errors.APIError("Request body must be a JSON object.", status=400)
            schema = getattr(adapter, "REQUEST_SCHEMA", None)
            if schema is None:
                return parsed
            try:
                return schema(parsed)
            except vol.Invalid as exc:
                message = f"Invalid request: {_where(exc)}{exc.error_message}."
                raise errors.APIError(message, status=400) from None

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

            try:
                body = self._read_body()
            except errors.APIError as exc:
                status, payload = adapter.error_body(exc)
                self._write_json(status, payload)
                return
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
                completion = _with_backend_retry(
                    lambda: client.chat.completions.create(
                        messages, model=model, stream=False, **rest
                    ),
                    retry_5xx=False,  # the transport already retries 5xx on this path
                )
            except errors.TilewardError as exc:
                status, payload = adapter.error_body(exc)
                self._write_json(status, payload)
                return
            self._write_json(200, adapter.from_chat_response(completion, model=model))

        def _handle_stream(self, messages: Any, rest: Dict[str, Any]) -> None:
            def _open():
                chunks = client.chat.completions.create(
                    messages, model=model, stream=True,
                    stream_options={"include_usage": True}, **rest
                )
                # `chunks` is a lazy generator; force the first item before committing to 200 +
                # SSE, so an upstream failure comes back as a real status code instead of a dead
                # stream -- and so a retry re-opens the whole request rather than resuming a
                # half-read one.
                return chunks, next(chunks)

            try:
                chunks, first = _with_backend_retry(_open, retry_5xx=True)
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
        token: Optional[str] = None,
    ) -> None:
        # A launch mints its own. A supervisor that has already written the token into a desktop
        # app's config passes it in, so the proxy can restart without the app losing access.
        self.token = token or secrets.token_urlsafe(24)
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


def run_until_stopped(
    proxy: Proxy,
    on_ready: Any,
    stop: Optional[threading.Event] = None,
    watch_parent: bool = True,
) -> None:
    """Start `proxy`, then block until SIGTERM, SIGINT, `stop`, or its parent exiting.

    Watching the parent is what keeps a supervisor's crash from leaving an orphan: a desktop app
    that is force-quit cannot stop its proxy, and an orphan would hold the port so the app's next
    launch could never bind it. When the process that started this one goes away, this one is
    reparented and its parent pid changes, so it stops too.
    """
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    if watch_parent:
        parent = os.getppid()

        def watch() -> None:
            while not stop.wait(1.0):
                if os.getppid() != parent:
                    stop.set()

        threading.Thread(target=watch, daemon=True).start()
    proxy.start()
    on_ready()
    try:
        stop.wait()
    finally:
        proxy.stop()
