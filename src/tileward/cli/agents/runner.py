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
from typing import Any, Optional, Sequence

import click

from ... import errors
from ...client import Tileward, openai_base_url
from ...config import config_dir
from . import anthropic, codex_config, opencode_config, responses
from .proxy import Proxy


def resolve_model(client: Tileward, requested: Optional[str]) -> str:
    """`--model`, validated against what's actually served; else the account default.

    Never a hardcoded fallback id — the served catalogue is data, not code (see
    `resources/models.py`), so a literal here would eventually 404 quietly.
    """
    if requested:
        client.models.retrieve(requested)  # raises NotFoundError naming what IS served, if wrong
        return requested
    model = client.models.default()
    if not model:
        raise errors.ConfigError(
            "No model given and the served catalogue is empty. Pass --model, or check "
            "`twcli models list`."
        )
    return model


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
        # Ctrl-C already reached the child directly -- both processes share the same foreground
        # process group, so the terminal signals them together. Just wait for its own shutdown
        # rather than sending a second signal that could race it.
        return proc.wait()


def launch_claude(
    ctx: Any, model: Optional[str], fast_model: Optional[str], port: int, args: Sequence[str]
) -> int:
    binary = _binary("claude")
    resolved = resolve_model(ctx.client, model)
    proxy = Proxy(
        client=ctx.client,
        adapter=anthropic,
        model=resolved,
        routes={"/v1/messages": "messages", "/v1/messages/count_tokens": "count_tokens"},
        port=port,
    )
    proxy.start()
    ctx.out.note(f"Tileward proxy on {proxy.base_url} -- model {resolved!r} -> claude")
    env = dict(os.environ)
    env["ANTHROPIC_BASE_URL"] = proxy.base_url
    env["ANTHROPIC_AUTH_TOKEN"] = proxy.token
    # An inherited real Anthropic key would otherwise silently win over the proxy token and send
    # this session straight to api.anthropic.com instead of Tileward.
    env.pop("ANTHROPIC_API_KEY", None)
    env["ANTHROPIC_MODEL"] = resolved
    env["ANTHROPIC_SMALL_FAST_MODEL"] = fast_model or resolved
    # Measured, not assumed: with a real `claude login` session on the machine, Claude Code 2.1.267
    # silently keeps using that Keychain-stored OAuth credential for the actual /v1/messages call
    # even though its own UI reports the auth-token precedence as active (verified against the
    # live proxy -- the OAuth token showed up on the wire; ANTHROPIC_AUTH_TOKEN never did). A
    # dedicated, isolated config directory gives Claude Code nothing to fall back to, which fixed
    # it in that same test. Never the user's real ~/.claude -- this is a twcli-owned directory
    # Claude Code has never seen before the first `launch claude`.
    claude_home = config_dir() / "claude-launch"
    claude_home.mkdir(parents=True, exist_ok=True)
    env["CLAUDE_CONFIG_DIR"] = str(claude_home)
    try:
        return _run_child(binary, args, env)
    finally:
        proxy.stop()


def launch_codex(ctx: Any, model: Optional[str], port: int, args: Sequence[str]) -> int:
    binary = _binary("codex")
    resolved = resolve_model(ctx.client, model)
    proxy = Proxy(
        client=ctx.client,
        adapter=responses,
        model=resolved,
        routes={"/v1/responses": "responses"},
        port=port,
    )
    proxy.start()
    token_env = "TILEWARD_PROXY_TOKEN"
    config_path = codex_config.merge(
        base_url=f"{proxy.base_url}/v1", token_env=token_env, model_id=resolved
    )
    ctx.out.note(
        f"Tileward proxy on {proxy.base_url} -- model {resolved!r} -> codex ({config_path})"
    )
    env = dict(os.environ)
    env[token_env] = proxy.token
    try:
        return _run_child(binary, ["--profile", "tileward", *args], env)
    finally:
        proxy.stop()


def launch_opencode(ctx: Any, model: Optional[str], args: Sequence[str]) -> int:
    binary = _binary("opencode")
    resolved = resolve_model(ctx.client, model)
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
    return _run_child(binary, args, env)


__all__ = ["launch_claude", "launch_codex", "launch_opencode", "resolve_model"]
