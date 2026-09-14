"""`twcli account` — balance, plan, usage, and the audit trail."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

import click

from ... import errors
from ..main import Ctx, common, pass_ctx
from ..output import rows_from, truncate

MICROS_PER_USD = 1_000_000


def _usd(micros: object) -> Optional[float]:
    return micros / MICROS_PER_USD if isinstance(micros, (int, float)) else None


def _pct(value: object) -> Optional[str]:
    return f"{value:.1f}%" if isinstance(value, (int, float)) else None


# A month is 30 days and a year 365, matching the API's own `30d` and `1y`.
_UNIT_HOURS = {"h": 1, "d": 24, "w": 24 * 7, "m": 24 * 30, "y": 24 * 365}
# The names the API uses for the windows it offers, by their hours, so `1d` asks for `24h`.
_WINDOW_NAMES = {6: "6h", 24: "24h", 120: "5d", 720: "30d", 8760: "1y"}


def parse_window(text: str) -> Tuple[str, Optional[int]]:
    """A window such as `30d`, `1w` or `1y`, as the name to ask the API for and its hours.

    `all` has no hours. A window the API has no name for is asked for by its length in days, or in
    hours when it is not a whole number of days.
    """
    value = text.strip().lower()
    if value == "all":
        return "all", None
    match = re.fullmatch(r"(\d+)([hdwmy])", value)
    if not match or int(match.group(1)) == 0:
        raise click.BadParameter(
            "use a number and a unit (h, d, w, m or y), such as 30d or 1w, or all",
            param_hint="WINDOW",
        )
    hours = int(match.group(1)) * _UNIT_HOURS[match.group(2)]
    if hours in _WINDOW_NAMES:
        return _WINDOW_NAMES[hours], hours
    return (f"{hours // 24}d" if hours % 24 == 0 else f"{hours}h"), hours


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
@click.option("--key-id", type=int, help="Only requests made with this key.")
@click.option(
    "--outcome",
    help="Only this outcome: allowed, rejected, refused or failed. The guard's refusals are "
    "rejected; refused is a model that declined.",
)
@click.option("--limit", type=int, help="Rows to return, up to 50.")
@common()
@pass_ctx
def audit(ctx: Ctx, key_id: Optional[int], outcome: Optional[str], limit: Optional[int]) -> None:
    """Request history — metadata only, never prompt or completion text."""
    ctx.require_session()
    payload = ctx.client.account.audit(key_id=key_id, outcome=outcome, limit=limit)
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
@click.argument("window", required=False, default="30d")
@click.option(
    "--conversation", "by_conversation", is_flag=True, help="Add savings by conversation."
)
@click.option("--detail", is_flag=True, help="Add savings by conversation and by day.")
@common()
@pass_ctx
def savings(ctx: Ctx, window: str, by_conversation: bool, detail: bool) -> None:
    """What Context saved over WINDOW: a number and a unit (h, d, w, m, y), or all. Default 30d.

    \b
      twcli account savings
      twcli account savings 5d --conversation
      twcli account savings 1y --detail
    """
    name, hours = parse_window(window)
    ctx.require_session()
    payload = ctx.client.account.context_savings(window=name)
    # The API answers a window it does not offer with its default, labelled as the default. Check
    # the hours it measured rather than show them under the window that was asked for.
    measured = payload.get("window_hours") if isinstance(payload, dict) else None
    if isinstance(payload, dict) and payload.get("available") and measured != hours:
        answered = payload.get("window") or "another window"
        raise click.UsageError(
            f"The API did not measure a {window} window; it answered for {answered}."
        )
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
        title=f"context savings, {window.strip().lower()}",
    )

    if not (by_conversation or detail):
        return

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

    if not detail:
        return
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
