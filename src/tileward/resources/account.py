"""Account: balance, plan, entitlements, usage, audit. Session-authenticated.

Rates are set server-side; read them from here rather than copying them into code.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

ACCOUNT = "/api/account"
AUDIT = "/api/account/audit"
BILLING = "/api/billing/summary"
CONTEXT_STATS = "/api/account/twinkle-stats"
CONTEXT_SAVINGS = "/api/account/twinkle-savings"
CONTEXT_DAILY = "/api/account/context/heatmap"


def balance_usd(payload: Dict[str, Any]) -> Optional[float]:
    value = payload.get("balance_usd")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class Account:
    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self) -> Dict[str, Any]:
        return self._client._transport.request("GET", ACCOUNT, auth="session")

    def billing(self) -> Dict[str, Any]:
        return self._client._transport.request("GET", BILLING, auth="session")

    def audit(
        self,
        *,
        key_id: Optional[int] = None,
        outcome: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Request history, metadata only — never prompt or completion text.

        `key_id` and `outcome` (allowed, rejected, refused, failed) narrow it. At most 50 rows come
        back; `total` counts every match.
        """
        return self._client._transport.request(
            "GET",
            AUDIT,
            params={"key_id": key_id, "outcome": outcome, "limit": limit},
            auth="session",
        )

    def context_stats(self) -> Dict[str, Any]:
        return self._client._transport.request("GET", CONTEXT_STATS, auth="session")

    def context_savings(self) -> Dict[str, Any]:
        return self._client._transport.request("GET", CONTEXT_SAVINGS, auth="session")

    def context_savings_daily(self, *, days: Optional[int] = None) -> Dict[str, Any]:
        """Tokens saved per UTC day, up to a year: `series` of `{label, ts, tokens}`."""
        return self._client._transport.request(
            "GET", CONTEXT_DAILY, params={"days": days}, auth="session"
        )


class AsyncAccount:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def get(self) -> Dict[str, Any]:
        return await self._client._transport.request("GET", ACCOUNT, auth="session")

    async def billing(self) -> Dict[str, Any]:
        return await self._client._transport.request("GET", BILLING, auth="session")

    async def audit(
        self,
        *,
        key_id: Optional[int] = None,
        outcome: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        return await self._client._transport.request(
            "GET",
            AUDIT,
            params={"key_id": key_id, "outcome": outcome, "limit": limit},
            auth="session",
        )

    async def context_stats(self) -> Dict[str, Any]:
        return await self._client._transport.request("GET", CONTEXT_STATS, auth="session")

    async def context_savings(self) -> Dict[str, Any]:
        return await self._client._transport.request("GET", CONTEXT_SAVINGS, auth="session")

    async def context_savings_daily(self, *, days: Optional[int] = None) -> Dict[str, Any]:
        return await self._client._transport.request(
            "GET", CONTEXT_DAILY, params={"days": days}, auth="session"
        )
