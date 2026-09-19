"""`twcli launch` -- open a coding agent CLI with Tileward as its model backend.

Claude Code and Codex do not speak Tileward's `/v1/chat/completions`, so `launch claude` and
`launch codex` run a small local translation proxy for the run's lifetime (see `cli/agents/`).
`launch opencode` needs none of that -- opencode speaks it natively.
"""

from __future__ import annotations

from typing import Optional, Tuple

import click

from ..agents import runner
from ..main import Ctx, common, pass_ctx

# Unrecognized flags fall through to `args` instead of erroring, so `-p` meant for the child
# isn't mistaken for one of ours.
_PASSTHROUGH = {"ignore_unknown_options": True, "allow_interspersed_args": False}


@click.group("launch")
def launch_group() -> None:
    """Open a coding agent CLI with Tileward as its model backend."""


@launch_group.command("claude", context_settings=_PASSTHROUGH)
@click.option("--model", "-m", help="Tileward model id to serve. Defaults to the account default.")
@click.option(
    "--fast-model", help="Model id for Claude Code's small/fast calls. Defaults to --model."
)
@click.option(
    "--port", type=int, default=0, help="Fixed local proxy port. Defaults to an OS-assigned one."
)
@click.option(
    "--compaction-hooks/--no-compaction-hooks",
    default=True,
    help="Log Claude Code's compaction events to <config dir>/claude-launch/compactions.jsonl.",
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@common()
@pass_ctx
def launch_claude(
    ctx: Ctx,
    model: Optional[str],
    fast_model: Optional[str],
    port: int,
    compaction_hooks: bool,
    args: Tuple[str, ...],
) -> None:
    """Start Claude Code against a Tileward-served model.

    Runs a local proxy translating the Anthropic Messages API into a Tileward chat completion --
    Claude Code has no native path to Tileward's own hosted models otherwise.

    \b
      twcli launch claude
      twcli launch claude --model Tileward-Qwen3.8-27b
      twcli launch claude -- -p "explain this repo"
    """
    raise SystemExit(runner.launch_claude(ctx, model, fast_model, port, args, compaction_hooks))


@launch_group.command("codex", context_settings=_PASSTHROUGH)
@click.option("--model", "-m", help="Tileward model id to serve. Defaults to the account default.")
@click.option(
    "--port", type=int, default=0, help="Fixed local proxy port. Defaults to an OS-assigned one."
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@common()
@pass_ctx
def launch_codex(ctx: Ctx, model: Optional[str], port: int, args: Tuple[str, ...]) -> None:
    """Start Codex against a Tileward-served model.

    Runs a local proxy translating the OpenAI Responses API into a Tileward chat completion --
    Codex dropped chat-completions support in 0.122, and Tileward has no /v1/responses of its own.

    \b
      twcli launch codex
      twcli launch codex -- exec "explain this repo"
    """
    raise SystemExit(runner.launch_codex(ctx, model, port, args))


@launch_group.command("opencode", context_settings=_PASSTHROUGH)
@click.option("--model", "-m", help="Tileward model id to serve. Defaults to the account default.")
@click.option(
    "--desktop",
    is_flag=True,
    help="Launch the OpenCode Desktop app (macOS only) instead of the terminal CLI.",
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@common()
@pass_ctx
def launch_opencode(
    ctx: Ctx, model: Optional[str], desktop: bool, args: Tuple[str, ...]
) -> None:
    """Start opencode against a Tileward-served model.

    opencode already speaks Tileward's chat-completions API natively -- this just writes a
    `provider.tileward` block into its config and runs it. No proxy involved.

    \b
      twcli launch opencode
      twcli launch opencode -- run "explain this repo"
      twcli launch opencode --desktop
    """
    raise SystemExit(runner.launch_opencode(ctx, model, args, desktop=desktop))


def register(cli: click.Group) -> None:
    cli.add_command(launch_group)
