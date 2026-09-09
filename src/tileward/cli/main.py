"""`twcli` — the command line over the same client the library exposes.

Exit codes: 0 fine, 1 error, 2 bad usage, 3 not signed in, 4 refused by governance,
5 balance exhausted. The client is built lazily so `--help` works with no credentials.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import click

from .. import errors
from .._version import __version__
from ..client import Tileward
from ..config import Config
from .output import Out

EXIT_ERROR = 1
EXIT_AUTH = 3
EXIT_REFUSED = 4
EXIT_BALANCE = 5


class Ctx:
    """Global options, plus the lazily-built client every command shares."""

    def __init__(
        self,
        *,
        profile: Optional[str],
        api_key: Optional[str],
        base_url: Optional[str],
        context_url: Optional[str],
        conversation: Optional[str],
        model: Optional[str],
        as_json: bool,
        quiet: bool,
        color: bool,
        timeout: Optional[float],
    ) -> None:
        self.profile = profile
        self.api_key = api_key
        self.base_url = base_url
        self.context_url = context_url
        self.conversation = conversation
        self.model = model
        self.out = Out(as_json=as_json, quiet=quiet, color=color)
        self.timeout = timeout
        self._config: Optional[Config] = None
        self._client: Optional[Tileward] = None

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = Config(self.profile)
        return self._config

    @property
    def client(self) -> Tileward:
        if self._client is None:
            kwargs: dict = {
                "profile": self.profile,
                "api_key": self.api_key,
                "base_url": self.base_url,
                "context_url": self.context_url,
                "conversation": self.conversation,
                "model": self.model,
                "user_agent_suffix": "twcli",
            }
            if self.timeout:
                kwargs["timeout"] = self.timeout
            self._client = Tileward(**kwargs)
        return self._client

    def emit(self, payload: Any) -> None:
        """Print JSON if asked. Commands call this and then their human rendering."""
        if self.out.as_json:
            self.out.json(payload)

    def require_session(self) -> None:
        if not self.client.has_session:
            raise click.ClickException(
                "This needs a signed-in session. Run `twcli auth login` first.\n"
                "(Key management and billing authenticate on a console session, not an API key.)"
            )


pass_ctx = click.make_pass_decorator(Ctx)


def _override(click_ctx: click.Context, param: click.Parameter, value: Any) -> Any:
    """Let a global flag be written after the subcommand as well as before it.

    Click binds an option to the level it is declared on, so `twcli --json models list` would work
    and `twcli models list --json` would be a usage error. Everyone types the second one. These
    callbacks re-declare the shared flags on each command and fold them back into the one `Ctx`,
    so both spellings mean the same thing.
    """
    if value in (None, False):
        return value
    ctx = click_ctx.find_object(Ctx)
    if ctx is None:
        return value
    if param.name == "as_json":
        ctx.out.as_json = True
    elif param.name == "quiet":
        ctx.out.quiet = True
    elif param.name == "conversation":
        ctx.conversation = value
        ctx._client = None  # rebuilt on next use, with the new scope
    elif param.name == "model":
        ctx.model = value
        ctx._client = None
    return value


def common(*, conversation: bool = False, model: bool = False):
    """Add the shared flags to a command. `expose_value=False` keeps them out of its signature."""

    def wrap(func):
        func = click.option(
            "--json",
            "as_json",
            is_flag=True,
            expose_value=False,
            callback=_override,
            help="Emit JSON on stdout and nothing else.",
        )(func)
        func = click.option(
            "--quiet",
            is_flag=True,
            expose_value=False,
            callback=_override,
            help="Suppress progress notes on stderr.",
        )(func)
        if model:
            func = click.option(
                "--model",
                "-m",
                expose_value=False,
                callback=_override,
                help="Model id to use.",
            )(func)
        if conversation:
            func = click.option(
                "--conversation",
                "-c",
                expose_value=False,
                callback=_override,
                help="Conversation to scope this call to.",
            )(func)
        return func

    return wrap


@click.group(context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100})
@click.version_option(__version__, "-V", "--version", prog_name="twcli")
@click.option("--profile", "-p", envvar="TILEWARD_PROFILE", help="Named profile to read and write.")
@click.option("--api-key", envvar="TILEWARD_API_KEY", help="Override the stored API key.")
@click.option("--base-url", envvar="TILEWARD_BASE_URL", help="Override the API host.")
@click.option("--context-url", envvar="TILEWARD_CONTEXT_URL", help="Override the Context host.")
@click.option(
    "--conversation",
    "-c",
    envvar="TILEWARD_CONVERSATION",
    help="Conversation to scope Context calls to. Without one, every thread on the key shares "
    "a single store.",
)
@click.option("--model", "-m", envvar="TILEWARD_MODEL", help="Model id to use by default.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON on stdout and nothing else.")
@click.option("--quiet", "-q", is_flag=True, help="Suppress progress notes on stderr.")
@click.option("--no-color", is_flag=True, help="Disable colour.")
@click.option("--timeout", type=float, help="Per-request timeout in seconds.")
@click.pass_context
def cli(
    ctx: click.Context,
    profile: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    context_url: Optional[str],
    conversation: Optional[str],
    model: Optional[str],
    as_json: bool,
    quiet: bool,
    no_color: bool,
    timeout: Optional[float],
) -> None:
    """Tileward from the command line: models, context, documents, and keys.

    Start with `twcli auth login`, then `twcli keys create --label laptop`.
    """
    ctx.obj = Ctx(
        profile=profile,
        api_key=api_key,
        base_url=base_url,
        context_url=context_url,
        conversation=conversation,
        model=model,
        as_json=as_json,
        quiet=quiet,
        color=not no_color,
        timeout=timeout,
    )


def _register() -> None:
    from .commands import account, auth, chat, config, context, docs, guard, keys, models

    for module in (auth, models, chat, guard, context, docs, keys, account, config):
        module.register(cli)


_register()


def main(argv: Optional[list] = None) -> int:
    """Entry point. Turns the library's exceptions into exit codes and one-line messages."""
    out = Out()
    try:
        # standalone_mode=False so click's SystemExit does not bypass the handlers below; click
        # still prints usage errors itself, which is why UsageError is re-raised untouched.
        cli.main(args=argv if argv is not None else sys.argv[1:], standalone_mode=False)
    except click.exceptions.Abort:
        out.error("Cancelled.")
        return EXIT_ERROR
    except click.exceptions.UsageError as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.ClickException as exc:
        exc.show()
        return EXIT_ERROR
    except errors.GuardRefusal as exc:
        out.error(str(exc))
        return EXIT_REFUSED
    except errors.InsufficientBalanceError as exc:
        out.error(str(exc))
        out.warn("Top up at https://app.tileward.com/account — calls resume immediately.")
        return EXIT_BALANCE
    except errors.AuthenticationError as exc:
        out.error(str(exc))
        out.warn("Run `twcli auth login`, or check TILEWARD_API_KEY.")
        return EXIT_AUTH
    except errors.ConfigError as exc:
        out.error(str(exc))
        return EXIT_AUTH
    except errors.TilewardError as exc:
        out.error(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        out.error("Interrupted.")
        return 130
    except BrokenPipeError:
        # `twcli models list | head` closes the pipe under us. That is not a failure, and a
        # traceback about it is noise in every shell pipeline anyone writes.
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
