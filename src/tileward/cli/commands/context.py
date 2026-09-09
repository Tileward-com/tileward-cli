"""`twcli context` — recall, remember, pin, forget.

EVERY COMMAND HERE IS SCOPED BY `-c/--conversation`, and the scoping is not cosmetic. Without one,
every thread on the key writes into a single shared store, and a later recall hands back an
unrelated conversation's material as if it were this one's. The commands warn once when they are
about to write into the shared default.
"""

from __future__ import annotations

import sys
from typing import Optional

import click

from ...resources.context import text_of
from ..main import Ctx, common, pass_ctx
from ..output import rows_from, truncate


def _warn_unscoped(ctx: Ctx, verb: str) -> None:
    if not ctx.client.conversation:
        ctx.out.warn(
            f"No -c/--conversation: this {verb} goes into the key's shared default store, "
            "which every unscoped conversation also reads."
        )


@click.group("context")
def context_group() -> None:
    """Work with Tileward Context: the slice of history a question needs."""


@context_group.command("recall")
@click.argument("query", required=False)
@click.option("--budget", type=int, help="Force a fixed token ceiling instead of the adaptive one.")
@click.option("--window", type=int, help="Your model's context window, for a better automatic cap.")
@click.option("--max-items", type=int, help="Cap the number of items returned.")
@click.option(
    "--scope",
    help="'all' to search every conversation on the key, or 'topic:<name>' for a labelled set.",
)
@click.option("--tag", "tags", multiple=True, help="Narrow to these tags. Repeatable.")
@click.option("--no-ingest", is_flag=True, help="Do not store the query itself.")
@click.option("--text-only", is_flag=True, help="Print just the block, for piping into a prompt.")
@common(conversation=True)
@pass_ctx
def recall(
    ctx: Ctx,
    query: Optional[str],
    budget: Optional[int],
    window: Optional[int],
    max_items: Optional[int],
    scope: Optional[str],
    tags,
    no_ingest: bool,
    text_only: bool,
) -> None:
    """Retrieve only the history relevant to QUERY."""
    if not query or query == "-":
        if sys.stdin.isatty():
            raise click.UsageError("Give a query or pipe one in.")
        query = sys.stdin.read().strip()
    bundle = ctx.client.context.recall(
        query,
        budget_tokens=budget,
        context_window=window,
        max_items=max_items,
        scope=scope,
        tags=list(tags) or None,
        ingest=False if no_ingest else None,
    )
    ctx.emit(bundle)
    if ctx.out.as_json:
        return
    block = text_of(bundle)
    if text_only:
        ctx.out.raw(block + ("\n" if not block.endswith("\n") else ""))
        return
    if block:
        ctx.out.raw(block + "\n")
    else:
        ctx.out.print("[dim]Nothing recalled for that query.[/dim]")
    stats = {
        k: bundle.get(k)
        for k in ("items", "tokens", "budget_tokens", "compression", "saved_tokens")
        if isinstance(bundle, dict) and bundle.get(k) is not None
    }
    if stats:
        ctx.out.note(", ".join(f"{k}={v}" for k, v in stats.items()))


@context_group.command("remember")
@click.argument("text", required=False)
@click.option("--role", default="user", show_default=True, help="user | assistant | system.")
@click.option("--tag", "tags", multiple=True, help="Tag this turn. Repeatable.")
@common(conversation=True)
@pass_ctx
def remember(ctx: Ctx, text: Optional[str], role: str, tags) -> None:
    """Store a turn so later recalls can find it."""
    if not text or text == "-":
        if sys.stdin.isatty():
            raise click.UsageError("Give some text or pipe it in.")
        text = sys.stdin.read().strip()
    _warn_unscoped(ctx, "write")
    result = ctx.client.context.remember(text, role=role, tags=list(tags) or None)
    ctx.emit(result)
    ctx.out.ok(f"Stored {len(text)} characters as {role}.")


@context_group.command("show")
@click.option("--budget", type=int, help="Token ceiling for the primer.")
@click.option("--window", type=int, help="Your model's context window.")
@common(conversation=True)
@pass_ctx
def show(ctx: Ctx, budget: Optional[int], window: Optional[int]) -> None:
    """A query-less primer of the live conversation state."""
    bundle = ctx.client.context.primer(budget_tokens=budget, context_window=window)
    ctx.emit(bundle)
    if not ctx.out.as_json:
        ctx.out.raw(text_of(bundle) + "\n")


@context_group.command("pin")
@click.argument("text")
@common(conversation=True)
@pass_ctx
def pin(ctx: Ctx, text: str) -> None:
    """Pin a durable fact that every recall includes, whatever the query."""
    _warn_unscoped(ctx, "pin")
    result = ctx.client.context.pin(text)
    ctx.emit(result)
    pin_id = result.get("pid") or result.get("id") if isinstance(result, dict) else None
    suffix = f" (id {pin_id})" if pin_id is not None else ""
    ctx.out.ok(f"Pinned{suffix}: {truncate(text)}")


@context_group.command("unpin")
@click.argument("pin_id", type=int)
@common(conversation=True)
@pass_ctx
def unpin(ctx: Ctx, pin_id: int) -> None:
    """Remove a pin by its id."""
    result = ctx.client.context.unpin(pin_id)
    ctx.emit(result)
    ctx.out.ok(f"Unpinned {pin_id}.")


@context_group.command("forget")
@click.argument("query")
@common(conversation=True)
@pass_ctx
def forget(ctx: Ctx, query: str) -> None:
    """Retire a topic from recall.

    THIS IS NOT A DELETE. It stops the material being returned; the stored turns remain. Use
    `twcli context purge` if the data itself has to go.
    """
    result = ctx.client.context.forget(query)
    ctx.emit(result)
    ctx.out.ok(f"Retired from recall: {truncate(query)}")
    ctx.out.note("The turns themselves are still stored — `twcli context purge` removes data.")


@context_group.command("topics")
@click.argument("topics", nargs=-1)
@click.option("--replace", is_flag=True, help="Replace the existing labels instead of adding.")
@common(conversation=True)
@pass_ctx
def topics(ctx: Ctx, topics, replace: bool) -> None:
    """Label this conversation so `--scope topic:<name>` can search across it."""
    if not topics:
        raise click.UsageError("Name at least one topic.")
    result = ctx.client.context.set_topics(list(topics), replace=replace)
    ctx.emit(result)
    ctx.out.ok(("Replaced" if replace else "Added") + " topics: " + ", ".join(topics))


@context_group.command("stats")
@common(conversation=True)
@pass_ctx
def stats(ctx: Ctx) -> None:
    """Size and shape of the store for this conversation."""
    payload = ctx.client.context.stats()
    ctx.emit(payload)
    if isinstance(payload, dict):
        ctx.out.pairs(payload, title=ctx.client.conversation or "default conversation")


@context_group.command("threads")
@common(conversation=True)
@pass_ctx
def threads(ctx: Ctx) -> None:
    """Every conversation on the account. Needs a signed-in session."""
    ctx.require_session()
    payload = ctx.client._transport.request("GET", "/api/context/threads", auth="session")
    ctx.emit(payload)
    rows = payload.get("threads") if isinstance(payload, dict) else payload
    ctx.out.table(
        rows_from(rows or []),
        ["conversation", "title", "turns", "tokens", "updated"],
        empty="No conversations stored yet.",
    )


@context_group.command("reset")
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
@common(conversation=True)
@pass_ctx
def reset(ctx: Ctx, yes: bool) -> None:
    """Empty ONE conversation's store."""
    target = ctx.client.conversation or "default"
    if not yes:
        click.confirm(f"Empty the store for conversation {target!r}?", abort=True)
    result = ctx.client.context.reset()
    ctx.emit(result)
    ctx.out.ok(f"Reset {target}.")


@context_group.command("purge")
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
@common(conversation=True)
@pass_ctx
def purge(ctx: Ctx, yes: bool) -> None:
    """Delete every conversation's stored data on this key. No undo.

    Unlike `forget`, this removes the data rather than hiding it from recall — which is what you
    need if a document has to be genuinely gone, because deleting a document on its own leaves
    material already folded into a summary behind.
    """
    if not yes:
        click.confirm("Delete ALL stored context on this key? This cannot be undone.", abort=True)
    result = ctx.client.context.purge_account()
    ctx.emit(result)
    ctx.out.ok("Purged.")


def register(cli: click.Group) -> None:
    cli.add_command(context_group)
