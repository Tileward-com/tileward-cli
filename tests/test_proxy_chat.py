"""Chat completions through the proxy: passed through, with the proxy holding the key.

For opencode's `@ai-sdk/openai-compatible` provider, which already speaks Tileward's own API and
only needs the proxy so the real `tw_live_` key never sits in its config.
"""

from __future__ import annotations

import json

import httpx

from tileward.cli.agents import chat, responses
from tileward.cli.agents.proxy import Proxy
from tileward.client import Tileward


def start(handler):
    calls = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = Tileward(
        api_key="tw_live_testkey",
        base_url="https://api.test",
        load_config=False,
        http_client=httpx.Client(transport=httpx.MockTransport(wrapped)),
    )
    proxy = Proxy(
        client=client,
        adapter=responses,
        model="tileward-35b-a3b",
        routes={"/v1/responses": "responses"},
        extra_routes={"/v1/chat/completions": ("chat", chat)},
    )
    proxy.start()
    return proxy, calls


COMPLETION = {
    "id": "c1",
    "object": "chat.completion",
    "model": "tileward-35b-a3b",
    "choices": [{
        "index": 0,
        "finish_reason": "tool_calls",
        "message": {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": "{\"city\": \"Paris\"}"},
        }]},
    }],
    "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
}

CALLS = COMPLETION["choices"][0]["message"]["tool_calls"]

TOOLS = [{"type": "function", "function": {
    "name": "get_weather",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
}}]


def test_a_chat_request_reaches_tileward_with_the_real_key_and_its_tools_intact():
    proxy, calls = start(lambda r: httpx.Response(200, json=COMPLETION))
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={
                "model": "whatever-the-client-says",
                "messages": [
                    {"role": "user", "content": "weather?"},
                    {"role": "assistant", "content": None, "tool_calls": CALLS},
                    {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
                ],
                "tools": TOOLS,
                "tool_choice": "auto",
                "temperature": 0.2,
            },
        )
        assert r.status_code == 200
        assert r.json() == COMPLETION, "the response is relayed as it came"
        sent = calls[0]
        assert sent.url.path == "/v1/chat/completions"
        # The proxy's key goes upstream, never the token the client holds.
        assert sent.headers["authorization"] == "Bearer tw_live_testkey"
        body = json.loads(sent.content)
        assert body["model"] == "tileward-35b-a3b", "the proxy's model wins over the client's"
        assert body["tools"] == TOOLS
        assert body["tool_choice"] == "auto"
        assert body["temperature"] == 0.2
        # A tool-use conversation only works if the tool result and the call it answers survive.
        assert body["messages"][1]["tool_calls"][0]["id"] == "call_1"
        assert body["messages"][2] == {"role": "tool", "tool_call_id": "call_1", "content": "sunny"}
    finally:
        proxy.stop()


def test_a_body_cannot_set_the_clients_own_options():
    """`timeout` and `headers` are options of the client library, not request fields. Passed
    through as keyword arguments they would change how the proxy talks to Tileward."""
    proxy, calls = start(lambda r: httpx.Response(200, json=COMPLETION))
    try:
        r = httpx.post(
            f"{proxy.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "timeout": 0.001,
                "headers": {"Authorization": "Bearer stolen"},
                "stream_options": {"include_usage": False},
            },
        )
        assert r.status_code == 200
        sent = calls[0]
        assert sent.headers["authorization"] == "Bearer tw_live_testkey"
        body = json.loads(sent.content)
        assert "timeout" not in body and "headers" not in body and "stream_options" not in body
    finally:
        proxy.stop()


def test_a_streamed_chat_is_relayed_as_openai_sse():
    upstream = (
        b'data: {"choices":[{"index":0,"delta":{"content":"Hi"}}]}\n\n'
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
        b"data: [DONE]\n\n"
    )
    proxy, calls = start(lambda r: httpx.Response(
        200, content=upstream, headers={"content-type": "text/event-stream"}))
    try:
        with httpx.stream(
            "POST", f"{proxy.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {proxy.token}"},
            json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"] == "text/event-stream"
            text = b"".join(r.iter_bytes()).decode("utf-8")
        events = [line[len("data: "):] for line in text.splitlines() if line.startswith("data: ")]
        assert events[-1] == "[DONE]"
        chunks = [json.loads(e) for e in events[:-1]]
        assert chunks[0]["choices"][0]["delta"]["content"] == "Hi"
        assert chunks[-1]["usage"] == {"prompt_tokens": 1, "completion_tokens": 1}
        assert json.loads(calls[0].content)["stream"] is True
    finally:
        proxy.stop()


def test_both_protocols_share_one_port():
    """Codex's Responses API still answers beside the chat route."""
    proxy, _ = start(lambda r: httpx.Response(200, json={
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }))
    try:
        headers = {"Authorization": f"Bearer {proxy.token}"}
        chat_r = httpx.post(f"{proxy.base_url}/v1/chat/completions", headers=headers,
                            json={"messages": [{"role": "user", "content": "hi"}]})
        resp_r = httpx.post(f"{proxy.base_url}/v1/responses", headers=headers,
                            json={"input": "hi", "model": "m"})
        # Chat is relayed untranslated; Responses is translated. Each route kept its own adapter.
        assert chat_r.status_code == 200
        assert "choices" in chat_r.json() and "output" not in chat_r.json()
        assert resp_r.status_code == 200 and resp_r.json()["object"] == "response"
        assert "output" in resp_r.json()
    finally:
        proxy.stop()


def test_the_wrong_token_is_refused_in_openais_error_shape():
    proxy, calls = start(lambda r: httpx.Response(500))
    try:
        r = httpx.post(f"{proxy.base_url}/v1/chat/completions",
                       headers={"Authorization": "Bearer nope"},
                       json={"messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 401
        assert r.json()["error"]["type"] == "invalid_request_error"
        assert calls == [], "a refused request never reaches Tileward"
    finally:
        proxy.stop()


def test_a_request_without_messages_is_refused_before_it_is_sent():
    proxy, calls = start(lambda r: httpx.Response(500))
    try:
        r = httpx.post(f"{proxy.base_url}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {proxy.token}"}, json={"model": "m"})
        assert r.status_code == 400
        assert "messages" in r.json()["error"]["message"]
        assert calls == []
    finally:
        proxy.stop()
