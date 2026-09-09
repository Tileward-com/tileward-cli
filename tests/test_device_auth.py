from __future__ import annotations

import httpx
import pytest
import respx

from tileward import auth as device_auth
from tileward import errors
from tileward._http import Transport

CODE = {
    "device_code": "dev-abc",
    "user_code": "WXYZ-1234",
    "verification_uri": "https://app.test/device",
    "verification_uri_complete": "https://app.test/device?code=WXYZ-1234",
    "expires_in": 600,
    "interval": 5,
}


def transport():
    return Transport(base_url="https://api.test", api_key=None, max_retries=0)


class Clock:
    """A monotonic clock that only moves when the flow sleeps, so tests run instantly."""

    def __init__(self):
        self.t = 0.0

    def sleep(self, seconds):
        self.t += seconds

    def now(self):
        return self.t


@respx.mock
def test_start_returns_the_code_and_the_url():
    respx.post("https://api.test/auth/device/code").mock(
        return_value=httpx.Response(200, json=CODE)
    )
    authorization = device_auth.start(transport(), client_name="twcli on laptop")
    assert authorization.user_code == "WXYZ-1234"
    assert authorization.url == "https://app.test/device?code=WXYZ-1234"
    assert authorization.interval == 5


@respx.mock
def test_client_name_is_sent_so_the_approval_screen_can_show_it():
    route = respx.post("https://api.test/auth/device/code").mock(
        return_value=httpx.Response(200, json=CODE)
    )
    device_auth.start(transport(), client_name="twcli on laptop")
    import json

    assert json.loads(route.calls[0].request.content)["client_name"] == "twcli on laptop"


@respx.mock
def test_a_response_missing_the_device_code_is_an_error():
    respx.post("https://api.test/auth/device/code").mock(
        return_value=httpx.Response(200, json={"user_code": "X"})
    )
    with pytest.raises(errors.APIError):
        device_auth.start(transport())


@respx.mock
def test_poll_waits_through_pending_then_returns_the_session():
    respx.post("https://api.test/auth/device/token").mock(
        side_effect=[
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(400, json={"error": "authorization_pending"}),
            httpx.Response(
                200,
                json={"access_token": "sess-xyz", "expires_in": 3600, "email": "a@b.c"},
            ),
        ]
    )
    clock = Clock()
    session = device_auth.poll(
        transport(), device_auth.DeviceAuthorization(CODE), sleep=clock.sleep, now=clock.now
    )
    assert session.access_token == "sess-xyz"
    assert session.email == "a@b.c"


@respx.mock
def test_slow_down_lengthens_the_interval_rather_than_failing():
    """Ignoring slow_down makes a client indistinguishable from one grinding the endpoint."""
    respx.post("https://api.test/auth/device/token").mock(
        side_effect=[
            httpx.Response(400, json={"error": "slow_down"}),
            httpx.Response(200, json={"access_token": "sess", "expires_in": 60}),
        ]
    )
    clock = Clock()
    device_auth.poll(
        transport(), device_auth.DeviceAuthorization(CODE), sleep=clock.sleep, now=clock.now
    )
    # First wait is the 5s interval; the second is 5+5 after the slow_down.
    assert clock.t == 15.0


@respx.mock
def test_denial_stops_immediately():
    respx.post("https://api.test/auth/device/token").mock(
        return_value=httpx.Response(400, json={"error": "access_denied"})
    )
    clock = Clock()
    with pytest.raises(errors.AuthenticationError) as exc:
        device_auth.poll(
            transport(), device_auth.DeviceAuthorization(CODE), sleep=clock.sleep, now=clock.now
        )
    assert exc.value.code == "access_denied"


@respx.mock
def test_polling_stops_at_the_deadline_rather_than_forever():
    respx.post("https://api.test/auth/device/token").mock(
        return_value=httpx.Response(400, json={"error": "authorization_pending"})
    )
    clock = Clock()
    short = dict(CODE, expires_in=12)
    with pytest.raises(errors.AuthenticationError) as exc:
        device_auth.poll(
            transport(), device_auth.DeviceAuthorization(short), sleep=clock.sleep, now=clock.now
        )
    assert exc.value.code == "expired_token"
    assert clock.t <= 12.0


@respx.mock
def test_expired_token_from_the_server_is_reported_as_expiry():
    respx.post("https://api.test/auth/device/token").mock(
        return_value=httpx.Response(400, json={"error": "expired_token"})
    )
    clock = Clock()
    with pytest.raises(errors.AuthenticationError) as exc:
        device_auth.poll(
            transport(), device_auth.DeviceAuthorization(CODE), sleep=clock.sleep, now=clock.now
        )
    assert exc.value.code == "expired_token"


@respx.mock
def test_an_openai_shaped_error_envelope_is_understood_too():
    """The polling errors are bare strings, but the gateway's other errors are objects."""
    respx.post("https://api.test/auth/device/token").mock(
        side_effect=[
            httpx.Response(
                400, json={"error": {"code": "authorization_pending", "message": "wait"}}
            ),
            httpx.Response(200, json={"access_token": "sess", "expires_in": 60}),
        ]
    )
    clock = Clock()
    session = device_auth.poll(
        transport(), device_auth.DeviceAuthorization(CODE), sleep=clock.sleep, now=clock.now
    )
    assert session.access_token == "sess"


@respx.mock
def test_a_200_with_no_token_does_not_spin_forever():
    respx.post("https://api.test/auth/device/token").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    clock = Clock()
    with pytest.raises(errors.APIError):
        device_auth.poll(
            transport(), device_auth.DeviceAuthorization(CODE), sleep=clock.sleep, now=clock.now
        )


@respx.mock
def test_an_unexpected_error_is_not_swallowed_as_pending():
    respx.post("https://api.test/auth/device/token").mock(
        return_value=httpx.Response(500, json={"error": {"message": "boom"}})
    )
    clock = Clock()
    with pytest.raises(errors.ServerError):
        device_auth.poll(
            transport(), device_auth.DeviceAuthorization(CODE), sleep=clock.sleep, now=clock.now
        )


def test_a_zero_interval_falls_back_to_the_default_rather_than_hammering():
    """A zero is a server that filled the field in wrong, not one asking to be polled flat out."""
    assert device_auth.DeviceAuthorization(dict(CODE, interval=0)).interval == 5.0
    assert device_auth.DeviceAuthorization(dict(CODE, interval=-3)).interval == 5.0


def test_a_missing_interval_uses_the_default():
    payload = {k: v for k, v in CODE.items() if k != "interval"}
    assert device_auth.DeviceAuthorization(payload).interval == device_auth.DEFAULT_INTERVAL


def test_an_absurd_interval_is_capped():
    assert (
        device_auth.DeviceAuthorization(dict(CODE, interval=9999)).interval
        == device_auth.MAX_INTERVAL
    )


# ---- the contract this client is pinned to -------------------------------------------------
# These assert the exact wire shapes documented in docs/device-auth.md. They are the tests that
# fail first if the server contract moves, which is the point of writing them down separately
# from the behaviour tests above.


@respx.mock
def test_the_request_body_matches_the_documented_contract():
    route = respx.post("https://api.test/auth/device/code").mock(
        return_value=httpx.Response(200, json=CODE)
    )
    device_auth.start(transport(), client_name="twcli on laptop")
    import json

    body = json.loads(route.calls[0].request.content)
    assert set(body) <= {"client_name"}


@respx.mock
def test_the_poll_body_carries_the_grant_type_and_the_device_code():
    route = respx.post("https://api.test/auth/device/token").mock(
        return_value=httpx.Response(200, json={"access_token": "s", "expires_in": 1})
    )
    clock = Clock()
    device_auth.poll(
        transport(), device_auth.DeviceAuthorization(CODE), sleep=clock.sleep, now=clock.now
    )
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["device_code"] == "dev-abc"
    assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"


@respx.mock
def test_no_credential_is_sent_while_getting_one():
    """This is what PRECEDES a login. Sending a stale key here would be nonsense at best."""
    route = respx.post("https://api.test/auth/device/code").mock(
        return_value=httpx.Response(200, json=CODE)
    )
    Transport(base_url="https://api.test", api_key="tw_live_stale", session_token="old")
    device_auth.start(
        Transport(base_url="https://api.test", api_key="tw_live_stale", session_token="old")
    )
    headers = route.calls[0].request.headers
    assert "authorization" not in headers
    assert "cookie" not in headers


@respx.mock
def test_too_many_waiting_sign_ins_explains_itself():
    """The server's slug is `slow_down`, which as a user-facing message reads like a bug."""
    respx.post("https://api.test/auth/device/code").mock(
        return_value=httpx.Response(429, json={"error": "slow_down"})
    )
    with pytest.raises(errors.RateLimitError) as exc:
        device_auth.start(transport())
    assert "already waiting" in str(exc.value)
