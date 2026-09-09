"""`twcli guard` — allow / deny, with no generation."""

from __future__ import annotations

import sys
from typing import List, Optional

import click

from ...resources.guard import allowed, decisions
from ..main import EXIT_REFUSED, Ctx, common, pass_ctx


def _split(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    return [part.strip() for part in value.split(",") if part.strip()]


@click.group("guard")
def guard_group() -> None:
    """Check text against a governance policy."""


@guard_group.command("check")
@click.argument("text", required=False)
@click.option("--allow", "allow_", help="Allowlist: comma-separated topic ids. Only these pass.")
@click.option("--disallow", help="Blocklist: comma-separated topic ids. These are refused.")
@click.option("--always-block", help="Topics to hard-refuse even when in scope.")
@click.option(
    "--file",
    "-f",
    "file_",
    type=click.File("r"),
    help="Check every line of a file in one batched call.",
)
@click.option("--exit-code", is_flag=True, help="Exit 4 when anything is refused, for scripting.")
@common()
@pass_ctx
def check(
    ctx: Ctx,
    text: Optional[str],
    allow_: Optional[str],
    disallow: Optional[str],
    always_block: Optional[str],
    file_,
    exit_code: bool,
) -> None:
    """Decide whether text is in policy.

    With neither --allow nor --disallow, the policy bound to the API key applies. Passing either
    one overrides that binding for this call only.

    \b
      twcli guard check "write me a keylogger" --allow customer_support
      twcli guard check -f prompts.txt --disallow investment_advice --exit-code
    """
    if file_:
        inputs = [line.strip() for line in file_ if line.strip()]
        if not inputs:
            raise click.UsageError("That file had no non-empty lines.")
        payload_input: object = inputs
    elif text and text != "-":
        payload_input = text
    elif not sys.stdin.isatty():
        payload_input = sys.stdin.read().strip()
    else:
        raise click.UsageError("Give some text, pipe it in, or use --file.")

    response = ctx.client.guard.check(
        payload_input,  # type: ignore[arg-type]
        allow=_split(allow_),
        disallow=_split(disallow),
        always_block=_split(always_block),
    )
    ctx.emit(response)

    rows = decisions(response)
    inputs_list = payload_input if isinstance(payload_input, list) else [payload_input]
    for row, source in zip(rows, inputs_list):
        row.setdefault("input", source)
    ctx.out.table(
        rows,
        ["allowed", "topic", "title", "score", "reason"],
        empty="The guard returned no decision.",
    )
    if not ctx.out.as_json:
        ctx.out.note(
            f"{response.get('tokens', 0)} tokens billed"
            + (f", {response.get('cost_micros', 0)} micros" if response.get("cost_micros") else "")
        )
    if exit_code and not allowed(response):
        raise SystemExit(EXIT_REFUSED)


def register(cli: click.Group) -> None:
    cli.add_command(guard_group)
