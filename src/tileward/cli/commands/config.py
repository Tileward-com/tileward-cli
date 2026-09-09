"""`twcli config` — see and change what a profile holds.

PROFILES ARE HOW ONE MACHINE TALKS TO TWO ACCOUNTS: `twcli -p staging chat "..."` reads a
different key and a different host, with no environment juggling.
"""

from __future__ import annotations

import click

from ..main import Ctx, common, pass_ctx

SETTABLE = ["base_url", "context_url", "console_url", "model", "conversation"]


@click.group("config")
def config_group() -> None:
    """Inspect and edit stored settings."""


@config_group.command("show")
@click.option(
    "--reveal",
    is_flag=True,
    help="Print the API key in full instead of a fingerprint.",
)
@common()
@pass_ctx
def show(ctx: Ctx, reveal: bool) -> None:
    """What this profile resolves to, after the environment is applied."""
    payload = ctx.config.as_dict(redact=not reveal)
    ctx.emit(payload)
    ctx.out.pairs(payload, title=f"profile: {ctx.config.profile}")
    if reveal and not ctx.out.as_json:
        ctx.out.warn("A live key is now in your terminal scrollback and shell history.")


@config_group.command("set")
@click.argument("name", type=click.Choice(SETTABLE))
@click.argument("value")
@common()
@pass_ctx
def set_(ctx: Ctx, name: str, value: str) -> None:
    """Set a value on this profile."""
    ctx.config.set(name, value)
    ctx.out.ok(f"{name} = {value} (profile {ctx.config.profile})")
    ctx.emit({"ok": True, "profile": ctx.config.profile, name: value})


@config_group.command("unset")
@click.argument("name", type=click.Choice(SETTABLE))
@common()
@pass_ctx
def unset(ctx: Ctx, name: str) -> None:
    """Remove a value, falling back to the built-in default."""
    ctx.config.set(name, None)
    ctx.out.ok(f"{name} cleared on profile {ctx.config.profile}.")
    ctx.emit({"ok": True})


@config_group.command("set-key")
@click.option("--key", prompt=True, hide_input=True, help="The tw_live_... secret.")
@common()
@pass_ctx
def set_key(ctx: Ctx, key: str) -> None:
    """Store an API key on this profile.

    Prompted rather than taken as an argument by default, so the secret does not land in shell
    history or in the process list where anyone on the machine can read it with `ps`.
    """
    ctx.config.set_credentials(api_key=key.strip())
    ctx.out.ok(f"Key stored on profile {ctx.config.profile} ({ctx.config.credentials_path}).")


@config_group.command("profiles")
@common()
@pass_ctx
def profiles(ctx: Ctx) -> None:
    """Every profile this machine knows about."""
    names = ctx.config.profile_names()
    ctx.emit(names)
    ctx.out.table(
        [{"profile": n, "active": n == ctx.config.profile} for n in names],
        ["profile", "active"],
        empty="No profiles.",
    )


@config_group.command("path")
@common()
@pass_ctx
def path(ctx: Ctx) -> None:
    """Where the config and credentials files live."""
    payload = {
        "config": str(ctx.config.config_path),
        "credentials": str(ctx.config.credentials_path),
    }
    ctx.emit(payload)
    ctx.out.pairs(payload)


def register(cli: click.Group) -> None:
    cli.add_command(config_group)
