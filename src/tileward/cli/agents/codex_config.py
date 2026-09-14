"""Write a dedicated Codex profile file that points at this run's local proxy.

CONFIRMED against a real `codex` install (0.154.0) after the version this was first built against
turned out to have a broken native binary in that environment: `codex --profile <name>` (`-p`)
"Layer[s] $CODEX_HOME/<name>.config.toml on top of the base user config" -- a SEPARATE file, not a
`[profiles.<name>]` table inside `config.toml` as an earlier ambiguous source suggested. That means
this never has to touch the user's real `config.toml` at all: `[model_providers.<name>]` and the
`model_provider`/`model` selection all live together in one file this fully owns.

No TOML library needed: the whole file is generated from scratch every launch (it carries this
run's ephemeral proxy port, so it wouldn't be idempotent to preserve anyway), never parsed.

THE PROFILE NAME IS PER-PROCESS, NOT A FIXED "tileward". Two `twcli launch codex` sessions running
at once would otherwise share one `tileward.config.toml`: whichever session's `merge()` writes
last decides the port BOTH sessions' `codex` processes read at startup, silently cross-wiring one
session's traffic through the other's proxy. Keying the filename (and the `model_providers` table
name it must match) to this process's pid gives each launch its own file; `runner.py` deletes it
in `finally` once the child exits, so `~/.codex/` doesn't accumulate one of these per run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Tuple

_HEADER = "# managed by `twcli launch codex` -- deleted when this session exits\n"


def codex_home() -> Path:
    explicit = os.environ.get("CODEX_HOME", "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".codex"


def profile_config_path(profile_name: str) -> Path:
    return codex_home() / f"{profile_name}.config.toml"


def merge(*, base_url: str, token_env: str, model_id: str) -> Tuple[Path, str]:
    """Write this launch's profile file. Returns `(path, profile_name)`; `runner.py` passes
    `--profile <profile_name>` to activate it and removes `path` when the child exits. The real
    Tileward key never appears here -- `env_key` only *names* an environment variable
    (`token_env`), set on the child process alone.
    """
    profile_name = f"tileward-{os.getpid()}"
    path = profile_config_path(profile_name)
    text = (
        f"{_HEADER}"
        f'model_provider = "{profile_name}"\n'
        f'model = "{model_id}"\n'
        f"\n"
        f"[model_providers.{profile_name}]\n"
        f'name = "Tileward (local proxy)"\n'
        f'base_url = "{base_url}"\n'
        f'env_key = "{token_env}"\n'
        f'wire_api = "responses"\n'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, profile_name
