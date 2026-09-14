"""Write a dedicated Codex profile file that points at this run's local proxy.

CONFIRMED against a real `codex` install (0.154.0) after the version this was first built against
turned out to have a broken native binary in that environment: `codex --profile <name>` (`-p`)
"Layer[s] $CODEX_HOME/<name>.config.toml on top of the base user config" -- a SEPARATE file, not a
`[profiles.<name>]` table inside `config.toml` as an earlier ambiguous source suggested. That means
this never has to touch the user's real `config.toml` at all: `[model_providers.tileward]` and the
`model_provider`/`model` selection all live together in one file this fully owns.

No TOML library needed: the whole file is generated from scratch every launch (it carries this
run's ephemeral proxy port, so it wouldn't be idempotent to preserve anyway), never parsed.
"""

from __future__ import annotations

import os
from pathlib import Path

_PROFILE_NAME = "tileward"
_HEADER = "# managed by `twcli launch codex` -- regenerated on every launch, safe to delete\n"


def codex_home() -> Path:
    explicit = os.environ.get("CODEX_HOME", "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".codex"


def profile_config_path() -> Path:
    return codex_home() / f"{_PROFILE_NAME}.config.toml"


def merge(*, base_url: str, token_env: str, model_id: str) -> Path:
    """Write the `tileward` profile file. Returns its path; `runner.py` passes `--profile
    tileward` to activate it. The real Tileward key never appears here -- `env_key` only *names*
    an environment variable (`token_env`), set on the child process alone.
    """
    path = profile_config_path()
    text = (
        f"{_HEADER}"
        f'model_provider = "{_PROFILE_NAME}"\n'
        f'model = "{model_id}"\n'
        f"\n"
        f"[model_providers.{_PROFILE_NAME}]\n"
        f'name = "Tileward (local proxy)"\n'
        f'base_url = "{base_url}"\n'
        f'env_key = "{token_env}"\n'
        f'wire_api = "responses"\n'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
