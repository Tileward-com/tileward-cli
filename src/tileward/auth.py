"""Device-code sign-in (RFC 8628).

The account surface authenticates on a console session, not a bearer key, so the CLI
needs a login. This gets one without a password touching the terminal. See
docs/device-auth.md.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from . import errors
from ._http import Transport

DEVICE_CODE_PATH = "/auth/device/code"
DEVICE_TOKEN_PATH = "/auth/device/token"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

DEFAULT_INTERVAL = 5.0
MIN_INTERVAL = 1.0
MAX_INTERVAL = 30.0

# The terminal states a poll can reach. Everything else means "keep waiting".
PENDING = "authorization_pending"
SLOW_DOWN = "slow_down"
EXPIRED = "expired_token"
DENIED = "access_denied"


class DeviceAuthorization:
    """What the server hands back when a device asks to be authorized."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.raw = payload
        self.device_code = str(payload.get("device_code") or "")
        self.user_code = str(payload.get("user_code") or "")
        self.verification_uri = str(payload.get("verification_uri") or "")
        self.verification_uri_complete = str(payload.get("verification_uri_complete") or "")
        try:
            self.expires_in = int(payload.get("expires_in") or 600)
        except (TypeError, ValueError):
            self.expires_in = 600
        # An absent interval means "use the default" (RFC 8628). So does a zero or a negative
        # one: those are not a server asking to be polled as fast as possible, they are a server
        # that filled the field in wrong, and honouring them would hammer the endpoint.
        raw_interval = payload.get("interval")
        try:
            interval = float(raw_interval) if raw_interval is not None else DEFAULT_INTERVAL
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL
        if interval <= 0:
            interval = DEFAULT_INTERVAL
        self.interval = min(max(interval, MIN_INTERVAL), MAX_INTERVAL)
        if not self.device_code or not self.user_code:
            raise errors.APIError(
                "The device-authorization response was missing device_code or user_code.",
                body=payload,
            )

    @property
    def url(self) -> str:
        """The link to open. Prefers the one with the code already in it."""
        return self.verification_uri_complete or self.verification_uri

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DeviceAuthorization user_code={self.user_code!r} url={self.url!r}>"


class DeviceSession:
    """A granted console session."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.raw = payload
        self.access_token = str(payload.get("access_token") or "")
        self.token_type = str(payload.get("token_type") or "session")
        self.email = payload.get("email")
        try:
            self.expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            self.expires_in = 0
        self.expires_at = time.time() + self.expires_in if self.expires_in else None
        if not self.access_token:
            raise errors.APIError("The device-token response carried no session.", body=payload)


def _error_code(exc: errors.APIError) -> str:
    """The RFC 8628 error slug out of whatever envelope it arrived in.

    The polling errors come back as HTTP 400 with `{"error": "authorization_pending"}`, but the
    gateway's other errors use the OpenAI envelope where `error` is an object. Both shapes have to
    resolve here or a perfectly normal "still waiting" would raise.
    """
    body = exc.body or {}
    raw = body.get("error")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return str(raw.get("code") or raw.get("type") or "")
    return exc.code or ""


def start(transport: Transport, *, client_name: Optional[str] = None) -> DeviceAuthorization:
    """Ask for a device code. Unauthenticated by definition — this is what precedes auth."""
    body: Dict[str, Any] = {}
    if client_name:
        # Shown on the approval screen. A person approving a code needs to see what they are
        # approving, or the screen is a rubber stamp.
        body["client_name"] = client_name
    try:
        payload = transport.request("POST", DEVICE_CODE_PATH, json=body, auth="none", retries=0)
    except errors.RateLimitError as exc:
        # The server answers 429 with the RFC's `slow_down` slug, which as an error MESSAGE reads
        # like a bug rather than an instruction. Say what it means and what to do.
        if _error_code(exc) == SLOW_DOWN:
            raise errors.RateLimitError(
                "Too many sign-ins already waiting from this machine. Finish or abandon one "
                "(they expire on their own in a few minutes) and try again.",
                status=429,
                code=SLOW_DOWN,
                body=exc.body,
            ) from exc
        raise
    if not isinstance(payload, dict):
        raise errors.APIError("Unexpected device-authorization response.", body={"raw": payload})
    return DeviceAuthorization(payload)


def poll(
    transport: Transport,
    authorization: DeviceAuthorization,
    *,
    on_tick: Optional[Callable[[float], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> DeviceSession:
    """Block until the user approves, denies, or the code expires.

    `sleep` and `now` are injectable so the timing can be tested without a test that takes ten
    minutes to run.
    """
    interval = authorization.interval
    deadline = now() + authorization.expires_in
    body = {"grant_type": GRANT_TYPE, "device_code": authorization.device_code}
    while True:
        remaining = deadline - now()
        if remaining <= 0:
            raise errors.AuthenticationError(
                "The sign-in code expired before it was approved. Run `twcli auth login` again.",
                code=EXPIRED,
            )
        if on_tick:
            on_tick(remaining)
        sleep(min(interval, max(remaining, 0.0)))
        try:
            payload = transport.request(
                "POST", DEVICE_TOKEN_PATH, json=body, auth="none", retries=0
            )
        except errors.APIError as exc:
            code = _error_code(exc)
            if code == PENDING:
                continue
            if code == SLOW_DOWN:
                # The spec's remedy is to add to the interval, not to multiply it: this is a
                # politeness signal, not a rate-limit punishment.
                interval = min(interval + 5.0, MAX_INTERVAL)
                continue
            if code == DENIED:
                raise errors.AuthenticationError(
                    "Sign-in was denied in the browser.", code=DENIED
                ) from exc
            if code == EXPIRED:
                raise errors.AuthenticationError(
                    "The sign-in code expired. Run `twcli auth login` again.", code=EXPIRED
                ) from exc
            raise
        if isinstance(payload, dict) and payload.get("access_token"):
            return DeviceSession(payload)
        # A 200 with no token is not a state the flow defines. Treating it as "keep polling" would
        # spin forever against a misbehaving proxy, so it is an error.
        raise errors.APIError(
            "The device-token endpoint answered without a session.",
            body=payload if isinstance(payload, dict) else {"raw": payload},
        )


def login(
    transport: Transport,
    *,
    client_name: Optional[str] = None,
    on_prompt: Optional[Callable[[DeviceAuthorization], None]] = None,
    on_tick: Optional[Callable[[float], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> DeviceSession:
    """The whole flow: request a code, show it, wait for approval."""
    authorization = start(transport, client_name=client_name)
    if on_prompt:
        on_prompt(authorization)
    return poll(transport, authorization, on_tick=on_tick, sleep=sleep, now=now)
