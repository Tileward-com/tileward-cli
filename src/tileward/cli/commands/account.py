"""`twcli account` — balance, plan, usage, and the audit trail."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import click

from ... import errors
from ..main import Ctx, common, pass_ctx
from ..output import rows_from, truncate

MICROS_PER_USD = 1_000_000


def _usd(micros: object) -> Optional[float]:
    return micros / MICROS_PER_USD if isinstance(micros, (int, float)) else None


def _pct(value: object) -> Optional[str]:
    return f"{value:.1f}%" if isinstance(value, (int, float)) else None


@click.group("account")
def account_group() -> None:
    """Read the account. Needs a signed-in session."""


@account_group.command("show")
@common()
@pass_ctx
def show(ctx: Ctx) -> None:
    """Balance, plan, and entitlements.

    The rates in this payload are the authoritative ones. They are set server-side and can change,
    so anything that copies a number out of here into code will eventually be quoting a price that
    is no longer charged.
    """
    ctx.require_session()
    payload = ctx.client.account.get()
    ctx.emit(payload)
    if ctx.out.as_json:
        return
    ctx.out.pairs(
        {
            "Email": payload.get("email"),
            "Plan": payload.get("effective_plan") or payload.get("plan"),
            "Balance (USD)": payload.get("balance_usd"),
            "Spent (USD)": payload.get("spent_usd"),
            "Tokens billed": payload.get("tokens_total"),
            "Free credit granted": payload.get("free_granted"),
            "Live keys": len([k for k in payload.get("keys") or [] if not k.get("revoked")]),
            "API base": payload.get("api_base"),
            "Context base": payload.get("mcp_base"),
        },
        title="account",
    )


@account_group.command("usage")
@common()
@pass_ctx
def usage(ctx: Ctx) -> None:
    """Day-by-day token usage."""
    ctx.require_session()
    payload = ctx.client.account.get()
    rows = payload.get("by_day") or []
    ctx.emit(rows)
    # A day comes back as `d`, `tok`, and `cost` in micro-dollars. The payload has no per-day
    # request count, so there is no requests column.
    ctx.out.table(
        [
            {"day": row.get("d"), "tokens": row.get("tok"), "cost_usd": _usd(row.get("cost"))}
            for row in rows_from(rows)
        ],
        ["day", "tokens", "cost_usd"],
        empty="No usage recorded yet.",
    )


@account_group.command("audit")
@click.option("--limit", type=int, help="Rows to return.")
@common()
@pass_ctx
def audit(ctx: Ctx, limit: Optional[int]) -> None:
    """Request history — metadata only, never prompt or completion text."""
    ctx.require_session()
    payload = ctx.client.account.audit(limit=limit)
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    ctx.emit(payload)
    ctx.out.table(
        rows_from(rows or []),
        ["ts", "model", "outcome", "key_id", "total_tokens", "request_id"],
        headers={"ts": "time", "total_tokens": "tokens"},
        timestamps=("ts",),
        empty="No audit rows.",
    )


@account_group.command("savings")
@common()
@pass_ctx
def savings(ctx: Ctx) -> None:
    """What Context has saved: tokens not re-sent, in total, by conversation, and by day."""
    ctx.require_session()
    payload = ctx.client.account.context_savings()
    ctx.emit(payload)
    if ctx.out.as_json or not isinstance(payload, dict):
        return
    if not payload.get("available"):
        ctx.out.note("Context savings are not available for this account.")
        return
    stale = payload.get("stale") or payload.get("snapshot_writer_stalled")
    age = payload.get("snapshot_age_hours")
    if stale and isinstance(age, (int, float)):
        ctx.out.warn(f"These figures come from a snapshot {age:.1f} hours old.")

    ctx.out.pairs(
        {
            "Tokens saved": payload.get("saved"),
            "Tokens sent": payload.get("spent"),
            "Reduction": _pct(payload.get("reduction_pct")),
            "Saved, all time": payload.get("lifetime"),
            "Reduction, all time": _pct(payload.get("lifetime_reduction_pct")),
        },
        title=f"context savings, {payload.get('window') or 'window'}",
    )

    conversations = [
        {
            "conversation": truncate(row.get("title") or row.get("conversation") or "", 44),
            "client": row.get("client"),
            "saved": row.get("saved"),
            "sent": row.get("spent"),
            "reduction": _pct(row.get("reduction_pct")),
        }
        for row in rows_from(payload.get("by_conversation") or [])
    ]
    if conversations:
        ctx.out.print()
        ctx.out.table(
            conversations,
            ["conversation", "client", "saved", "sent", "reduction"],
            title="by conversation",
        )

    days = _days_in_window(ctx, payload.get("window_hours"))
    if days:
        ctx.out.print()
        ctx.out.table(days, ["day", "saved"], title="by day")


def _days_in_window(ctx: Ctx, window_hours: object) -> List[Dict[str, Any]]:
    """Saved tokens per day across the window, from the daily endpoint.

    Not the savings payload's own `series`: the daily figures are the ones that add up to the
    window's total, so they are the ones worth a table.
    """
    try:
        daily = ctx.client.account.context_savings_daily()
    except errors.TilewardError:
        return []
    if not isinstance(daily, dict) or not daily.get("available"):
        return []
    rows = rows_from(daily.get("series") or [])
    if isinstance(window_hours, (int, float)):
        since = time.time() - window_hours * 3600
        # A day counts if any of it falls inside the window; `ts` is the day's UTC midnight.
        rows = [
            r for r in rows if isinstance(r.get("ts"), (int, float)) and r["ts"] + 86400 > since
        ]
    return [{"day": r.get("label"), "saved": r.get("tokens")} for r in rows]


def register(cli: click.Group) -> None:
    cli.add_command(account_group)
