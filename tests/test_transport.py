from __future__ import annotations

import httpx
import pytest
import respx

from tileward import errors
from tileward._http import Transport


def make(**kwargs):
    kwargs.setdefault("base_url", "https://api.test")
    kwargs.setdefault("api_key", "tw_live_k")
    return Transport(**kwargs)


@respx.mock
def test_bearer_header_on_key_auth():
    route = respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    make().request("GET", "/v1/models")
    assert route.calls[0].request.headers["authorization"] == "Bearer tw_live_k"
    assert "cookie" not in route.calls[0].request.headers


@respx.mock
def test_session_auth_sends_a_cookie_not_a_bearer():
    """The account surface reads `request.cookies`. A bearer header authenticates nothing there."""
    route = respx.get("https://api.test/api/account").mock(
        return_value=httpx.Response(200, json={"email": "a@b.c"})
    )
    make(session_token="sess-123").request("GET", "/api/account", auth="session")
    request = route.calls[0].request
    assert request.headers["cookie"] == "tw_session=sess-123"
    assert "authorization" not in request.headers


def test_missing_key_names_the_credential_that_is_missing():
    with pytest.raises(errors.ConfigError) as exc:
        make(api_key=None).request("GET", "/v1/models")
    assert "API key" in str(exc.value)


def test_missing_session_says_login_not_key():
    with pytest.raises(errors.ConfigError) as exc:
        make().request("GET", "/api/account", auth="session")
    assert "twcli auth login" in str(exc.value)


@respx.mock
def test_retries_a_429_then_succeeds(monkeypatch):
    monkeypatch.setattr("tileward._http.time.sleep", lambda _s: None)
    route = respx.get("https://api.test/v1/models").mock(
        side_effect=[
            httpx.Response(429, json={"error": {"message": "slow"}}),
            httpx.Response(200, json={"data": [{"id": "m"}]}),
        ]
    )
    out = make().request("GET", "/v1/models")
    assert out["data"][0]["id"] == "m"
    assert route.call_count == 2


@respx.mock
def test_does_not_retry_a_402(monkeypatch):
    """A depleted balance does not get better by asking again."""
    monkeypatch.setattr("tileward._http.time.sleep", lambda _s: None)
    route = respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(402, json={"error": {"message": "no funds"}})
    )
    with pytest.raises(errors.InsufficientBalanceError):
        make().request("POST", "/v1/chat/completions", json={})
    assert route.call_count == 1


@respx.mock
def test_gives_up_after_the_retry_budget(monkeypatch):
    monkeypatch.setattr("tileward._http.time.sleep", lambda _s: None)
    route = respx.get("https://api.test/v1/models").mock(
        return_value=httpx.Response(503, json={"error": {"message": "down"}})
    )
    with pytest.raises(errors.ServerError):
        make(max_retries=2).request("GET", "/v1/models")
    assert route.call_count == 3  # the original plus two retries


@respx.mock
def test_connection_failure_becomes_a_tileward_error(monkeypatch):
    monkeypatch.setattr("tileward._http.time.sleep", lambda _s: None)
    respx.get("https://api.test/v1/models").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(errors.ConnectionError_):
        make(max_retries=0).request("GET", "/v1/models")


@respx.mock
def test_none_params_are_dropped():
    """An unset optional must not travel as the string 'None'."""
    route = respx.get("https://api.test/api/account/audit").mock(
        return_value=httpx.Response(200, json={})
    )
    make(session_token="s").request(
        "GET", "/api/account/audit", params={"limit": None}, auth="session"
    )
    assert "limit" not in str(route.calls[0].request.url)


@respx.mock
def test_sse_stream_stops_at_done_and_skips_the_sentinel():
    body = (
        'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
        "data: [DONE]\n\n"
    )
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
    )
    chunks = list(make().stream_sse("POST", "/v1/chat/completions", json={}))
    assert [c["choices"][0]["delta"]["content"] for c in chunks] == ["a", "b"]


@respx.mock
def test_stream_error_status_raises_before_yielding():
    respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(402, json={"error": {"message": "no funds"}})
    )
    with pytest.raises(errors.InsufficientBalanceError):
        list(make().stream_sse("POST", "/v1/chat/completions", json={}))
