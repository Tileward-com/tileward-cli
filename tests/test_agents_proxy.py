"""The real `ThreadingHTTPServer`, driven with `httpx`, against a mocked Tileward backend.

The outbound leg is mocked with `httpx.MockTransport` (not `respx`'s global monkeypatch, which
intercepts this file's own calls to the local proxy too)."""

from __future__ import annotations

import http.client
import json
import socket
import struct
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from tileward.cli.agents import anthropic, responses
from tileward.cli.agents import proxy as proxy_mod
from tileward.cli.agents.proxy import Proxy
from tileward.client import Tileward


def make_client(handler):
    """A Tileward client whose outbound HTTP is a plain function, not a real socket."""
    calls = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    client = Tileward(
        api_key="tw_live_testkey",
        base_url="https://api.test",
        load_config=False,
        http_client=httpx.Client(transport=transport),
    )
    return client, calls


def start_anthropic_proxy(handler, *, adapter=anthropic, error_log=None):
    client, calls = make_client(handler)
    proxy = Proxy(
        client=client,
        adapter=adapter,
        model="tileward-35b-a3b",
        routes={"/v1/messages": "messages", "/v1/messages/count_tokens": "count_tokens"},
        error_log=error_log,
    )
    proxy.start()
    return proxy, calls


def start_responses_proxy(handler):
    client, calls = make_client(handler)
    proxy = Proxy(
        client=client, adapter=responses, model="gpt-oss-20b", routes={"/v1/responses": "responses"}
    )
    proxy.start()
    return proxy, calls


def raw_post(proxy, path, headers, body=b""):
    """POST with the headers exactly as given. httpx will not send an invalid Content-Length, and
    the timeout makes a proxy that hangs on one fail this test instead of stalling the suite."""
    conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
    try:
        conn.putrequest("POST", path)
        conn.putheader("Authorization", f"Bearer {proxy.token}")
        for name, value in headers.items():
            conn.putheader(name, value)
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected outbound call: {request.url}")


def data(chunk: dict) -> bytes:
    return f"data: {json.dumps(chunk)}\n\n".encode()


def sse_response(*chunks: dict) -> httpx.Response:
    """An upstream streamed completion: one `data:` line per chunk, then `[DONE]`."""
    body = b"".join(data(c) for c in chunks) + b"data: [DONE]\n\n"
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


class Scripted(httpx.SyncByteStream):
    """An upstream response body that yields `parts` in order, and notes when it is let go of.

    `park` runs just before part number `park_at`, which is how a test holds the upstream
    mid-answer while the client goes away."""

    def __init__(self, parts, *, pause=0.0, park_at=None, park=None):
        self.parts = parts
        self.pause = pause
        self.park_at = park_at
        self.park = park
        self.closed = threading.Event()

    def __iter__(self):
        for i, part in enumerate(self.parts):
            if i == self.park_at and self.park is not None:
                self.park()
            time.sleep(self.pause)
            yield part

    def close(self):
        self.closed.set()


def event_stream(body: Scripted) -> httpx.Response:
    return httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"})


def open_raw(proxy: Proxy, body: dict, path: str = "/v1/messages") -> socket.socket:
    """Send one request over a bare socket, so the test decides when the client leaves."""
    payload = json.dumps(body).encode()
    head = (
        f"POST {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {proxy.token}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n"
    )
    sock = socket.create_connection(("127.0.0.1", proxy.port))
    sock.sendall(head.encode() + payload)
    return sock


def hang_up(sock: socket.socket) -> None:
    """Close the way an aborted request does: with a reset, so the proxy's next write fails at
    once instead of depending on how quickly the kernel notices a plain close."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()
    time.sleep(0.2)  # let the reset land before the proxy touches the socket again


HI = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}


def test_wrong_token_is_rejected_before_touching_tileward():
    proxy, calls = start_anthropic_proxy(never_called)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            headers={"x-api-key": "not-the-token"},
            json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 16},
        )
        assert r.status_code == 401
        assert r.json()["type"] == "error"
        assert calls == []
    finally:
        proxy.stop()


def test_missing_token_is_rejected():
    proxy, _calls = start_anthropic_proxy(never_called)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 16},
        )
        assert r.status_code == 401
    finally:
        proxy.stop()


def test_unknown_path_is_404():
    proxy, _calls = start_anthropic_proxy(never_called)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/nope",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={},
        )
        assert r.status_code == 404
    finally:
        proxy.stop()


def test_get_models_returns_openai_shape():
    """GET /v1/models returns an OpenAI-compatible model list so Codex's metadata poll
    gets a clean response instead of a 501 error."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "tileward-35b-a3b",
                        "object": "model",
                        "owned_by": "tileward",
                        "tileward": {"context_len": 131072, "price_per_mtoken_usd": 0.0},
                    }
                ],
            },
        )

    proxy, _calls = start_anthropic_proxy(handler)
    try:
        r = httpx.get(f"{proxy.base_url}/v1/models")
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "list"
        assert isinstance(body["data"], list)
        assert len(body["data"]) > 0
        entry = body["data"][0]
        assert entry["object"] == "model"
        assert entry["owned_by"] == "tileward"
        assert entry["id"] == "tileward-35b-a3b"
        # Nested tileward fields are flattened into the top-level shape
        assert entry["context_len"] == 131072
    finally:
        proxy.stop()


def test_get_unknown_path_returns_404():
    """GET to an unmapped path returns 404, not a crash."""
    proxy, _calls = start_anthropic_proxy(never_called)
    try:
        r = httpx.get(f"{proxy.base_url}/v1/nope")
        assert r.status_code == 404
    finally:
        proxy.stop()


def test_count_tokens_needs_no_outbound_call():
    proxy, calls = start_anthropic_proxy(never_called)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages/count_tokens",
            headers={"x-api-key": proxy.token},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        assert r.json()["input_tokens"] >= 1
        assert calls == []
    finally:
        proxy.stop()


def test_non_streaming_translate_round_trip():
    """A non-streaming client request is streamed upstream and folded back into one message."""

    def handler(request):
        return sse_response(
            {
                "id": "chatcmpl-1",
                "model": "tileward-35b-a3b",
                "choices": [{"delta": {"content": "Hel"}}],
            },
            {"choices": [{"delta": {"content": "lo!"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        )

    proxy, calls = start_anthropic_proxy(handler)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json=HI,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["type"] == "message"
        assert body["id"] == "chatcmpl-1"
        assert body["content"] == [{"type": "text", "text": "Hello!"}]
        assert body["stop_reason"] == "end_turn"
        assert body["usage"] == {"input_tokens": 5, "output_tokens": 2}
        assert len(calls) == 1
        sent = json.loads(calls[0].content)
        assert sent["model"] == "tileward-35b-a3b"
        # A blocking upstream call stays silent for the whole generation and hits read timeouts.
        assert sent["stream"] is True
    finally:
        proxy.stop()


def test_upstream_401_becomes_a_proper_status_before_any_bytes_are_sent():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    proxy, _calls = start_anthropic_proxy(handler)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
                "stream": True,
            },
        )
        assert r.status_code == 401
        assert r.json()["type"] == "error"
    finally:
        proxy.stop()


def test_mid_stream_failure_sends_an_error_event_instead_of_a_silent_hang():
    """A network failure partway through a stream (timeout, connection reset) used to propagate
    unhandled out of the proxy, dropping the connection with no signal -- indistinguishable from
    a hang on the client side. It should send the protocol's own error event instead."""

    def failing_iter():
        yield b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
        raise httpx.ReadError("connection reset")

    def handler(request):
        return httpx.Response(
            200, content=failing_iter(), headers={"content-type": "text/event-stream"}
        )

    proxy, _calls = start_anthropic_proxy(handler)
    try:
        with httpx.stream(
            "POST",
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
                "stream": True,
            },
        ) as r:
            assert r.status_code == 200
            text = b"".join(r.iter_bytes()).decode("utf-8", errors="replace")
        assert '"text": "Hi"' in text  # the content that did arrive is not lost
        assert "event: error" in text
        assert "connection reset" in text
    finally:
        proxy.stop()


def test_streaming_forwards_sse_with_the_right_content_type():
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
        b"data: [DONE]\n\n"
    )

    def handler(request):
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    proxy, _calls = start_anthropic_proxy(handler)
    try:
        with httpx.stream(
            "POST",
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
                "stream": True,
            },
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"] == "text/event-stream"
            text = b"".join(r.iter_bytes()).decode("utf-8")
        assert "event: message_start" in text
        assert "event: message_stop" in text
        assert '"text": "Hi"' in text
    finally:
        proxy.stop()


def test_responses_proxy_non_streaming_round_trip():
    def handler(request):
        return sse_response(
            {"choices": [{"delta": {"content": "Hi!"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {}},
        )

    proxy, _calls = start_responses_proxy(handler)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/responses",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={"input": "hi"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["object"] == "response"
        assert body["status"] == "completed"
        assert body["output"][0]["content"][0]["text"] == "Hi!"
    finally:
        proxy.stop()


def test_responses_proxy_mid_stream_failure_sends_a_response_failed_event():
    def failing_iter():
        yield b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
        raise httpx.ReadError("connection reset")

    def handler(request):
        return httpx.Response(
            200, content=failing_iter(), headers={"content-type": "text/event-stream"}
        )

    proxy, _calls = start_responses_proxy(handler)
    try:
        with httpx.stream(
            "POST",
            f"{proxy.base_url}/v1/responses",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={"input": "hi", "stream": True},
        ) as r:
            assert r.status_code == 200
            text = b"".join(r.iter_bytes()).decode("utf-8", errors="replace")
        assert "event: response.failed" in text
        assert "connection reset" in text
    finally:
        proxy.stop()


def test_a_body_that_is_not_json_is_refused_before_any_outbound_call():
    proxy, calls = start_anthropic_proxy(never_called)
    try:
        status, payload = raw_post(proxy, "/v1/messages", {"Content-Length": "8"}, b"not json")
        assert status == 400
        assert payload["error"]["type"] == "invalid_request_error"
        assert "not valid JSON" in payload["error"]["message"]
        assert calls == []
    finally:
        proxy.stop()


@pytest.mark.parametrize(
    "body",
    [b"[]", b'"text"', b"5", b"null", b"", b"[" * 100_000],
    ids=["array", "string", "number", "null", "empty", "nested-too-deep"],
)
def test_a_body_that_is_not_a_json_object_is_refused(body):
    proxy, calls = start_anthropic_proxy(never_called)
    try:
        status, payload = raw_post(proxy, "/v1/messages", {"Content-Length": str(len(body))}, body)
        assert status == 400
        assert payload["error"]["type"] == "invalid_request_error"
        assert calls == []
    finally:
        proxy.stop()


@pytest.mark.parametrize("length", ["abc", "-1", "1.5", "1e3", "+5", "1_0", "0x10", ""])
def test_a_content_length_that_is_not_a_whole_number_is_refused(length):
    proxy, calls = start_anthropic_proxy(never_called)
    try:
        status, payload = raw_post(proxy, "/v1/messages", {"Content-Length": length}, b"{}")
        assert status == 400
        assert "Content-Length" in payload["error"]["message"]
        assert calls == []
    finally:
        proxy.stop()


def test_a_content_length_beyond_the_limit_is_refused_without_reading_it():
    proxy, calls = start_anthropic_proxy(never_called)
    try:
        status, payload = raw_post(proxy, "/v1/messages", {"Content-Length": str(2**40)})
        assert status == 413
        assert payload["error"]["type"] == "invalid_request_error"
        assert calls == []
    finally:
        proxy.stop()


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages/count_tokens"])
@pytest.mark.parametrize(
    ("body", "where"),
    [
        ({"messages": 5}, "messages"),
        ({"messages": ["x"]}, "messages[0]"),
        ({"messages": [{"role": "user", "content": 5}]}, "messages[0].content"),
        ({"system": 5}, "system"),
        ({"tools": 5}, "tools"),
        ({"stream": "false"}, "stream"),
    ],
    ids=["messages", "message", "content", "system", "tools", "stream"],
)
def test_a_messages_request_of_the_wrong_shape_is_refused_naming_the_field(path, body, where):
    proxy, calls = start_anthropic_proxy(never_called)
    try:
        r = httpx.post(f"{proxy.base_url}{path}", headers={"x-api-key": proxy.token}, json=body)
        assert r.status_code == 400
        assert r.json()["type"] == "error"
        assert r.json()["error"]["type"] == "invalid_request_error"
        assert where in r.json()["error"]["message"]
        assert calls == []
    finally:
        proxy.stop()


@pytest.mark.parametrize(
    ("body", "where"),
    [
        ({"input": 5}, "input"),
        ({"instructions": 5}, "instructions"),
        ({"tools": 5}, "tools"),
        ({"stream": "false"}, "stream"),
    ],
    ids=["input", "instructions", "tools", "stream"],
)
def test_a_responses_request_of_the_wrong_shape_is_refused_naming_the_field(body, where):
    proxy, calls = start_responses_proxy(never_called)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/responses",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json=body,
        )
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"
        assert where in r.json()["error"]["message"]
        assert calls == []
    finally:
        proxy.stop()


def test_shapes_real_clients_send_still_pass_validation():
    """Only the shape the translators index into is checked. Everything else a client sends, and
    the nulls some send for fields they leave unset, rides along untouched."""

    def handler(request):
        choice = {"finish_reason": "stop", "message": {"content": "ok"}}
        return httpx.Response(200, json={"choices": [choice], "usage": {}})

    full = {
        "model": "claude-sonnet",
        "max_tokens": 64,
        "system": [{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "ls", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}],
            },
            {"role": "user", "content": None, "name": "an extra key"},
        ],
        "tools": [{"name": "ls", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "auto"},
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "metadata": {"user_id": "u"},
    }
    unset = {"system": None, "messages": None, "tools": None, "stream": None}

    proxy, calls = start_anthropic_proxy(handler)
    try:
        for body in (full, unset):
            r = httpx.post(
                f"{proxy.base_url}/v1/messages", headers={"x-api-key": proxy.token}, json=body
            )
            assert r.status_code == 200, r.text
        assert len(calls) == 2
    finally:
        proxy.stop()


def test_once_retries_a_truncated_backend_body(monkeypatch):
    """A backend that received a truncated request body answers 4xx with a JSON-decode message.
    twcli only ever sends valid JSON, so that verdict is a transient transport failure, not the
    caller's fault -- the proxy retries it instead of ending the session."""
    monkeypatch.setattr(proxy_mod, "_RETRY_BACKOFFS", (0.0, 0.0))
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(400, json={"error": {
                "message": "Unterminated string starting at: line 1 column 9 (char 8)",
                "type": "invalid_request_error", "code": None}})
        return sse_response({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})

    proxy, calls = start_responses_proxy(handler)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/responses",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={"input": "hi"},
        )
        assert r.status_code == 200
        assert r.json()["output"][0]["content"][0]["text"] == "ok"
        assert len(calls) == 2  # failed once, retried, succeeded
    finally:
        proxy.stop()


def test_stream_retries_a_truncated_backend_body(monkeypatch):
    monkeypatch.setattr(proxy_mod, "_RETRY_BACKOFFS", (0.0, 0.0))
    sse = (
        b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(400, json={"error": {
                "message": "Unterminated string starting at: line 1 column 9 (char 8)"}})
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    proxy, calls = start_responses_proxy(handler)
    try:
        with httpx.stream(
            "POST",
            f"{proxy.base_url}/v1/responses",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={"input": "hi", "stream": True},
        ) as r:
            assert r.status_code == 200
            text = b"".join(r.iter_bytes()).decode("utf-8")
        assert "Hi" in text  # the retried attempt streamed through
        assert len(calls) == 2
    finally:
        proxy.stop()


def test_a_genuine_client_4xx_is_surfaced_not_retried(monkeypatch):
    """A real bad request -- an over-length prompt, say -- is the caller's to fix. Its 4xx has no
    JSON-decode signature, so it is surfaced on the first try, never retried."""
    monkeypatch.setattr(proxy_mod, "_RETRY_BACKOFFS", (0.0, 0.0))

    def handler(request):
        return httpx.Response(400, json={"error": {
            "message": "This model's maximum context length is 262144 tokens.",
            "type": "invalid_request_error", "code": "context_length_exceeded"}})

    proxy, calls = start_responses_proxy(handler)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/responses",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={"input": "hi"},
        )
        assert r.status_code == 400
        assert len(calls) == 1
    finally:
        proxy.stop()


def test_stream_retries_a_transient_5xx(monkeypatch):
    """The streaming path retries a transient upstream 5xx; the SDK transport carries its
    RETRY_STATUS budget only on the non-streaming request() path, so here the proxy is the only
    line of defence."""
    monkeypatch.setattr(proxy_mod, "_RETRY_BACKOFFS", (0.0, 0.0))
    sse = b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\ndata: [DONE]\n\n'
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, json={"error": {"message": "backend waking"}})
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    proxy, calls = start_responses_proxy(handler)
    try:
        with httpx.stream(
            "POST",
            f"{proxy.base_url}/v1/responses",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={"input": "hi", "stream": True},
        ) as r:
            assert r.status_code == 200
            b"".join(r.iter_bytes())
        assert len(calls) == 2
    finally:
        proxy.stop()


def test_once_retries_a_transient_5xx(monkeypatch):
    """A non-streaming client request goes upstream as a stream, and a stream carries no retry of
    its own, so the proxy retries the transient failure the SDK transport used to absorb."""
    monkeypatch.setattr(proxy_mod, "_RETRY_BACKOFFS", (0.0, 0.0))
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(503, json={"error": {"message": "backend waking"}})
        return sse_response({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})

    proxy, calls = start_responses_proxy(handler)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/responses",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={"input": "hi"},
        )
        assert r.status_code == 200
        assert r.json()["output"][0]["content"][0]["text"] == "ok"
        assert len(calls) == 2
    finally:
        proxy.stop()


def test_client_leaving_mid_request_prints_nothing_and_frees_the_upstream(capfd):
    """Esc in the child CLI aborts its request. The proxy then answered into a dead socket, and
    `socketserver` printed the traceback across the terminal the child was drawing on."""
    started, release = threading.Event(), threading.Event()
    body = Scripted(
        [
            data({"choices": [{"delta": {"content": "Hi"}}]}),
            data({"choices": [{"delta": {"content": "!"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n\n",
        ],
        park_at=1,
        park=lambda: (started.set(), release.wait(5)),
    )

    def handler(request):
        if json.loads(request.content).get("stream"):
            return event_stream(body)
        started.set()
        release.wait(5)
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "Hi"}}]}
        )

    proxy, _calls = start_anthropic_proxy(handler)
    try:
        sock = open_raw(proxy, HI)
        assert started.wait(3)
        hang_up(sock)
        release.set()
        time.sleep(0.3)
        assert capfd.readouterr().err == ""
        assert body.closed.wait(3), "the upstream generation was left running"
    finally:
        proxy.stop()


def test_a_long_answer_is_dropped_the_moment_the_client_leaves(capfd):
    """A non-streaming request is folded from an upstream stream. Once the client is gone the rest
    of the answer is for nobody, so the proxy stops reading it instead of waiting it out."""
    body = Scripted([data({"choices": [{"delta": {"content": "tok"}}]})] * 1000, pause=0.01)
    proxy, _calls = start_anthropic_proxy(lambda request: event_stream(body))
    try:
        sock = open_raw(proxy, HI)
        time.sleep(0.3)  # the upstream is mid-answer and the client is still waiting on it
        hang_up(sock)
        assert body.closed.wait(2), "the answer was read to its end for a client that had left"
        assert capfd.readouterr().err == ""
    finally:
        proxy.stop()


def test_client_leaving_mid_stream_prints_nothing_and_frees_the_upstream(capfd):
    body = Scripted([data({"choices": [{"delta": {"content": "tok"}}]})] * 1000, pause=0.01)
    proxy, _calls = start_anthropic_proxy(lambda request: event_stream(body))
    try:
        sock = open_raw(proxy, {**HI, "stream": True})
        sock.settimeout(3)
        seen = b""
        while b"content_block_delta" not in seen:
            seen += sock.recv(4096)
        hang_up(sock)
        time.sleep(0.3)
        assert capfd.readouterr().err == ""
        assert body.closed.wait(3), "the upstream generation was left running"
    finally:
        proxy.stop()


def wait_until_idle(baseline: int, timeout: float = 8.0) -> None:
    """Block until the proxy's request threads have finished, however long a path takes."""
    deadline = time.time() + timeout
    while threading.active_count() > baseline and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.1)  # let a traceback that is about to print, print


@pytest.mark.parametrize("stream", [False, True])
def test_an_upstream_failure_after_the_client_left_prints_nothing(stream, capfd):
    """The chain in the original report: the upstream times out, the proxy goes to send that
    error, and the client is long gone -- so the error write itself broke."""
    release = threading.Event()

    def handler(request):
        release.wait(5)
        raise httpx.ReadTimeout("The read operation timed out")

    proxy, _calls = start_anthropic_proxy(handler)
    try:
        baseline = threading.active_count()
        sock = open_raw(proxy, {**HI, "stream": stream})
        time.sleep(0.2)  # the request is in flight
        hang_up(sock)
        release.set()
        wait_until_idle(baseline)
    finally:
        proxy.stop()
    assert capfd.readouterr().err == ""


@pytest.mark.parametrize("stream", [False, True])
def test_a_transient_failure_is_not_retried_for_a_client_that_left(stream, capfd, monkeypatch):
    """A retry repeats the whole generation. With nobody left to read it, that is only cost."""
    monkeypatch.setattr(proxy_mod, "_RETRY_BACKOFFS", (0.0, 0.0))
    release = threading.Event()

    def handler(request):
        release.wait(5)
        return httpx.Response(503, json={"error": {"message": "backend waking"}})

    proxy, calls = start_anthropic_proxy(handler)
    try:
        baseline = threading.active_count()
        sock = open_raw(proxy, {**HI, "stream": stream})
        time.sleep(0.2)  # the request is in flight
        hang_up(sock)
        release.set()
        wait_until_idle(baseline)
    finally:
        proxy.stop()
    assert len(calls) == 1
    assert capfd.readouterr().err == ""


def test_a_connection_reset_before_the_request_is_read_prints_nothing(capfd):
    proxy, _calls = start_anthropic_proxy(never_called)
    try:
        sock = socket.create_connection(("127.0.0.1", proxy.port))
        sock.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: local")
        hang_up(sock)
        time.sleep(0.3)
    finally:
        proxy.stop()
    assert capfd.readouterr().err == ""


def test_a_patient_client_is_not_mistaken_for_one_that_left():
    """The proxy asks whether the client is still there between upstream chunks; a client that is
    just waiting for the answer must never read as gone."""
    parts = [data({"choices": [{"delta": {"content": c}}]}) for c in "abcdef"]
    parts += [data({"choices": [{"delta": {}, "finish_reason": "stop"}]}), b"data: [DONE]\n\n"]
    body = Scripted(parts, pause=0.05)
    proxy, _calls = start_anthropic_proxy(lambda request: event_stream(body))
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json=HI,
        )
        assert r.status_code == 200
        assert r.json()["content"] == [{"type": "text", "text": "abcdef"}]
    finally:
        proxy.stop()


@pytest.mark.parametrize("stream", [False, True])
def test_upstream_timeout_is_one_attempt_and_a_clean_error(stream, capfd):
    """A timed-out generation is not retried: the retry repeats the whole generation and triples
    the wait before the client hears anything."""

    def handler(request):
        raise httpx.ReadTimeout("The read operation timed out")

    proxy, calls = start_anthropic_proxy(handler)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={**HI, "stream": stream},
        )
    finally:
        proxy.stop()
    assert r.status_code == 500
    assert r.json()["error"]["type"] == "api_error"
    assert "timed out" in r.json()["error"]["message"]
    assert len(calls) == 1
    assert capfd.readouterr().err == ""


def call_chunk(index, *, id=None, name=None, arguments=""):
    """One streamed fragment of tool call number `index`; only its first carries the id and name."""
    fn = {"arguments": arguments}
    if name:
        fn["name"] = name
    call = {"index": index, "function": fn}
    if id:
        call.update(id=id, type="function")
    return {"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]}


def test_non_streaming_folds_streamed_tool_calls():
    def handler(request):
        return sse_response(
            {
                "id": "chatcmpl-9",
                "model": "tileward-35b-a3b",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Looking."}}],
            },
            call_chunk(0, id="call_a", name="Read", arguments='{"file_'),
            call_chunk(0, id="call_a", name="Read", arguments='path": "/x"}'),  # repeats id + name
            call_chunk(1, id="call_b", name="Glob", arguments='{"pattern": "*.py"}'),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 12}},
        )

    proxy, _calls = start_anthropic_proxy(handler)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json=HI,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["content"] == [
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "call_a", "name": "Read", "input": {"file_path": "/x"}},
            {"type": "tool_use", "id": "call_b", "name": "Glob", "input": {"pattern": "*.py"}},
        ]
        assert body["stop_reason"] == "tool_use"
        assert body["usage"] == {"input_tokens": 40, "output_tokens": 12}
    finally:
        proxy.stop()


def test_non_streaming_empty_upstream_stream_is_an_empty_message():
    proxy, _calls = start_anthropic_proxy(lambda request: sse_response())
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json=HI,
        )
        assert r.status_code == 200
        assert r.json()["content"] == []
    finally:
        proxy.stop()


def adapter_with(**overrides):
    """The anthropic adapter with some functions swapped for ones that fail, standing in for a
    bug in the translation."""
    names = (
        "to_chat_request",
        "from_chat_response",
        "stream_events",
        "error_body",
        "stream_error_event",
        "count_tokens",
    )
    fns = {name: getattr(anthropic, name) for name in names}
    fns.update(overrides)
    return SimpleNamespace(**fns)


def test_a_bug_in_the_proxy_is_an_api_error_and_a_log_entry_not_a_traceback(tmp_path, capfd):
    def explode(completion, *, model):
        raise RuntimeError("boom")

    log = tmp_path / "proxy-errors.log"
    proxy, _calls = start_anthropic_proxy(
        lambda request: sse_response({"choices": [{"delta": {"content": "x"}}]}),
        adapter=adapter_with(from_chat_response=explode),
        error_log=log,
    )
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json=HI,
        )
    finally:
        proxy.stop()
    assert r.status_code == 500
    assert r.json()["error"]["type"] == "api_error"
    assert "boom" in r.json()["error"]["message"]
    assert "Traceback" in log.read_text()
    assert "RuntimeError: boom" in log.read_text()
    assert capfd.readouterr().err == ""


def test_a_bug_mid_stream_is_an_error_event_and_a_log_entry(tmp_path, capfd):
    def flaky(chunks, *, model):
        events = anthropic.stream_events(chunks, model=model)
        yield next(events)
        raise RuntimeError("bad chunk")

    log = tmp_path / "proxy-errors.log"
    proxy, _calls = start_anthropic_proxy(
        lambda request: sse_response({"choices": [{"delta": {"content": "x"}}]}),
        adapter=adapter_with(stream_events=flaky),
        error_log=log,
    )
    try:
        with httpx.stream(
            "POST",
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={**HI, "stream": True},
        ) as r:
            assert r.status_code == 200
            text = b"".join(r.iter_bytes()).decode("utf-8", errors="replace")
    finally:
        proxy.stop()
    assert "event: error" in text
    assert "bad chunk" in text
    assert "RuntimeError: bad chunk" in log.read_text()
    assert capfd.readouterr().err == ""


def test_an_upstream_failure_is_one_log_line_not_a_traceback(tmp_path):
    log = tmp_path / "proxy-errors.log"

    def handler(request):
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    proxy, _calls = start_anthropic_proxy(handler, error_log=log)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json=HI,
        )
    finally:
        proxy.stop()
    assert r.status_code == 429
    assert "RateLimitError" in log.read_text()
    assert "Traceback" not in log.read_text()


def test_server_errors_go_to_the_log_never_to_stderr(tmp_path, capfd):
    log = tmp_path / "proxy-errors.log"
    proxy, _calls = start_anthropic_proxy(never_called, error_log=log)
    try:
        for exc in (BrokenPipeError(), ConnectionResetError()):
            try:
                raise exc
            except Exception:
                proxy._httpd.handle_error(None, ("127.0.0.1", 1))
        assert not log.exists()  # a client leaving is not worth a line
        try:
            raise ValueError("odd")
        except ValueError:
            proxy._httpd.handle_error(None, ("127.0.0.1", 1))
    finally:
        proxy.stop()
    assert "ValueError: odd" in log.read_text()
    assert capfd.readouterr().err == ""


def test_the_error_log_rolls_over_at_its_cap_and_never_raises(tmp_path, monkeypatch):
    log = tmp_path / "logs" / "proxy-errors.log"
    monkeypatch.setattr(proxy_mod, "_LOG_CAP", 60)
    proxy_mod._record(log, "a" * 100)  # past the cap on its own
    proxy_mod._record(log, "b" * 100)
    assert "b" * 100 in log.read_text()
    assert "a" * 100 not in log.read_text()
    assert "a" * 100 in log.with_name("proxy-errors.log.1").read_text()
    proxy_mod._record(None, "no log configured")
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    proxy_mod._record(blocker / "proxy-errors.log", "under a file, so it cannot be created")
