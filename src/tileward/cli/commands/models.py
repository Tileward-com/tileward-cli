"""`twcli models` — what is served right now."""

from __future__ import annotations

import click

from ...resources.models import summarize, summarize_all
from ..main import Ctx, common, pass_ctx


@click.group("models")
def models_group() -> None:
    """List and inspect served models."""


@models_group.command("list")
@common()
@pass_ctx
def list_models(ctx: Ctx) -> None:
    """Every model the API is serving.

    This is the authority. The catalogue is data, not code, so ids appear, get repriced, and get
    withdrawn without a release — a model id copied into a script is one that will eventually 404.
    """
    rows = ctx.client.models.list()
    # --json emits the API's own rows, unflattened: a script reading this should see what the
    # endpoint actually returns, not a shape this CLI invented for a terminal.
    ctx.emit(rows)
    ctx.out.table(
        summarize_all(rows),
        ["id", "precision", "context_len", "price_per_mtoken_usd", "compression_ratio"],
        headers={
            "price_per_mtoken_usd": "USD / Mtoken",
            "context_len": "context",
            "compression_ratio": "compression",
        },
        empty="No models are being served on this endpoint.",
    )


@models_group.command("show")
@click.argument("model_id")
@common()
@pass_ctx
def show_model(ctx: Ctx, model_id: str) -> None:
    """Everything the API reports about one model."""
    row = ctx.client.models.retrieve(model_id)
    ctx.emit(row)
    ctx.out.pairs(summarize(row), title=model_id)


def register(cli: click.Group) -> None:
    cli.add_command(models_group)
