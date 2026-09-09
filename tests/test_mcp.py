from __future__ import annotations

import json

import httpx
import pytest
import respx

from tileward import errors
from tileward._mcp import ContextTransport, build_payload, conversation_headers, parse_body, unwrap


def tool_result(value):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(value)}]},
    }


def test_payload_shape_matches_the_transport():
    payload = build_payload("tileward_recall", {"query": "q", "scope": None})
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "tools/call"
    assert payload["params"]["name"] == "tileward_recall"
    # Unset optionals are omitted, not sent as null.
    assert payload["params"]["arguments"] == {"query": "q"}


def test_conversation_is_sent_under_both_spellings():
    """One deployed generation reads only the older header name."""
    headers = conversation_headers("thread-1")
    assert headers["X-Tileward-Conversation"] == "thread-1"
    assert headers["X-Twinkle-Conversation"] == "thread-1"


def test_no_conversation_sends_no_header():
    assert conversation_headers(None) == {}
    assert conversation_headers("  ") == {}


def test_parses_a_plain_json_body():
    envelope = parse_body(200, "application/json", json.dumps(tool_result({"ok": True})))
    assert unwrap(envelope) == {"ok": True}


def test_parses_an_sse_body():
    """Streamable HTTP may answer either way; the server picks."""
    body = "event: message\ndata: " + json.dumps(tool_result({"ok": True})) + "\n\n"
    envelope = parse_body(200, "text/event-stream", body)
    assert unwrap(envelope) == {"ok": True}


def test_sse_with_no_jsonrpc_response_is_an_error():
    with pytest.raises(errors.APIError):
        parse_body(200, "text/event-stream", "data: [DONE]\n\n")


def test_jsonrpc_error_raises():
    with pytest.raises(errors.APIError) as exc:
        unwrap({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad args"}})
    assert "bad args" in str(exc.value)


def test_tool_level_iserror_raises_with_the_tool_message():
    """`isError` sits inside a SUCCESSFUL JSON-RPC result, a level deeper than a protocol error."""
    with pytest.raises(errors.APIError) as exc:
        unwrap(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"isError": True, "content": [{"type": "text", "text": "no such doc"}]},
            }
        )
    assert "no such doc" in str(exc.value)


def test_a_tool_returning_a_string_comes_back_as_a_string():
    envelope = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": "plain words"}]}}
    assert unwrap(envelope) == "plain words"


def test_missing_result_is_an_error_not_an_empty_dict():
    with pytest.raises(errors.APIError):
        unwrap({"jsonrpc": "2.0", "id": 1})


@respx.mock
def test_call_posts_to_the_host_root_with_both_accept_types():
    route = respx.post("https://context.test").mock(
        return_value=httpx.Response(200, json=tool_result({"text": "recalled"}))
    )
    transport = ContextTransport(url="https://context.test", api_key="tw_live_k")
    out = transport.call("tileward_recall", {"query": "q"}, conversation="t1")
    assert out == {"text": "recalled"}
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer tw_live_k"
    assert "text/event-stream" in request.headers["accept"]
    assert request.headers["x-tileward-conversation"] == "t1"


@respx.mock
def test_a_401_from_context_surfaces_the_servers_own_message():
    """Its 401 body explains that OAuth clients need a different path. Do not swallow it."""
    respx.post("https://context.test").mock(
        return_value=httpx.Response(
            401,
            json={"error": "invalid or missing API key. This endpoint takes a Tileward API key"},
        )
    )
    transport = ContextTransport(url="https://context.test", api_key="bad")
    with pytest.raises(errors.AuthenticationError) as exc:
        transport.call("tileward_stats", {})
    assert "takes a Tileward API key" in str(exc.value)


def test_no_key_fails_before_the_request():
    transport = ContextTransport(url="https://context.test", api_key=None)
    with pytest.raises(errors.ConfigError):
        transport.call("tileward_stats", {})
