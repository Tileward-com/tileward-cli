"""`twcli keys` — mint, rotate, and revoke API keys.

NEEDS A SIGNED-IN SESSION, not an API key: a key that can mint keys would survive its own
revocation. Run `twcli auth login` first.

THE SECRET IS PRINTED ONCE. Nothing can retrieve it afterwards, including this command.
"""

from __future__ import annotations

import click

from ...config import fingerprint
from ..main import Ctx, common, pass_ctx
from ..output import rows_from

COLUMNS = ["id", "label", "prefix", "cells", "created", "last_used", "revoked"]


@click.group("keys")
def keys_group() -> None:
    """Manage API keys."""


@keys_group.command("list")
@click.option("--all", "include_revoked", is_flag=True, help="Include revoked keys.")
@common()
@pass_ctx
def list_keys(ctx: Ctx, include_revoked: bool) -> None:
    """Keys on this account."""
    ctx.require_session()
    rows = ctx.client.keys.list(include_revoked=include_revoked)
    ctx.emit(rows)
    ctx.out.table(
        rows_from(rows),
        COLUMNS,
        headers={"cells": "locked topics"},
        empty="No keys yet. Mint one with `twcli keys create --label laptop`.",
    )


@keys_group.command("create")
@click.option("--label", "-l", required=True, help="What this key is for. Shown in the console.")
@click.option(
    "--tile",
    "tiles",
    multiple=True,
    help="Lock the key to a topic, enforced at the gate. Repeatable.",
)
@click.option("--save", is_flag=True, help="Store the new key in this profile for later commands.")
@common()
@pass_ctx
def create(ctx: Ctx, label: str, tiles, save: bool) -> None:
    """Mint a key. The secret is shown once and never again."""
    ctx.require_session()
    result = ctx.client.keys.create(label, cells=list(tiles) or None)
    secret = result.get("key")

    if save and secret:
        ctx.config.set_credentials(api_key=secret)

    ctx.emit(result)
    if ctx.out.as_json:
        return

    ctx.out.ok(f"Created key {result.get('id')} ({label}).")
    ctx.out.print()
    ctx.out.print(f"  [bold]{secret}[/bold]")
    ctx.out.print()
    ctx.out.warn("Copy it now — this is the only time it is shown.")
    if save:
        ctx.out.note(f"Saved to profile {ctx.config.profile} at {ctx.config.credentials_path}.")
    else:
        ctx.out.note("Store it in TILEWARD_API_KEY, or re-run with --save.")


@keys_group.command("rotate")
@click.argument("key_id", type=int)
@click.option("--save", is_flag=True, help="Store the new secret in this profile.")
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
@common()
@pass_ctx
def rotate(ctx: Ctx, key_id: int, save: bool, yes: bool) -> None:
    """Issue a new secret for an existing key.

    Rotating keeps the key's id, and with it the policy bound to the key and its whole usage and
    audit history. Revoking and creating a new one instead would silently drop the configuration
    and split the trail.

    The old secret stops working immediately.
    """
    ctx.require_session()
    if not yes:
        click.confirm(f"Rotate key {key_id}? The current secret stops working.", abort=True)
    result = ctx.client.keys.rotate(key_id)
    secret = result.get("key")
    if save and secret:
        ctx.config.set_credentials(api_key=secret)
    ctx.emit(result)
    if ctx.out.as_json:
        return
    ctx.out.ok(f"Rotated key {key_id}.")
    ctx.out.print()
    ctx.out.print(f"  [bold]{secret}[/bold]")
    ctx.out.print()
    ctx.out.warn("Copy it now — this is the only time it is shown.")


@keys_group.command("revoke")
@click.argument("key_id", type=int)
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
@common()
@pass_ctx
def revoke(ctx: Ctx, key_id: int, yes: bool) -> None:
    """Revoke a key. Immediate, and not reversible."""
    ctx.require_session()
    if not yes:
        click.confirm(f"Revoke key {key_id}? This cannot be undone.", abort=True)
    result = ctx.client.keys.revoke(key_id)
    ctx.emit(result)
    ctx.out.ok(f"Revoked key {key_id}.")
    stored = ctx.config.api_key
    # If they just revoked the key this profile is holding, say so — otherwise the next command
    # fails with a 401 that looks like a different problem entirely.
    if stored:
        ctx.out.note(f"This profile still holds {fingerprint(stored)}.")


@keys_group.command("policy")
@click.argument("key_id", type=int)
@common()
@pass_ctx
def policy(ctx: Ctx, key_id: int) -> None:
    """What governs this key: locked topics, model config, context config."""
    ctx.require_session()
    payload = ctx.client.keys.policy(key_id)
    ctx.emit(payload)
    if isinstance(payload, dict):
        ctx.out.pairs(payload, title=f"key {key_id}")


def register(cli: click.Group) -> None:
    cli.add_command(keys_group)
