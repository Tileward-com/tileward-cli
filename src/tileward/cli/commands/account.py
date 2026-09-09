"""`twcli account` — balance, plan, usage, and the audit trail."""

from __future__ import annotations

from typing import Optional

import click

from ..main import Ctx, common, pass_ctx
from ..output import rows_from


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
    ctx.out.table(
        rows_from(rows),
        ["day", "tokens", "cost_usd", "requests"],
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
        ["ts", "model", "decision", "key_id", "tokens", "request_id"],
        empty="No audit rows.",
    )


@account_group.command("savings")
@common()
@pass_ctx
def savings(ctx: Ctx) -> None:
    """What Context has saved: stored bytes, and tokens not re-sent."""
    ctx.require_session()
    payload = ctx.client.account.context_savings()
    ctx.emit(payload)
    if isinstance(payload, dict):
        ctx.out.pairs(payload, title="context savings")


def register(cli: click.Group) -> None:
    cli.add_command(account_group)
