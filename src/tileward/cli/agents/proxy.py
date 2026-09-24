"""The local HTTP proxy `launch claude` and `launch codex` run.

One per `twcli launch`, bound to `127.0.0.1` only, torn down when the child process exits. The
child CLI only ever sees a random per-run bearer token, never the real `tw_live_...` key.

The child owns the terminal and this process shares it, so the proxy never prints. A request the
client abandons is dropped quietly, a failure goes back to the child as its own kind of API error,
and anything unexpected is appended to the error log instead.

An "adapter" is a module with five functions -- `anthropic.py` and `responses.py` both have this
shape, so a third protocol is a third module, not a change here:

    to_chat_request(body: dict, *, model: str) -> dict
    from_chat_response(completion: dict, *, model: str) -> dict
    stream_events(chunks: Iterator[dict], *, model: str) -> Iterator[bytes]
    error_body(exc: Exception) -> tuple[int, dict]
    stream_error_event(exc: Exception) -> bytes  # a mid-stream failure, after headers are sent
    count_tokens(body: dict) -> dict  # optional, routed only if `routes` maps a path to it
    REQUEST_SCHEMA: voluptuous.Schema  # optional, checked against every request body first
    RELAYS_RESPONSE: bool  # optional, True if from_chat_response returns the completion unchanged

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
import socket
import socketserver
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import chain
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

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

# Transient upstream/gateway failures. A request goes upstream as a stream (a non-streaming client
# request is folded from one, see `_fold`), and a stream retries nothing on its own, so the proxy
# is the only line of defence. The one direct call left, for an adapter that relays its response
# as it came, is retried on 5xx by the SDK transport already, so the proxy leaves that to it.
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


def _is_retryable_backend_error(exc: Exception, retry_5xx: bool = True) -> bool:
    status = getattr(exc, "status", None)
    if retry_5xx and status in _RETRYABLE_STATUS:
        return True
    # A truncated request body is never retried by the transport (a 4xx reads as the caller's
    # fault), so the proxy owns this case too.
    if isinstance(status, int) and 400 <= status < 500:
        message = (getattr(exc, "message", "") or "").lower()
        return any(sig in message for sig in _TRUNCATION_SIGNATURES)
    return False


def _with_backend_retry(
    call: Any, *, retry_5xx: bool = True, gone: Optional[Callable[[], bool]] = None
) -> Any:
    """Run `call()`, retrying a transient backend failure a couple of times before giving up.

    `call` must open a FRESH upstream request each time and must not have written anything to the
    client yet -- every proxy path has the upstream answer, or its first chunk, in hand before it
    commits a response, which is what keeps a retry here invisible to the agent on the other end.
    `gone()` says whether that agent has already left: a retry would then only repeat a generation
    nobody will read.
    """
    for backoff in _RETRY_BACKOFFS:
        try:
            return call()
        except errors.TilewardError as exc:
            if not _is_retryable_backend_error(exc, retry_5xx) or (gone is not None and gone()):
                raise
            time.sleep(backoff)
    return call()  # last attempt; its result or its exception is the caller's


# The client gave up on a request: Esc in the child CLI, its own timeout, the child exiting. That
# is routine, and there is nobody left to tell.
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

# Bytes. Past this the log rolls over to `<name>.1`, so a long-lived bug can't fill a disk and the
# newest entries are still the ones kept.
_LOG_CAP = 1_000_000
_LOG_LOCK = threading.Lock()


def _record(path: Optional[Path], text: str) -> None:
    """Append `text` to the error log. Best effort: this runs while another failure is being
    handled, so it must not raise."""
    if path is None:
        return
    try:
        with _LOG_LOCK:
            if path.exists() and path.stat().st_size > _LOG_CAP:
                path.replace(path.with_name(path.name + ".1"))
            path.parent.mkdir(parents=True, exist_ok=True)
            at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{at} {text.rstrip()}\n")
    except OSError:
        pass


def _traceback(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _describe(exc: Exception) -> str:
    """One line for a failure of ours, since the child shows its message; the traceback for
    anything else, which is a bug here."""
    if isinstance(exc, errors.TilewardError):
        return f"{type(exc).__name__}: {exc}"
    return _traceback(exc)


def _fold(chunks: Iterator[Dict[str, Any]], gone: Callable[[], bool]) -> Dict[str, Any]:
    """Fold a chat-completions stream into the one completion a non-streaming caller expects.

    Non-streaming requests are streamed upstream too. A single blocking response stays silent for
    the whole generation, so a long answer runs into a read timeout -- and the retries that repeat
    the generation -- long before it finishes. A stream keeps bytes flowing, and can be dropped the
    moment the client leaves: `gone()` is asked between chunks and raises out of here if so.
    """
    completion: Dict[str, Any] = {}
    text: List[str] = []
    calls: Dict[int, Dict[str, Any]] = {}
    finish_reason: Optional[str] = None
    for chunk in chunks:
        if gone():
            raise BrokenPipeError("the client closed the connection")
        for key in ("id", "model", "usage"):
            if chunk.get(key):
                completion[key] = chunk[key]
        for choice in chunk.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text.append(delta["content"])
            for call in delta.get("tool_calls") or []:
                slot = calls.setdefault(
                    call.get("index", 0),
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                # The id and name come from a call's first fragment, as in `stream_events`; some
                # servers repeat them on every fragment after it.
                slot["id"] = slot["id"] or call.get("id")
                fn = call.get("function") or {}
                slot["function"]["name"] = slot["function"]["name"] or fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""
            finish_reason = choice.get("finish_reason") or finish_reason

    message: Dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        message["tool_calls"] = [calls[i] for i in sorted(calls)]
    completion["choices"] = [
        {"index": 0, "message": message, "finish_reason": finish_reason or "stop"}
    ]
    return completion


def _make_handler(
    *,
    client: Tileward,
    model: str,
    token: str,
    adapters: Dict[str, Tuple[str, ModuleType]],
    error_log: Optional[Path] = None,
) -> type:
    """Build a handler class closed over this launch's client/token and its routes.

    `adapters` maps each path to its route name and the adapter that speaks that path's protocol,
    so one port can serve more than one: Codex's Responses API and opencode's chat completions
    side by side.

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
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                # Keep-alive parsing broke under a client that pipelines a retry right behind a
                # non-2xx response; always closing sidesteps it, and a loopback proxy loses nothing.
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                self.wfile.write(body)
            except _CLIENT_GONE:
                self.close_connection = True

        def _client_gone(self) -> bool:
            """Whether the client has closed its end: a closed socket reads as EOF, while an open
            one has either nothing to read yet or bytes waiting."""
            sock = self.connection
            timeout = sock.gettimeout()
            try:
                sock.settimeout(0)
                try:
                    return sock.recv(1, socket.MSG_PEEK) == b""
                finally:
                    sock.settimeout(timeout)
            except BlockingIOError:
                return False
            except OSError:
                return True

        def _fail(self, adapter: ModuleType, exc: Exception) -> None:
            """Log `exc` and send it as `adapter`'s error."""
            _record(error_log, _describe(exc))
            status, payload = adapter.error_body(exc)
            self._write_json(status, payload)

        def _read_body(self, adapter: ModuleType) -> Dict[str, Any]:
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
            entry = adapters.get(self.path.split("?", 1)[0])
            if entry is None:
                msg = f"twcli launch: no route for {self.path}"
                self._write_json(404, {"error": {"message": msg}})
                return
            route, adapter = entry
            try:
                self._serve(route, adapter)
            except _CLIENT_GONE:
                self.close_connection = True
            except Exception as exc:  # a bug here must not become a traceback on the child's screen
                self._fail(adapter, exc)

        def _serve(self, route: str, adapter: ModuleType) -> None:
            if not self._authorized():
                status, payload = adapter.error_body(
                    errors.AuthenticationError("Bad or missing local proxy token.", status=401)
                )
                self._write_json(status, payload)
                return

            try:
                body = self._read_body(adapter)
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
                self._handle_stream(adapter, messages, chat_body)
            else:
                self._handle_once(adapter, messages, chat_body)

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

        def _start(
            self, adapter: ModuleType, messages: Any, rest: Dict[str, Any]
        ) -> Optional[Tuple[Any, Iterator[Dict[str, Any]]]]:
            """Open the upstream stream and read its first chunk, retrying a transient failure.

            Returns `(stream, events)`, `events` being the stream with that chunk put back in front,
            or None once the client has been answered instead. The stream is lazy: nothing is sent,
            and nothing can fail, until an item is read. Reading the first here lets a failure come
            back as a real status code instead of a dead stream.
            """

            def attempt() -> Tuple[Any, Dict[str, Any]]:
                stream = client.chat.completions.create(
                    messages,
                    model=model,
                    stream=True,
                    stream_options={"include_usage": True},
                    **rest,
                )
                return stream, next(stream)

            try:
                stream, first = _with_backend_retry(attempt, gone=self._client_gone)
            except errors.TilewardError as exc:
                self._fail(adapter, exc)
                return None
            except StopIteration:
                self._write_json(200, adapter.from_chat_response({"choices": []}, model=model))
                return None
            return stream, chain([first], stream)

        def _relay_once(self, adapter: ModuleType, messages: Any, rest: Dict[str, Any]) -> None:
            """A non-streaming request for an adapter that hands the completion back as it came,
            which a completion folded from a stream would not be."""
            try:
                completion = _with_backend_retry(
                    lambda: client.chat.completions.create(
                        messages, model=model, stream=False, **rest
                    ),
                    retry_5xx=False,
                    gone=self._client_gone,
                )
            except errors.TilewardError as exc:
                self._fail(adapter, exc)
                return
            self._write_json(200, adapter.from_chat_response(completion, model=model))

        def _handle_once(self, adapter: ModuleType, messages: Any, rest: Dict[str, Any]) -> None:
            if getattr(adapter, "RELAYS_RESPONSE", False):
                self._relay_once(adapter, messages, rest)
                return
            started = self._start(adapter, messages, rest)
            if started is None:
                return
            stream, events = started
            try:
                completion = _fold(events, self._client_gone)
            except errors.TilewardError as exc:
                self._fail(adapter, exc)
                return
            finally:
                stream.close()  # frees the upstream generation whether it finished or not
            self._write_json(200, adapter.from_chat_response(completion, model=model))

        def _handle_stream(
            self, adapter: ModuleType, messages: Any, rest: Dict[str, Any]
        ) -> None:
            started = self._start(adapter, messages, rest)
            if started is None:
                return
            stream, events = started
            try:
                # No Content-Length up front, so the client can't tell the body ended without a
                # closed connection -- it would otherwise block on the next read forever.
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                try:
                    for piece in adapter.stream_events(events, model=model):
                        self.wfile.write(piece)
                        self.wfile.flush()
                except _CLIENT_GONE:
                    pass
                except Exception as exc:
                    # Headers are already sent, so this can't become a status code -- send the
                    # protocol's own mid-stream error event instead of just dropping the
                    # connection, which otherwise looks identical to a silent hang on the client.
                    _record(error_log, _describe(exc))
                    try:
                        self.wfile.write(adapter.stream_error_event(exc))
                        self.wfile.flush()
                    except OSError:
                        pass  # the client already hung up; nothing left to write to
            finally:
                stream.close()  # stop paying for a generation nobody is reading

    return Handler


class _Server(ThreadingHTTPServer):
    """`ThreadingHTTPServer` without the host name lookup its `server_bind` makes before listening,
    and without the traceback on stderr its `handle_error` prints.

    On macOS the reverse lookup of 127.0.0.1 goes to DNS, where a slow resolver keeps the port
    closed until it answers. Nothing here reads `server_name`, so it holds the address as given.
    """

    def __init__(self, address: Any, handler: Any, error_log: Optional[Path] = None) -> None:
        super().__init__(address, handler)
        self._error_log = error_log

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The default prints a traceback to stderr, which is the terminal the child CLI is drawing
        # on: a client that hangs up mid-request would scatter one across its screen.
        exc = sys.exc_info()[1]
        if exc is not None and not isinstance(exc, _CLIENT_GONE):
            _record(self._error_log, _traceback(exc))


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
        extra_routes: Optional[Dict[str, Tuple[str, ModuleType]]] = None,
        error_log: Optional[Path] = None,
    ) -> None:
        # A launch mints its own. A supervisor that has already written the token into a desktop
        # app's config passes it in, so the proxy can restart without the app losing access.
        self.token = token or secrets.token_urlsafe(24)
        adapters: Dict[str, Tuple[str, ModuleType]] = {
            path: (route, adapter) for path, route in routes.items()
        }
        adapters.update(extra_routes or {})
        handler = _make_handler(
            client=client, model=model, token=self.token, adapters=adapters, error_log=error_log
        )
        self._httpd = _Server(("127.0.0.1", port), handler, error_log)
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
