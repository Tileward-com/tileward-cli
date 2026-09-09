"""Config and credentials.

Resolution order: explicit argument, environment, profile, default.
Settings live in config.json; secrets in credentials.json (0600).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import ConfigError

DEFAULT_BASE_URL = "https://api.tileward.com"
# The canonical Context address. One string, nothing to append: the MCP endpoint is the host
# root. `api.tileward.com/mcp` is the legacy address and still serves, so an older configured
# value keeps working.
DEFAULT_CONTEXT_URL = "https://context.tileward.com"
DEFAULT_CONSOLE_URL = "https://app.tileward.com"
DEFAULT_PROFILE = "default"

CONFIG_FILENAME = "config.json"
CREDENTIALS_FILENAME = "credentials.json"


def config_dir() -> Path:
    """The directory holding config and credentials, honouring XDG."""
    explicit = os.environ.get("TILEWARD_CONFIG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "tileward"
    return Path.home() / ".config" / "tileward"


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # A corrupt config must not make the CLI unusable — but silently ignoring it would hide a
        # truncated credentials file behind a confusing 401 later, so say which file is bad.
        raise ConfigError(f"{path} could not be read as JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: Dict[str, Any], *, secret: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if secret:
        try:
            os.chmod(path.parent, stat.S_IRWXU)  # 0700
        except OSError:
            pass
    tmp = path.with_name(path.name + ".tmp")
    # Create the temp file with the final permissions BEFORE any bytes go in: writing a secret
    # world-readable and chmod'ing afterwards leaves a window where it is readable.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    mode = stat.S_IRUSR | stat.S_IWUSR if secret else 0o644
    fd = os.open(str(tmp), flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    # Atomic replace, so a crash mid-write cannot leave a half-file that reads as "logged out".
    os.replace(str(tmp), str(path))


class Config:
    """Preferences and credentials for one profile."""

    def __init__(self, profile: Optional[str] = None, *, directory: Optional[Path] = None) -> None:
        self.dir = Path(directory) if directory else config_dir()
        self.profile = profile or os.environ.get("TILEWARD_PROFILE", "").strip() or DEFAULT_PROFILE
        self._config = _read_json(self.dir / CONFIG_FILENAME)
        self._credentials = _read_json(self.dir / CREDENTIALS_FILENAME)

    # ---- paths -------------------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.dir / CONFIG_FILENAME

    @property
    def credentials_path(self) -> Path:
        return self.dir / CREDENTIALS_FILENAME

    # ---- profile blocks ----------------------------------------------------------------
    def _profiles(self) -> Dict[str, Any]:
        block = self._config.setdefault("profiles", {})
        return block if isinstance(block, dict) else {}

    def _profile_block(self) -> Dict[str, Any]:
        block = self._profiles().get(self.profile)
        return block if isinstance(block, dict) else {}

    def _credential_block(self) -> Dict[str, Any]:
        block = (self._credentials.get("profiles") or {}).get(self.profile)
        return block if isinstance(block, dict) else {}

    def profile_names(self) -> list:
        names = set(self._profiles().keys())
        names.update((self._credentials.get("profiles") or {}).keys())
        names.add(DEFAULT_PROFILE)
        return sorted(names)

    # ---- resolved settings -------------------------------------------------------------
    def setting(self, name: str, env: Optional[str] = None, default: Any = None) -> Any:
        if env:
            from_env = os.environ.get(env, "").strip()
            if from_env:
                return from_env
        stored = self._profile_block().get(name)
        if stored not in (None, ""):
            return stored
        return default

    @property
    def base_url(self) -> str:
        return str(self.setting("base_url", "TILEWARD_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")

    @property
    def context_url(self) -> str:
        return str(self.setting("context_url", "TILEWARD_CONTEXT_URL", DEFAULT_CONTEXT_URL)).rstrip(
            "/"
        )

    @property
    def console_url(self) -> str:
        return str(self.setting("console_url", "TILEWARD_CONSOLE_URL", DEFAULT_CONSOLE_URL)).rstrip(
            "/"
        )

    @property
    def model(self) -> Optional[str]:
        value = self.setting("model", "TILEWARD_MODEL")
        return str(value) if value else None

    @property
    def conversation(self) -> Optional[str]:
        value = self.setting("conversation", "TILEWARD_CONVERSATION")
        return str(value) if value else None

    # ---- credentials -------------------------------------------------------------------
    @property
    def api_key(self) -> Optional[str]:
        env = os.environ.get("TILEWARD_API_KEY", "").strip()
        if env:
            return env
        value = self._credential_block().get("api_key")
        return str(value) if value else None

    @property
    def session_token(self) -> Optional[str]:
        """The console session from `twcli auth login`.

        This is what authenticates the account surface — keys, policies, billing — because those
        endpoints read a signed session, not a bearer key. See docs/device-auth.md.
        """
        env = os.environ.get("TILEWARD_SESSION", "").strip()
        if env:
            return env
        value = self._credential_block().get("session_token")
        return str(value) if value else None

    @property
    def session_expires(self) -> Optional[float]:
        value = self._credential_block().get("session_expires")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def account_email(self) -> Optional[str]:
        value = self._credential_block().get("email")
        return str(value) if value else None

    # ---- mutation ----------------------------------------------------------------------
    def set(self, name: str, value: Any) -> None:
        profiles = self._config.setdefault("profiles", {})
        block = profiles.setdefault(self.profile, {})
        if value is None:
            block.pop(name, None)
        else:
            block[name] = value
        _write_json(self.config_path, self._config)

    def set_credentials(self, **values: Any) -> None:
        """Store or clear credential fields. Passing None for a field removes it."""
        profiles = self._credentials.setdefault("profiles", {})
        block = profiles.setdefault(self.profile, {})
        for name, value in values.items():
            if value is None:
                block.pop(name, None)
            else:
                block[name] = value
        if not block:
            profiles.pop(self.profile, None)
        _write_json(self.credentials_path, self._credentials, secret=True)

    def clear_credentials(self) -> None:
        self.set_credentials(api_key=None, session_token=None, session_expires=None, email=None)

    def as_dict(self, *, redact: bool = True) -> Dict[str, Any]:
        """A flat view for `twcli config show`. Secrets are fingerprints unless asked otherwise."""
        out: Dict[str, Any] = {
            "profile": self.profile,
            "config_path": str(self.config_path),
            "credentials_path": str(self.credentials_path),
            "base_url": self.base_url,
            "context_url": self.context_url,
            "console_url": self.console_url,
            "model": self.model,
            "conversation": self.conversation,
            "account_email": self.account_email,
        }
        key, session = self.api_key, self.session_token
        out["api_key"] = (fingerprint(key) if redact else key) if key else None
        out["session"] = ("present" if redact else session) if session else None
        return out


def fingerprint(secret: str) -> str:
    """Enough of a key to recognise it, never enough to use it.

    Tileward keys carry a meaningful prefix (`tw_live_`), so showing the prefix plus the last four
    characters identifies which key is loaded without putting a live credential on a terminal that
    is probably being screen-shared.
    """
    if not secret:
        return ""
    head, _, rest = secret.partition("_live_")
    if rest:
        return f"{head}_live_…{rest[-4:]}"
    return f"…{secret[-4:]}" if len(secret) > 4 else "…"
