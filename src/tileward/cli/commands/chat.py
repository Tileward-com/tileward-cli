"""`twcli chat` — talk to a model, once or interactively.

STREAMING IS THE DEFAULT for an interactive terminal and off when stdout is a pipe: a consumer
reading the output wants the whole answer, and interleaving it with progress would corrupt it.
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional

import click

from ...errors import GuardRefusal
from ...resources.chat import refusal_of, text_of
from ..main import EXIT_REFUSED, Ctx, common, pass_ctx


def _read_prompt(prompt: Optional[str]) -> str:
    """The prompt from an argument, or from stdin when it is piped or named as `-`."""
    if prompt and prompt != "-":
        return prompt
    if prompt == "-" or not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise click.UsageError("Give a prompt, pipe one in, or use `twcli chat --interactive`.")


@click.command("chat")
@click.argument("prompt", required=False)
@click.option("--system", "-s", help="System prompt.")
@click.option("--max-tokens", type=int, help="Cap the answer's length.")
@click.option("--temperature", "-t", type=float, help="Sampling temperature.")
@click.option("--top-p", type=float, help="Nucleus sampling cutoff.")
@click.option("--stream/--no-stream", default=None, help="Stream tokens as they arrive.")
@click.option("--interactive", "-i", is_flag=True, help="Start a REPL that keeps the transcript.")
@click.option(
    "--remember/--no-remember",
    default=False,
    help="Also write each turn into Tileward Context, and recall before answering.",
)
@click.option("--usage", is_flag=True, help="Print the token usage after the answer.")
@common(conversation=True, model=True)
@pass_ctx
def chat(
    ctx: Ctx,
    prompt: Optional[str],
    system: Optional[str],
    max_tokens: Optional[int],
    temperature: Optional[float],
    top_p: Optional[float],
    stream: Optional[bool],
    interactive: bool,
    remember: bool,
    usage: bool,
) -> None:
    """Send a prompt and print the answer.

    \b
      twcli chat "Summarise this repo's release process."
      cat notes.md | twcli chat --system "You summarise."
      twcli chat -i -c project-x --remember
    """
    if interactive:
        _repl(ctx, system, max_tokens, temperature, top_p, remember)
        return

    text = _read_prompt(prompt)
    should_stream = stream if stream is not None else (sys.stdout.isatty() and not ctx.out.as_json)

    # The system prompt is built as its own string rather than shuffled through the kwargs dict:
    # mixing it in with the numeric options makes the dict heterogeneous, and the concatenation
    # below then reads as "maybe a float plus a string" to anything checking types — including a
    # reader.
    prompt_system = system
    if remember:
        recalled = ctx.client.context.recall_text(text)
        if recalled:
            # Prepended as context, not merged into the user's words: the model should be able to
            # tell what the person asked from what the store supplied.
            prefix = f"{prompt_system}\n\n" if prompt_system else ""
            prompt_system = f"{prefix}Relevant context from earlier:\n{recalled}"

    options: Dict[str, Any] = {
        "system": prompt_system,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    kwargs = {k: v for k, v in options.items() if v is not None}

    if should_stream:
        pieces: List[str] = []
        for piece in ctx.client.chat.stream(text, **kwargs):
            pieces.append(piece)
            ctx.out.raw(piece)
        ctx.out.raw("\n")
        answer = "".join(pieces)
        if remember and answer:
            ctx.client.context.remember(text, role="user")
            ctx.client.context.remember(answer, role="assistant")
        return

    completion = ctx.client.chat.completions.create(text, stream=False, **kwargs)
    refusal = refusal_of(completion)
    ctx.emit(completion)
    if refusal:
        ctx.out.error(refusal)
        raise SystemExit(EXIT_REFUSED)
    answer = text_of(completion)
    if not ctx.out.as_json:
        ctx.out.raw(answer + "\n")
    if usage and not ctx.out.as_json:
        ctx.out.pairs(completion.get("usage") or {}, title="usage")
    if remember and answer:
        ctx.client.context.remember(text, role="user")
        ctx.client.context.remember(answer, role="assistant")


def _repl(
    ctx: Ctx,
    system: Optional[str],
    max_tokens: Optional[int],
    temperature: Optional[float],
    top_p: Optional[float],
    remember: bool,
) -> None:
    out = ctx.out
    model = ctx.client.models.default() or "the default model"
    out.print(f"[dim]Talking to {model}. Ctrl-D or /exit to leave.[/dim]")
    if remember and not ctx.client.conversation:
        # Without a conversation every thread on the key writes into one shared store, and a later
        # recall in an unrelated session gets this transcript back. Say so once, up front.
        out.warn("--remember with no -c: this transcript joins the key's shared default store.")
    transcript: List[dict] = []
    if system:
        transcript.append({"role": "system", "content": system})
    while True:
        try:
            line = click.prompt("›", prompt_suffix=" ", show_default=False)
        except (click.exceptions.Abort, EOFError):
            out.print()
            return
        line = line.strip()
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return
        if line == "/reset":
            transcript = [t for t in transcript if t.get("role") == "system"]
            out.note("Transcript cleared.")
            continue
        transcript.append({"role": "user", "content": line})
        kwargs = {"max_tokens": max_tokens, "temperature": temperature, "top_p": top_p}
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        pieces: List[str] = []
        try:
            for piece in ctx.client.chat.stream(transcript, **kwargs):
                pieces.append(piece)
                out.raw(piece)
        except GuardRefusal as exc:
            out.error(str(exc))
            transcript.pop()
            continue
        out.raw("\n\n")
        answer = "".join(pieces)
        transcript.append({"role": "assistant", "content": answer})
        if remember:
            ctx.client.context.remember(line, role="user")
            if answer:
                ctx.client.context.remember(answer, role="assistant")


def register(cli: click.Group) -> None:
    cli.add_command(chat)
