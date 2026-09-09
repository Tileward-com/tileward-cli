"""Tileward Context: recall, remember, pin, forget, topics, stats.

Scope every call to a conversation. Without one, every thread on the key shares a single
store and a recall returns another thread's material.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Dict, List, Optional

RECALL = "tileward_recall"
REMEMBER = "tileward_remember"
REMEMBER_MANY = "tileward_remember_many"
CONTEXT = "tileward_context"
PIN = "tileward_pin"
UNPIN = "tileward_unpin"
FORGET = "tileward_forget"
SET_TOPICS = "tileward_set_topics"
STATS = "tileward_stats"
RESET = "tileward_reset"
CLEAR_ACCOUNT = "tileward_clear_account"
PURGE_ACCOUNT = "tileward_purge_account"


def recall_args(
    query: str,
    *,
    budget_tokens: Optional[int] = None,
    context_window: Optional[int] = None,
    max_items: Optional[int] = None,
    lam: Optional[float] = None,
    decompose: Optional[bool] = None,
    scope: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    ingest: Optional[bool] = None,
) -> Dict[str, Any]:
    """Only what the caller actually set.

    Sending `budget_tokens=0` is not the same as omitting it in spirit even though the server
    treats them alike today; leaving unset values out keeps the request describing the caller's
    intent rather than this client's defaults.
    """
    args: Dict[str, Any] = {"query": query}
    if budget_tokens is not None:
        args["budget_tokens"] = int(budget_tokens)
    if context_window is not None:
        args["context_window"] = int(context_window)
    if max_items is not None:
        args["max_items"] = int(max_items)
    if lam is not None:
        args["lam"] = float(lam)
    if decompose is not None:
        args["decompose"] = bool(decompose)
    if scope:
        args["scope"] = scope
    if tags:
        args["tags"] = list(tags)
    if ingest is not None:
        args["ingest"] = bool(ingest)
    return args


def text_of(bundle: Any) -> str:
    """The block you paste into a prompt."""
    if isinstance(bundle, dict):
        value = bundle.get("text")
        if isinstance(value, str):
            return value
    return str(bundle or "")


class _ContextBase:
    def __init__(self, client: Any) -> None:
        self._client = client

    def _conv(self, conversation: Optional[str]) -> Optional[str]:
        return conversation or getattr(self._client, "conversation", None)


class Context(_ContextBase):
    def _call(self, tool: str, args: Dict[str, Any], conversation: Optional[str]) -> Any:
        return self._client._context_transport.call(
            tool, args, conversation=self._conv(conversation)
        )

    # ---- reading -----------------------------------------------------------------------
    def recall(
        self, query: str, *, conversation: Optional[str] = None, **kwargs: Any
    ) -> Dict[str, Any]:
        """The relevant slice of history for `query`, sized automatically."""
        return self._call(RECALL, recall_args(query, **kwargs), conversation)

    def recall_text(self, query: str, **kwargs: Any) -> str:
        """Just the text block, for dropping straight into a prompt."""
        return text_of(self.recall(query, **kwargs))

    def primer(
        self,
        *,
        budget_tokens: Optional[int] = None,
        context_window: Optional[int] = None,
        conversation: Optional[str] = None,
    ) -> Dict[str, Any]:
        """A query-less summary of the live conversation state."""
        args: Dict[str, Any] = {}
        if budget_tokens is not None:
            args["budget_tokens"] = int(budget_tokens)
        if context_window is not None:
            args["context_window"] = int(context_window)
        return self._call(CONTEXT, args, conversation)

    def stats(self, *, conversation: Optional[str] = None) -> Dict[str, Any]:
        return self._call(STATS, {}, conversation)

    # ---- writing -----------------------------------------------------------------------
    def remember(
        self,
        text: str,
        *,
        role: str = "user",
        tags: Optional[Sequence[str]] = None,
        source: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        conversation: Optional[str] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {"text": text, "role": role}
        if tags:
            args["tags"] = list(tags)
        if source:
            args["source"] = source
        if idempotency_key:
            args["idempotency_key"] = idempotency_key
        return self._call(REMEMBER, args, conversation)

    def remember_many(
        self, turns: Sequence[Dict[str, Any]], *, conversation: Optional[str] = None
    ) -> Dict[str, Any]:
        """Ingest a batch of `{role, text}` turns in one call."""
        return self._call(REMEMBER_MANY, {"turns": [dict(t) for t in turns]}, conversation)

    def pin(self, text: str, *, conversation: Optional[str] = None) -> Dict[str, Any]:
        """A durable fact that every recall includes, whatever the query."""
        return self._call(PIN, {"text": text}, conversation)

    def unpin(self, pin_id: int, *, conversation: Optional[str] = None) -> Dict[str, Any]:
        return self._call(UNPIN, {"pid": int(pin_id)}, conversation)

    def forget(self, query: str, *, conversation: Optional[str] = None) -> Dict[str, Any]:
        """Drop a topic from recall.

        NOT A DELETE. This retires material from what recall will return; the stored turns
        themselves stay. `purge_account()` is the one that removes data.
        """
        return self._call(FORGET, {"query": query}, conversation)

    def set_topics(
        self,
        topics: Sequence[str],
        *,
        replace: bool = False,
        conversation: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Label this conversation, so `scope="topic:<name>"` can recall across it."""
        return self._call(
            SET_TOPICS, {"topics": list(topics), "replace": bool(replace)}, conversation
        )

    # ---- destructive -------------------------------------------------------------------
    def reset(self, *, conversation: Optional[str] = None) -> Dict[str, Any]:
        """Empty ONE conversation's store."""
        return self._call(RESET, {}, conversation)

    def clear_account(self) -> Dict[str, Any]:
        """Empty every conversation on the key. There is no undo."""
        return self._call(CLEAR_ACCOUNT, {}, None)

    def purge_account(self) -> Dict[str, Any]:
        """Delete the stored data itself, not just its visibility to recall. No undo."""
        return self._call(PURGE_ACCOUNT, {}, None)


class AsyncContext(_ContextBase):
    async def _call(self, tool: str, args: Dict[str, Any], conversation: Optional[str]) -> Any:
        return await self._client._context_transport.acall(
            tool, args, conversation=self._conv(conversation)
        )

    async def recall(
        self, query: str, *, conversation: Optional[str] = None, **kwargs: Any
    ) -> Dict[str, Any]:
        return await self._call(RECALL, recall_args(query, **kwargs), conversation)

    async def recall_text(self, query: str, **kwargs: Any) -> str:
        return text_of(await self.recall(query, **kwargs))

    async def primer(
        self,
        *,
        budget_tokens: Optional[int] = None,
        context_window: Optional[int] = None,
        conversation: Optional[str] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {}
        if budget_tokens is not None:
            args["budget_tokens"] = int(budget_tokens)
        if context_window is not None:
            args["context_window"] = int(context_window)
        return await self._call(CONTEXT, args, conversation)

    async def stats(self, *, conversation: Optional[str] = None) -> Dict[str, Any]:
        return await self._call(STATS, {}, conversation)

    async def remember(
        self,
        text: str,
        *,
        role: str = "user",
        tags: Optional[Sequence[str]] = None,
        source: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        conversation: Optional[str] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {"text": text, "role": role}
        if tags:
            args["tags"] = list(tags)
        if source:
            args["source"] = source
        if idempotency_key:
            args["idempotency_key"] = idempotency_key
        return await self._call(REMEMBER, args, conversation)

    async def remember_many(
        self, turns: Sequence[Dict[str, Any]], *, conversation: Optional[str] = None
    ) -> Dict[str, Any]:
        return await self._call(REMEMBER_MANY, {"turns": [dict(t) for t in turns]}, conversation)

    async def pin(self, text: str, *, conversation: Optional[str] = None) -> Dict[str, Any]:
        return await self._call(PIN, {"text": text}, conversation)

    async def unpin(self, pin_id: int, *, conversation: Optional[str] = None) -> Dict[str, Any]:
        return await self._call(UNPIN, {"pid": int(pin_id)}, conversation)

    async def forget(self, query: str, *, conversation: Optional[str] = None) -> Dict[str, Any]:
        return await self._call(FORGET, {"query": query}, conversation)

    async def set_topics(
        self, topics: Sequence[str], *, replace: bool = False, conversation: Optional[str] = None
    ) -> Dict[str, Any]:
        return await self._call(
            SET_TOPICS, {"topics": list(topics), "replace": bool(replace)}, conversation
        )

    async def reset(self, *, conversation: Optional[str] = None) -> Dict[str, Any]:
        return await self._call(RESET, {}, conversation)

    async def clear_account(self) -> Dict[str, Any]:
        return await self._call(CLEAR_ACCOUNT, {}, None)

    async def purge_account(self) -> Dict[str, Any]:
        return await self._call(PURGE_ACCOUNT, {}, None)


def documents_of(payload: Any) -> List[Dict[str, Any]]:
    """The document rows out of a `list_documents` response, whatever it wrapped them in."""
    if isinstance(payload, dict):
        for field in ("documents", "items", "rows", "data"):
            value = payload.get(field)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []
