"""Exceptions. All carry the HTTP status and the parsed error envelope."""

from __future__ import annotations

from typing import Any, Dict, Optional


class TilewardError(Exception):
    """Base for every error this library raises on purpose."""


class ConfigError(TilewardError):
    """The client is not configured well enough to make the call (no key, no base URL, ...)."""


class APIError(TilewardError):
    """The API answered, and the answer was an error."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        code: Optional[str] = None,
        type: Optional[str] = None,  # noqa: A002 - mirrors the wire field name
        body: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.type = type
        self.body = body or {}
        self.request_id = request_id

    def __str__(self) -> str:
        bits = [self.message]
        if self.status is not None:
            bits.append(f"(HTTP {self.status}" + (f", code={self.code}" if self.code else "") + ")")
        if self.request_id:
            bits.append(f"[request {self.request_id}]")
        return " ".join(bits)


class AuthenticationError(APIError):
    """401. The key or session is missing, malformed, revoked, or expired."""


class PermissionError_(APIError):
    """403. Authenticated, but not allowed — usually a plan entitlement."""


class NotFoundError(APIError):
    """404. Most often a model id that is not served; check `GET /v1/models`."""


class InsufficientBalanceError(APIError):
    """402. The prepaid balance is exhausted. Top up; calls resume immediately."""


class RateLimitError(APIError):
    """429. Back off and retry; the client already retries these on idempotent calls."""


class ServerError(APIError):
    """5xx. Includes 502 from an upstream model, which is transient."""


class ConnectionError_(TilewardError):
    """The request never got an answer: DNS, TLS, connect timeout, read timeout."""


class GuardRefusal(TilewardError):
    """A chat completion was refused by governance rather than answered.

    Raised only by helpers that promise text back (``chat.say``). The low-level
    ``chat.completions.create`` returns the refusal as an ordinary completion with
    ``finish_reason == "content_filter"``, because that is what the API returns and a caller
    metering responses needs to see it.
    """

    def __init__(self, message: str, *, completion: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.completion = completion or {}


_STATUS_MAP = {
    401: AuthenticationError,
    402: InsufficientBalanceError,
    403: PermissionError_,
    404: NotFoundError,
    429: RateLimitError,
}


def from_response(status: int, body: Any, request_id: Optional[str] = None) -> APIError:
    """Build the right exception from a status and a decoded body.

    Bodies are not guaranteed to be the OpenAI envelope — a proxy 502 is often HTML, and the
    console endpoints answer `{"error": "plain string"}` — so every shape degrades to a message
    rather than raising a second error while reporting the first.
    """
    err: Dict[str, Any] = {}
    message = ""
    if isinstance(body, dict):
        raw = body.get("error", body)
        if isinstance(raw, dict):
            err = raw
            message = str(raw.get("message") or "")
        elif isinstance(raw, str):
            message = raw
        if not message:
            message = str(body.get("detail") or body.get("message") or "")
    elif isinstance(body, str):
        message = body.strip()[:500]

    if not message:
        message = f"Tileward API returned HTTP {status}."

    cls = _STATUS_MAP.get(status, ServerError if status >= 500 else APIError)
    return cls(
        message,
        status=status,
        code=err.get("code"),
        type=err.get("type"),
        body=body if isinstance(body, dict) else {"raw": body},
        request_id=request_id,
    )
