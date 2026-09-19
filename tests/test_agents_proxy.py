"""The real `ThreadingHTTPServer`, driven with `httpx`, against a mocked Tileward backend.

The outbound leg is mocked with `httpx.MockTransport` (not `respx`'s global monkeypatch, which
intercepts this file's own calls to the local proxy too)."""

from __future__ import annotations

import json

import httpx

from tileward.cli.agents import anthropic, responses
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


def start_anthropic_proxy(handler):
    client, calls = make_client(handler)
    proxy = Proxy(
        client=client,
        adapter=anthropic,
        model="tileward-35b-a3b",
        routes={"/v1/messages": "messages", "/v1/messages/count_tokens": "count_tokens"},
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


def never_called(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected outbound call: {request.url}")


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
    def handler(request):
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "model": "tileward-35b-a3b",
                "choices": [{"finish_reason": "stop", "message": {"content": "Hello!"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    proxy, calls = start_anthropic_proxy(handler)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 16},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["type"] == "message"
        assert body["content"] == [{"type": "text", "text": "Hello!"}]
        assert body["usage"] == {"input_tokens": 5, "output_tokens": 2}
        assert len(calls) == 1
        assert json.loads(calls[0].content)["model"] == "tileward-35b-a3b"
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
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "Hi!"}}],
                "usage": {},
            },
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


def test_malformed_request_body_does_not_crash_the_proxy():
    """An unparsable body decodes to `{}`, a valid empty Messages request, rather than crashing
    `do_POST`."""

    def handler(request):
        return httpx.Response(
            200,
            json={"choices": [{"finish_reason": "stop", "message": {"content": ""}}], "usage": {}},
        )

    proxy, _calls = start_anthropic_proxy(handler)
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/messages",
            headers={"Authorization": f"Bearer {proxy.token}", "Content-Type": "application/json"},
            content=b"not json",
        )
        assert r.status_code == 200
        assert r.json()["type"] == "message"
    finally:
        proxy.stop()
