"""OpenAI chat completions, passed through.

For clients that already speak Tileward's own API — opencode's `@ai-sdk/openai-compatible`
provider — and only need the proxy for its token: the client holds the proxy's token, and the proxy
holds the real `tw_live_` key. So this adapter translates nothing. It hands the body on, with the
model this proxy serves in place of whatever the client named, and relays what comes back.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, Tuple

import voluptuous as vol

# Only what the proxy itself relies on. Everything else in the body — tools, tool_choice,
# response_format, stop, and whatever the API grows next — passes through untouched.
REQUEST_SCHEMA = vol.Schema(
    {vol.Required("messages"): vol.All(list, vol.Length(min=1))},
    extra=vol.ALLOW_EXTRA,
)

# The reply goes back as it came, so a request that is not streaming is answered by a call that is
# not streaming either: a completion rebuilt from a stream is not the one the gateway sent.
RELAYS_RESPONSE = True

# Keys the client library takes as its own options rather than request fields. A body that set
# them would change how the proxy talks to Tileward, not what it asks; the stream options are set
# by the proxy itself, which always asks for the usage chunk.
_NOT_REQUEST_FIELDS = ("model", "timeout", "headers", "stream_options")


def to_chat_request(body: Dict[str, Any], *, model: str) -> Dict[str, Any]:
    return {key: value for key, value in body.items() if key not in _NOT_REQUEST_FIELDS}


def from_chat_response(completion: Dict[str, Any], *, model: str) -> Dict[str, Any]:
    return completion


def stream_events(chunks: Iterator[Dict[str, Any]], *, model: str) -> Iterator[bytes]:
    for chunk in chunks:
        yield b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n"
    yield b"data: [DONE]\n\n"


def error_body(exc: Exception) -> Tuple[int, Dict[str, Any]]:
    status = getattr(exc, "status", None) or 500
    message = getattr(exc, "message", None) or str(exc)
    code = getattr(exc, "code", None)
    etype = "invalid_request_error" if status < 500 else "server_error"
    return status, {"error": {"message": message, "type": etype, "code": code}}


def stream_error_event(exc: Exception) -> bytes:
    _, payload = error_body(exc)
    return b"data: " + json.dumps(payload).encode("utf-8") + b"\n\n"
