"""`twcli proxy serve` -- run the local translating proxy in the foreground.

For a supervisor that owns the proxy's lifetime, such as the Tileward desktop app, rather than for
a person at a terminal: `twcli launch codex` still runs its own proxy for each session. The
supervisor picks the port, holds the token, and stops the process when it is done with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import click

from ..agents import chat, responses, runner
from ..agents.proxy import Proxy, run_until_stopped
from ..main import Ctx, common, pass_ctx


@click.group("proxy")
def proxy_group() -> None:
    """Run the local proxy a desktop app talks to."""


@proxy_group.command("serve")
@click.option("--port", type=click.IntRange(1, 65535), required=True, help="Loopback port to bind.")
@click.option(
    "--token-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="File holding the bearer token clients must send. Keep it 0600.",
)
@click.option("--model", "-m", help="Tileward model id to serve. Defaults to the account default.")
@common()
@pass_ctx
def serve(ctx: Ctx, port: int, token_file: Path, model: Optional[str]) -> None:
    """Serve the OpenAI Responses and chat completions APIs on 127.0.0.1 until stopped.

    Responses is translated for Codex; chat completions is passed through, for clients that
    already speak Tileward's own API and only need the proxy to hold the key.

    Runs until SIGTERM or SIGINT. The token comes from a file rather than an argument so it never
    shows in a process listing.

    \b
      twcli proxy serve --port 51713 --token-file ~/.config/tileward/proxy-token
    """
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise click.UsageError(f"{token_file} is empty.", ctx=click.get_current_context())
    resolved = runner.resolve_model(ctx, model)
    try:
        proxy = Proxy(
            client=ctx.client,
            adapter=responses,
            model=resolved,
            routes={"/v1/responses": "responses"},
            port=port,
            token=token,
            extra_routes={"/v1/chat/completions": ("chat", chat)},
            error_log=runner.proxy_error_log(),
        )
    except OSError as exc:
        raise click.ClickException(
            f"Could not listen on 127.0.0.1:{port}: {exc.strerror or exc}"
        ) from exc
    run_until_stopped(proxy, lambda: ctx.out.note(f"serving {proxy.base_url} -- model {resolved}"))


def register(cli: click.Group) -> None:
    cli.add_command(proxy_group)
