"""Minting and retiring API keys. Needs a console session, not a key.

The secret is returned once. Rotate keeps the key's id, and with it its policy and
history; revoke-then-create does not.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Dict, List, Optional

ACCOUNT = "/api/account"
KEYS = "/api/account/keys"


def _rows(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        keys = payload.get("keys")
        if isinstance(keys, list):
            return [row for row in keys if isinstance(row, dict)]
    return []


def create_body(label: str, cells: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"label": label}
    if cells:
        body["cells"] = list(cells)
    return body


def is_live(row: Dict[str, Any]) -> bool:
    return not row.get("revoked")


class Keys:
    def __init__(self, client: Any) -> None:
        self._client = client

    def list(self, *, include_revoked: bool = False) -> List[Dict[str, Any]]:
        rows = _rows(self._client._transport.request("GET", ACCOUNT, auth="session"))
        return rows if include_revoked else [r for r in rows if is_live(r)]

    def create(self, label: str, *, cells: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Mint a key. The returned `key` field is the only time the secret exists outside the API.

        `cells` locks the key to named topics — the governance binding, enforced at the gate.
        """
        return self._client._transport.request(
            "POST", KEYS, json=create_body(label, cells), auth="session"
        )

    def rotate(self, key_id: int) -> Dict[str, Any]:
        """New secret, same id, same policy, same history. Returned once."""
        return self._client._transport.request(
            "POST", f"{KEYS}/{int(key_id)}/rotate", json={}, auth="session"
        )

    def revoke(self, key_id: int) -> Dict[str, Any]:
        return self._client._transport.request(
            "POST", f"{KEYS}/{int(key_id)}/revoke", json={}, auth="session"
        )

    def metrics(self) -> Dict[str, Any]:
        return self._client._transport.request("GET", f"{KEYS}/metrics", auth="session")

    def policy(self, key_id: int) -> Dict[str, Any]:
        """What this key is governed by: its bound topics, model config, and context config."""
        return self._client._transport.request(
            "GET", f"{KEYS}/{int(key_id)}/policy", auth="session"
        )


class AsyncKeys:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def list(self, *, include_revoked: bool = False) -> List[Dict[str, Any]]:
        rows = _rows(await self._client._transport.request("GET", ACCOUNT, auth="session"))
        return rows if include_revoked else [r for r in rows if is_live(r)]

    async def create(self, label: str, *, cells: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        return await self._client._transport.request(
            "POST", KEYS, json=create_body(label, cells), auth="session"
        )

    async def rotate(self, key_id: int) -> Dict[str, Any]:
        return await self._client._transport.request(
            "POST", f"{KEYS}/{int(key_id)}/rotate", json={}, auth="session"
        )

    async def revoke(self, key_id: int) -> Dict[str, Any]:
        return await self._client._transport.request(
            "POST", f"{KEYS}/{int(key_id)}/revoke", json={}, auth="session"
        )

    async def metrics(self) -> Dict[str, Any]:
        return await self._client._transport.request("GET", f"{KEYS}/metrics", auth="session")

    async def policy(self, key_id: int) -> Dict[str, Any]:
        return await self._client._transport.request(
            "GET", f"{KEYS}/{int(key_id)}/policy", auth="session"
        )
