"""`GET /v1/models` — what is served right now.

The catalogue is a database table, so the source tree cannot tell you what is served.
Do not hardcode a model id.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..errors import NotFoundError

PATH = "/v1/models"


def _entries(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _find(rows: List[Dict[str, Any]], model_id: str) -> Dict[str, Any]:
    for row in rows:
        if row.get("id") == model_id:
            return row
    raise NotFoundError(
        f"{model_id!r} is not in the served catalogue. "
        f"Served now: {', '.join(str(r.get('id')) for r in rows) or '(none)'}.",
        status=404,
        code="model_not_found",
    )


# The interesting fields are nested under a `tileward` object; only id/object/owned_by/description
# are top level. Reading `row["context_len"]` gets None and renders as a blank column.
NESTED = "tileward"


def summarize(row: Dict[str, Any]) -> Dict[str, Any]:
    """One flat mapping per model, for a table or a detail view.

    The nested block is merged UP, and the outer keys win a collision: `id` and `owned_by` are
    the model's identity and must not be shadowed by anything the catalogue grows later.
    """
    inner = row.get(NESTED)
    flat: Dict[str, Any] = dict(inner) if isinstance(inner, dict) else {}
    flat.update({k: v for k, v in row.items() if k != NESTED})
    return flat


def summarize_all(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [summarize(row) for row in rows]


class Models:
    def __init__(self, client: Any) -> None:
        self._client = client

    def list(self) -> List[Dict[str, Any]]:
        """Every served model, as returned by the API."""
        return _entries(self._client._transport.request("GET", PATH))

    def retrieve(self, model_id: str) -> Dict[str, Any]:
        """One model's row, including whatever rate and spec fields the API reports."""
        return _find(self.list(), model_id)

    def ids(self) -> List[str]:
        return [str(row.get("id")) for row in self.list() if row.get("id")]

    def default(self) -> Optional[str]:
        """The id to use when the caller named none.

        Prefers a configured default, then the first served id. It deliberately does not fall back
        to a literal: a hardcoded id that stops being served turns into a 404 at call time, which
        is exactly the failure this whole module exists to avoid.
        """
        configured = getattr(self._client, "default_model", None)
        if configured:
            return str(configured)
        ids = self.ids()
        return ids[0] if ids else None


class AsyncModels:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def list(self) -> List[Dict[str, Any]]:
        return _entries(await self._client._transport.request("GET", PATH))

    async def retrieve(self, model_id: str) -> Dict[str, Any]:
        return _find(await self.list(), model_id)

    async def ids(self) -> List[str]:
        return [str(row.get("id")) for row in await self.list() if row.get("id")]

    async def default(self) -> Optional[str]:
        configured = getattr(self._client, "default_model", None)
        if configured:
            return str(configured)
        ids = await self.ids()
        return ids[0] if ids else None
