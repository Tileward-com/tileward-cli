"""Minimal MCP client for Tileward Context.

The transport is stateless streamable-HTTP, so a `tools/call` needs no initialize
handshake. The conversation header goes out under both spellings; one deployed
generation reads only the older name.
"""

from __future__ import annotations

import json as _json
from collections.abc import Mapping
from typing import Any, Dict, Optional

import httpx

from . import errors

JSONRPC_VERSION = "2.0"
# Streamable HTTP may answer with either a JSON body or an SSE stream; the spec allows both and
# the server picks. Asking for both is what keeps the client working across that choice.
MCP_ACCEPT = "application/json, text/event-stream"


def build_payload(tool: str, arguments: Mapping[str, Any], request_id: int = 1) -> Dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": {k: v for k, v in arguments.items() if v is not None},
        },
    }


def conversation_headers(conversation: Optional[str]) -> Dict[str, str]:
    if not conversation:
        return {}
    value = str(conversation).strip()
    if not value:
        return {}
    return {"X-Tileward-Conversation": value, "X-Twinkle-Conversation": value}


def parse_body(status: int, content_type: str, text: str) -> Dict[str, Any]:
    """Decode a streamable-HTTP response into the JSON-RPC envelope."""
    if "text/event-stream" in (content_type or ""):
        envelope: Optional[Dict[str, Any]] = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                candidate = _json.loads(payload)
            except ValueError:
                continue
            if isinstance(candidate, dict) and ("result" in candidate or "error" in candidate):
                envelope = candidate
        if envelope is None:
            raise errors.APIError(
                "Context returned an event stream with no JSON-RPC response in it.",
                status=status,
                body={"raw": text[:500]},
            )
        return envelope
    try:
        decoded = _json.loads(text)
    except ValueError as exc:
        raise errors.APIError(
            f"Context returned a body that is not JSON: {text[:200]}", status=status
        ) from exc
    if not isinstance(decoded, dict):
        raise errors.APIError("Context returned a JSON value that is not an object.", status=status)
    return decoded


def unwrap(envelope: Mapping[str, Any]) -> Any:
    """Pull a tool's return value out of the JSON-RPC + MCP content wrapping.

    Three failure shapes have to be told apart, and they arrive at three different depths:
      * a JSON-RPC `error` — the method or the arguments were wrong;
      * `result.isError` — the tool ran and refused (a bad conversation id, a missing document);
      * a result with no content — nothing to unwrap, and returning `{}` would look like success.
    """
    if "error" in envelope:
        err = envelope.get("error") or {}
        message = str(err.get("message") or "Context returned a JSON-RPC error.")
        raise errors.APIError(message, code=str(err.get("code") or ""), body=dict(envelope))

    result = envelope.get("result")
    if not isinstance(result, dict):
        raise errors.APIError("Context returned no result.", body=dict(envelope))

    content = result.get("content") or []
    text = ""
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            text = str(first.get("text") or "")

    if result.get("isError"):
        raise errors.APIError(text or "The Context tool reported an error.", body=dict(envelope))

    # A tool that returns a dict is serialised as JSON inside a text block; one that returns a
    # string is that string. Both are legitimate, so a failed parse is data, not an error.
    if not text:
        structured = result.get("structuredContent")
        return structured if structured is not None else {}
    try:
        return _json.loads(text)
    except ValueError:
        return text


class ContextTransport:
    """Synchronous MCP-over-HTTP calls against the Context endpoint."""

    def __init__(
        self,
        *,
        url: str,
        api_key: Optional[str],
        timeout: float = 60.0,
        user_agent: str = "tileward-python",
        client: Optional[httpx.Client] = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.user_agent = user_agent
        self._client = client
        self._owns_client = client is None
        self._counter = 0

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=True)
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def _headers(self, conversation: Optional[str]) -> Dict[str, str]:
        if not self.api_key:
            raise errors.ConfigError(
                "Tileward Context needs an API key. Set TILEWARD_API_KEY or pass api_key=..."
            )
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": MCP_ACCEPT,
            "User-Agent": self.user_agent,
        }
        headers.update(conversation_headers(conversation))
        return headers

    def call(
        self,
        tool: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        conversation: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        self._counter += 1
        payload = build_payload(tool, arguments or {}, self._counter)
        try:
            response = self.client.post(
                self.url,
                json=payload,
                headers=self._headers(conversation),
                timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:
            raise errors.ConnectionError_(f"Context call to {self.url} failed: {exc}") from exc
        if response.status_code >= 400:
            # A 401 here is its own thing: the Context endpoint takes a Tileward API key, and the
            # error it returns points OAuth clients at a different path. Surface it verbatim.
            raise errors.from_response(
                response.status_code,
                _safe_json(response.text),
                response.headers.get("x-request-id"),
            )
        envelope = parse_body(
            response.status_code, response.headers.get("content-type", ""), response.text
        )
        return unwrap(envelope)


class AsyncContextTransport(ContextTransport):
    """Asynchronous twin. Shares every decoding rule above."""

    def __init__(self, *args: Any, client: Optional[httpx.AsyncClient] = None, **kwargs: Any):
        kwargs.pop("client", None)
        super().__init__(*args, **kwargs)
        self._aclient = client
        self._owns_aclient = client is None

    @property
    def aclient(self) -> httpx.AsyncClient:
        if self._aclient is None:
            self._aclient = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self._aclient

    async def aclose(self) -> None:
        if self._aclient is not None and self._owns_aclient:
            await self._aclient.aclose()
            self._aclient = None

    async def acall(
        self,
        tool: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        conversation: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        self._counter += 1
        payload = build_payload(tool, arguments or {}, self._counter)
        try:
            response = await self.aclient.post(
                self.url,
                json=payload,
                headers=self._headers(conversation),
                timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:
            raise errors.ConnectionError_(f"Context call to {self.url} failed: {exc}") from exc
        if response.status_code >= 400:
            raise errors.from_response(
                response.status_code,
                _safe_json(response.text),
                response.headers.get("x-request-id"),
            )
        envelope = parse_body(
            response.status_code, response.headers.get("content-type", ""), response.text
        )
        return unwrap(envelope)


def _safe_json(text: str) -> Any:
    try:
        return _json.loads(text)
    except ValueError:
        return text
