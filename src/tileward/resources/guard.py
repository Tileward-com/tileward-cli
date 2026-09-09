"""`POST /v1/guard` — allow / deny, no generation.

`disallow` refuses what you name and passes the rest; `allow` passes only what you name
and refuses the rest. Passing a list classifies the batch in one call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Dict, List, Optional, Union

PATH = "/v1/guard"

TextInput = Union[str, Sequence[str]]


def build_body(
    text: TextInput,
    *,
    allow: Optional[Sequence[str]] = None,
    disallow: Optional[Sequence[str]] = None,
    always_block: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"input": list(text) if not isinstance(text, str) else text}
    # Omitting both is meaningful: it tells the guard to use the policy bound to the key. Sending
    # empty lists instead would override that binding with "govern nothing".
    if allow:
        body["allow"] = list(allow)
    if disallow:
        body["disallow"] = list(disallow)
    if always_block:
        body["always_block"] = list(always_block)
    return body


def decisions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Always a list, whether one input was sent or many."""
    result = payload.get("result")
    if isinstance(result, list):
        return [r for r in result if isinstance(r, dict)]
    if isinstance(result, dict):
        return [result]
    return []


def allowed(payload: Dict[str, Any]) -> bool:
    """True only if every decision in the response allowed."""
    rows = decisions(payload)
    return bool(rows) and all(bool(r.get("allowed")) for r in rows)


class Guard:
    def __init__(self, client: Any) -> None:
        self._client = client

    def check(
        self,
        text: TextInput,
        *,
        allow: Optional[Sequence[str]] = None,
        disallow: Optional[Sequence[str]] = None,
        always_block: Optional[Sequence[str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """The raw response: `{tokens, cost_micros, result}`."""
        body = build_body(text, allow=allow, disallow=disallow, always_block=always_block)
        return self._client._transport.request("POST", PATH, json=body, timeout=timeout)

    def allows(self, text: TextInput, **kwargs: Any) -> bool:
        """Just the verdict, for an `if` statement."""
        return allowed(self.check(text, **kwargs))


class AsyncGuard:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def check(
        self,
        text: TextInput,
        *,
        allow: Optional[Sequence[str]] = None,
        disallow: Optional[Sequence[str]] = None,
        always_block: Optional[Sequence[str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        body = build_body(text, allow=allow, disallow=disallow, always_block=always_block)
        return await self._client._transport.request("POST", PATH, json=body, timeout=timeout)

    async def allows(self, text: TextInput, **kwargs: Any) -> bool:
        return allowed(await self.check(text, **kwargs))
