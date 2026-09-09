"""`twcli auth` — sign in, sign out, and see who you are.

WHAT LOGIN GETS YOU, AND WHAT IT DOES NOT. Signing in stores a console session, which is what the
account surface needs: minting keys, reading billing, binding policies. It is NOT an API key, and
having one does not let you call the model. The two are separate on purpose, and the natural first
session is `twcli auth login` followed by `twcli keys create`.
"""

from __future__ import annotations

import socket
import time
import webbrowser
from typing import Optional

import click

from ... import auth as device_auth
from ...config import fingerprint
from ..main import Ctx, common, pass_ctx


def _client_name() -> str:
    """What the approval screen will say is asking.

    The hostname is the useful half — someone approving a code needs to recognise the machine, and
    "twcli" alone describes every terminal they have ever used.
    """
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    return f"twcli on {host}" if host else "twcli"


@click.group("auth")
def auth_group() -> None:
    """Sign in to the console session that manages keys and billing."""


@auth_group.command("login")
@click.option("--no-browser", is_flag=True, help="Print the URL instead of opening a browser.")
@click.option("--client-name", help="Label shown on the approval screen.")
@common()
@pass_ctx
def login(ctx: Ctx, no_browser: bool, client_name: Optional[str]) -> None:
    """Sign in from this terminal, approving in a browser."""
    out = ctx.out
    client = ctx.client
    transport = client._transport

    authorization = device_auth.start(transport, client_name=client_name or _client_name())

    if ctx.out.as_json:
        # Emitted BEFORE the wait, not after: a script driving this needs the code while the
        # code is still useful.
        out.json(
            {
                "user_code": authorization.user_code,
                "verification_uri": authorization.verification_uri,
                "verification_uri_complete": authorization.verification_uri_complete,
                "expires_in": authorization.expires_in,
            }
        )
    else:
        out.print()
        out.print(f"  Code:  [bold]{authorization.user_code}[/bold]")
        out.print(f"  Open:  [cyan]{authorization.url}[/cyan]")
        out.print()

    opened = False
    if not no_browser and not ctx.out.as_json:
        try:
            opened = webbrowser.open(authorization.url)
        except Exception:
            opened = False
    if not opened and not ctx.out.as_json:
        out.note("Open that link on any device and approve the code.")

    deadline_note = {"last": 0.0}

    def tick(remaining: float) -> None:
        # One line a minute, not one a poll: a spinner that redraws every five seconds is worse
        # than silence when the output is going to a log.
        if time.time() - deadline_note["last"] > 60:
            deadline_note["last"] = time.time()
            out.note(f"Waiting for approval… {int(remaining)}s left.")

    session = device_auth.poll(transport, authorization, on_tick=tick)

    ctx.config.set_credentials(
        session_token=session.access_token,
        session_expires=session.expires_at,
        email=session.email,
    )
    if ctx.out.as_json:
        return
    who = session.email or "your account"
    out.ok(f"Signed in as {who} (profile: {ctx.config.profile}).")
    if not ctx.config.api_key:
        out.print()
        out.print("  Next: [bold]twcli keys create --label laptop[/bold] to mint an API key.")


@auth_group.command("logout")
@click.option("--all", "all_credentials", is_flag=True, help="Also forget the stored API key.")
@common()
@pass_ctx
def logout(ctx: Ctx, all_credentials: bool) -> None:
    """Forget the stored session on this machine.

    This deletes the local copy. It does not end the session server-side — to cut off a session
    you no longer control, sign out everywhere from the console.
    """
    config = ctx.config
    if all_credentials:
        config.clear_credentials()
        ctx.out.ok("Session and API key removed from this machine.")
    else:
        config.set_credentials(session_token=None, session_expires=None, email=None)
        ctx.out.ok("Session removed from this machine.")
    ctx.emit({"ok": True})


@auth_group.command("status")
@common()
@pass_ctx
def status(ctx: Ctx) -> None:
    """What credentials this profile is holding, and whether they still work."""
    config = ctx.config
    client = ctx.client
    payload = {
        "profile": config.profile,
        "base_url": client.base_url,
        "context_url": client.context_url,
        "api_key": fingerprint(config.api_key) if config.api_key else None,
        "session": bool(config.session_token),
        "email": config.account_email,
        "session_expires": config.session_expires,
    }

    # A stored token is not the same as a working one — it expires, and it can be invalidated by a
    # sign-out elsewhere. Ask the server rather than reporting the file's contents as truth.
    if config.session_token:
        try:
            account = client.account.get()
            payload["session_valid"] = True
            payload["email"] = account.get("email") or payload["email"]
            payload["balance_usd"] = account.get("balance_usd")
            payload["plan"] = account.get("effective_plan") or account.get("plan")
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            payload["session_valid"] = False
            payload["session_error"] = str(exc)

    ctx.emit(payload)
    out = ctx.out
    out.pairs(
        {
            "Profile": payload["profile"],
            "API host": payload["base_url"],
            "Context host": payload["context_url"],
            "API key": payload["api_key"] or "not set",
            "Session": (
                "valid"
                if payload.get("session_valid")
                else "stored, not valid"
                if payload["session"]
                else "not signed in"
            ),
            "Account": payload.get("email") or "—",
            "Plan": payload.get("plan"),
            "Balance (USD)": payload.get("balance_usd"),
        }
    )
    if payload["session"] and payload.get("session_valid") is False:
        out.warn(f"The stored session did not work: {payload.get('session_error')}")
        out.warn("Run `twcli auth login` again.")


def register(cli: click.Group) -> None:
    cli.add_command(auth_group)
