"""The shared launch sequence behind `twcli launch claude|codex|opencode`.

Resolve which Tileward model backs the session, start a local proxy (skipped for opencode, which
needs none), point the target CLI at it without ever handing it the real Tileward key, run it with
inherited stdio so its own interactive UI behaves exactly as if launched directly, and always tear
the proxy down when it exits.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Optional, Sequence

import click

from ... import errors
from ...client import openai_base_url
from ...config import config_dir
from ...resources.models import summarize
from . import anthropic, claude_config, codex_config, opencode_config, responses
from .proxy import Proxy


def _interactive(ctx: Any) -> bool:
    """A human at a keyboard who can answer a prompt -- not a pipe, a script, or `--json`."""
    return sys.stdin.isatty() and not ctx.out.as_json


def _pick_model(ctx: Any) -> str:
    """No `--model` and no configured default: ask, rather than guess.

    One served model needs no choice. More than one used to mean "silently take whichever the API
    listed first" — exactly the kind of guess `resources/models.py` otherwise refuses to make, and
    the reason `--model` chose the wrong model by default until now. Ask instead, when there is
    someone to ask; a script or a pipe gets a clear error instead of a hang.
    """
    rows = ctx.client.models.list()
    ids = [str(row.get("id")) for row in rows if row.get("id")]
    if not ids:
        raise errors.ConfigError(
            "No model given and the served catalogue is empty. Pass --model, or check "
            "`twcli models list`."
        )
    if len(ids) == 1:
        return ids[0]
    if not _interactive(ctx):
        raise errors.ConfigError(
            "Multiple models are served and no default is configured. Pass --model, set one "
            "with `twcli config set model <id>`, or run this from a terminal to choose "
            "interactively."
        )
    ctx.out.table(
        [{"#": i, **summarize(row)} for i, row in enumerate(rows, start=1)],
        ["#", "id", "context_len", "price_per_mtoken_usd", "compression_ratio"],
        title="Available models",
        headers={"price_per_mtoken_usd": "USD / Mtoken", "context_len": "context"},
    )
    choice = click.prompt(f"Pick a model [1-{len(ids)}]", type=click.IntRange(1, len(ids)))
    picked = ids[choice - 1]
    ctx.out.note(f"Set a default to skip this next time: twcli config set model {picked}")
    return picked


def resolve_model(ctx: Any, requested: Optional[str]) -> str:
    """`--model`, validated against what's actually served; else the configured default; else,
    when more than one model is served, ask which one to use.

    Never a hardcoded fallback id — the served catalogue is data, not code (see
    `resources/models.py`), so a literal here would eventually 404 quietly.
    """
    client = ctx.client
    if requested:
        client.models.retrieve(requested)  # raises NotFoundError naming what IS served, if wrong
        return requested
    if client.default_model:
        return str(client.default_model)
    return _pick_model(ctx)


def _binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise click.ClickException(
            f"`{name}` was not found on PATH. Install it and make sure it's on PATH, then "
            f"try `twcli launch` again."
        )
    return path


def _run_child(binary: str, args: Sequence[str], env: dict) -> int:
    """Inherit stdio: the child owns the terminal exactly as if it had been run directly."""
    proc = subprocess.Popen([binary, *args], env=env)
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # Ctrl-C already reached the child too (same foreground process group); just wait.
        return proc.wait()


def _declare_context_window(ctx: Any, model_id: str, env: dict) -> None:
    """Tell Claude Code the window it can actually compact against (see `claude_config.py`).

    An explicit `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is the user's call and is left alone. A catalogue
    row with no usable context length leaves Claude Code on its own default.
    """
    if claude_config.WINDOW_ENV in env:
        return
    try:
        context = claude_config.served_context(ctx.client.models.retrieve(model_id))
    except errors.TilewardError:
        return
    window = claude_config.compaction_window(context, env)
    reserve = claude_config.output_reserve(env)
    if window is None:
        if context is not None:
            ctx.out.warn(
                f"{model_id!r} serves a {context:,}-token context, no more than Claude Code's "
                f"{reserve:,}-token output reserve -- Claude Code is unlikely to work on it."
            )
        return
    env[claude_config.WINDOW_ENV] = str(window)
    ctx.out.note(
        f"Claude Code compacts against {window:,} tokens ({context:,} served, less "
        f"{reserve:,} reserved for output)"
    )


def launch_claude(
    ctx: Any, model: Optional[str], fast_model: Optional[str], port: int, args: Sequence[str], compaction_hooks: bool
) -> int:
    binary = _binary("claude")
    resolved = resolve_model(ctx, model)
    env = dict(os.environ)
    # Before the proxy starts: a catalogue failure here must not strand a running proxy thread.
    _declare_context_window(ctx, resolved, env)
    child_args = list(args)
    if compaction_hooks:
        if claude_config.passes_own_settings(args):
            ctx.out.note(
                "Your own --settings is passed through, so twcli's compaction hooks are off for "
                "this run: Claude Code keeps only the last --settings it is given."
            )
        else:
            settings = claude_config.hook_settings(claude_config.compaction_log_path())
            child_args = ["--settings", settings, *child_args]
    proxy = Proxy(
        client=ctx.client,
        adapter=anthropic,
        model=resolved,
        routes={"/v1/messages": "messages", "/v1/messages/count_tokens": "count_tokens", "/v1/models": "models"},
        port=port,
    )
    proxy.start()
    ctx.out.note(f"Tileward proxy on {proxy.base_url} -- model {resolved!r} -> claude")
    env["ANTHROPIC_BASE_URL"] = proxy.base_url
    env["ANTHROPIC_AUTH_TOKEN"] = proxy.token
    # An inherited real Anthropic key would otherwise silently win over the proxy token and send
    # this session straight to api.anthropic.com instead of Tileward.
    env.pop("ANTHROPIC_API_KEY", None)
    env["ANTHROPIC_MODEL"] = resolved
    env["ANTHROPIC_SMALL_FAST_MODEL"] = fast_model or resolved
    # With a real `claude login` session present, Claude Code silently keeps using that Keychain
    # credential over ANTHROPIC_AUTH_TOKEN (verified on the wire); an isolated config dir fixes it.
    claude_home = config_dir() / "claude-launch"
    claude_home.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(claude_home)
    try:
        return _run_child(binary, child_args, env)
    finally:
        proxy.stop()


def launch_codex(ctx: Any, model: Optional[str], port: int, args: Sequence[str]) -> int:
    binary = _binary("codex")
    resolved = resolve_model(ctx, model)
    proxy = Proxy(
        client=ctx.client,
        adapter=responses,
        model=resolved,
        routes={"/v1/responses": "responses"},
        port=port,
    )
    proxy.start()
    token_env = "TILEWARD_PROXY_TOKEN"
    config_path, profile_name = codex_config.merge(
        base_url=f"{proxy.base_url}/v1", token_env=token_env, model_id=resolved
    )
    ctx.out.note(
        f"Tileward proxy on {proxy.base_url} -- model {resolved!r} -> codex ({config_path})"
    )
    env = dict(os.environ)
    env[token_env] = proxy.token
    try:
        return _run_child(binary, ["--profile", profile_name, *args], env)
    finally:
        proxy.stop()
        config_path.unlink(missing_ok=True)


def launch_opencode(
    ctx: Any, model: Optional[str], args: Sequence[str], *, desktop: bool = False
) -> int:
    resolved = resolve_model(ctx, model)
    model_ids = ctx.client.models.ids() or [resolved]
    base_url = openai_base_url(ctx.client.base_url)
    path, applied = opencode_config.merge(base_url, model_ids)
    if applied:
        ctx.out.note(f"Tileward provider written to {path}")
    else:
        block = opencode_config.provider_block(base_url, model_ids)
        ctx.out.warn(
            f"{path} exists but isn't plain JSON (opencode also allows JSONC) -- left untouched. "
            'Add this under "provider" yourself:\n' + json.dumps({"tileward": block}, indent=2)
        )
    env = dict(os.environ)
    if not env.get("TILEWARD_API_KEY") and ctx.client.api_key:
        env["TILEWARD_API_KEY"] = ctx.client.api_key

    if desktop:
        if sys.platform != "darwin":
            raise click.ClickException(
                "OpenCode Desktop is macOS-only. Use `twcli launch opencode` without --desktop "
                "for the terminal version on Linux / WSL."
            )
        proc = subprocess.Popen(["open", "-a", "ai.opencode.desktop"], env=env)
        return proc.wait()

    binary = _binary("opencode")
    return _run_child(binary, args, env)


__all__ = ["launch_claude", "launch_codex", "launch_opencode", "resolve_model"]
