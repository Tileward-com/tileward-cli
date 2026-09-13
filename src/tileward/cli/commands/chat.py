"""`twcli chat` — talk to a model, once or interactively.

WITH NO PROMPT, A TERMINAL GETS A CONVERSATION. `twcli chat` on its own starts a REPL that keeps
the transcript; a prompt argument, a pipe, or `-` still gets one answer.

STREAMING IS THE DEFAULT for an interactive terminal and off when stdout is a pipe: a consumer
reading the output wants the whole answer, and interleaving it with progress would corrupt it.

AN ANSWER GETS WHATEVER THE WINDOW LEAVES. A request that names no `max_tokens` gets the
gateway's short default cap, which stops a long answer mid-sentence. So unless `--max-tokens` is
given, each request asks for the model's context length less the prompt. Nothing here tokenizes:
until a turn has come back the prompt is bounded by its bytes, and after that it is measured by
the turn's `usage`. When the window refuses the ask anyway, because the gateway added context of
its own, the ask is halved and sent again.
"""

from __future__ import annotations

import itertools
import secrets
import sys
from collections.abc import Callable, Iterator
from typing import Any, Dict, List, Optional, Tuple, TypeVar

import click

from ... import errors
from ..._mcp import conversation_headers
from ...resources.chat import delta_of, normalize_messages, refusal_of, text_of
from ...resources.models import summarize
from ..main import EXIT_REFUSED, Ctx, common, pass_ctx

Messages = List[Dict[str, Any]]
T = TypeVar("T")

# Room for the chat template's role markers, on top of the messages themselves.
TEMPLATE_ALLOWANCE = 256
# How many times a refused ask is halved before the refusal stands. The refusal cannot size the
# retry: its "at least N input tokens" is one more than the room the ask left, whatever the
# prompt's real length.
HALVINGS = 4


def _interactive_terminal() -> bool:
    return sys.stdin.isatty()


def _read_prompt(prompt: Optional[str]) -> str:
    """The prompt from an argument, or from stdin when it is piped or named as `-`."""
    if prompt and prompt != "-":
        return prompt
    if prompt == "-" or not _interactive_terminal():
        text = sys.stdin.read().strip()
        if text:
            return text
        raise click.UsageError("Nothing arrived on stdin to send.")
    raise click.UsageError("Give a prompt, or pipe one in.")


@click.command("chat")
@click.argument("prompt", required=False)
@click.option("--system", "-s", help="System prompt.")
@click.option(
    "--max-tokens",
    type=int,
    help="Cap the answer's length. Unset, it may use whatever the model's window leaves.",
)
@click.option("--temperature", "-t", type=float, help="Sampling temperature.")
@click.option("--top-p", type=float, help="Nucleus sampling cutoff.")
@click.option("--stream/--no-stream", default=None, help="Stream tokens as they arrive.")
@click.option(
    "--interactive",
    "-i",
    is_flag=True,
    help="Hold a conversation. The default with no prompt when stdin is a terminal.",
)
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
    """Hold a conversation, or send one prompt and print the answer.

    \b
      twcli chat
      twcli chat "Summarise this repo's release process."
      cat notes.md | twcli chat --system "You summarise."
      twcli chat -c project-x --remember
    """
    if interactive or (prompt is None and _interactive_terminal()):
        if ctx.out.as_json:
            raise click.UsageError("A conversation has no JSON form; give a prompt or pipe one in.")
        first = prompt if prompt != "-" else None
        _repl(ctx, first, system, max_tokens, temperature, top_p, remember)
        return

    text = _read_prompt(prompt)
    should_stream = stream if stream is not None else (sys.stdout.isatty() and not ctx.out.as_json)
    messages = normalize_messages(text, system)
    if remember:
        recalled = ctx.client.context.recall_text(text)
        if recalled:
            messages = _with_recalled(messages, recalled)
    # A given cap leaves nothing to size against the window, so the catalogue is not read for it.
    model, window = (ctx.client.default_model, None) if max_tokens else _model_and_window(ctx)
    options = _options(model, max_tokens, temperature, top_p)

    if should_stream:
        pieces: List[str] = []
        finish: Optional[str] = None
        spent: Any = None
        for chunk in _stream(ctx, messages, options, window, _prompt_ceiling(messages)):
            piece = delta_of(chunk)
            if piece:
                pieces.append(piece)
                ctx.out.raw(piece)
            finish = _finish_of(chunk) or finish
            spent = chunk.get("usage") or spent
        ctx.out.raw("\n")
        if finish == "content_filter":
            raise SystemExit(EXIT_REFUSED)
        if finish == "length":
            ctx.out.note(_stopped(max_tokens, window))
        if usage and isinstance(spent, dict):
            ctx.out.pairs(spent, title="usage")
        answer = "".join(pieces)
        if remember and answer:
            ctx.client.context.remember(text, role="user")
            ctx.client.context.remember(answer, role="assistant")
        return

    completion = _complete(ctx, messages, options, window, _prompt_ceiling(messages))
    refusal = refusal_of(completion)
    ctx.emit(completion)
    if refusal:
        ctx.out.error(refusal)
        raise SystemExit(EXIT_REFUSED)
    answer = text_of(completion)
    if not ctx.out.as_json:
        ctx.out.raw(answer + "\n")
    if _finish_of(completion) == "length":
        ctx.out.note(_stopped(max_tokens, window))
    if usage and not ctx.out.as_json:
        ctx.out.pairs(completion.get("usage") or {}, title="usage")
    if remember and answer:
        ctx.client.context.remember(text, role="user")
        ctx.client.context.remember(answer, role="assistant")


def _repl(
    ctx: Ctx,
    first: Optional[str],
    system: Optional[str],
    max_tokens: Optional[int],
    temperature: Optional[float],
    top_p: Optional[float],
    remember: bool,
) -> None:
    out = ctx.out
    if _interactive_terminal():
        try:
            import readline  # noqa: F401 -- line editing and history for input()
        except ImportError:  # pragma: no cover - not every platform ships it
            pass
    model, window = _model_and_window(ctx)
    if max_tokens:
        window = None
        reach = f"up to {max_tokens:,} tokens"
    elif window:
        reach = f"the rest of the {window:,}-token window"
    else:
        reach = "the server's default length"
    options = _options(model, max_tokens, temperature, top_p)
    # A Context thread of its own, named here rather than left to the gateway, so this session's
    # history is never mixed with another session's.
    options["headers"] = conversation_headers(f"twcli-{secrets.token_hex(5)}")
    out.print(
        f"[dim]Talking to {model or 'the default model'}; each answer gets {reach}. "
        "/reset clears the transcript, Ctrl-D or /exit leaves.[/dim]"
    )
    if remember and not ctx.client.conversation:
        # Without a conversation every thread on the key writes into one shared store, and a later
        # recall in an unrelated session gets this transcript back. Say so once, up front.
        out.warn("--remember with no -c: this transcript joins the key's shared default store.")

    transcript: Messages = [{"role": "system", "content": system}] if system else []
    # Tokens the transcript held after the last answer, as `usage` reported them. Measured rather
    # than bounded, so it stands in for the byte count of everything said before the next question.
    held: Optional[int] = None
    pending = first
    while True:
        if pending:
            line, pending = pending, None
            out.raw(f"› {line}\n")
        else:
            try:
                line = input("› ")
            except EOFError:
                out.raw("\n")
                return
            except KeyboardInterrupt:
                out.raw("\n")
                out.note("Ctrl-D or /exit leaves.")
                continue
        line = line.strip()
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return
        if line == "/reset":
            transcript = [m for m in transcript if m.get("role") == "system"]
            held = None
            out.note("Transcript cleared.")
            continue

        turn: Dict[str, Any] = {"role": "user", "content": line}
        request: Messages = [*transcript, turn]
        pieces: List[str] = []
        finish: Optional[str] = None
        spent: Any = None
        try:
            if remember:
                recalled = ctx.client.context.recall_text(line)
                if recalled:
                    request = _with_recalled(request, recalled)
            if held is None or remember:
                size = _prompt_ceiling(request)
            else:
                size = held + _prompt_ceiling([turn])
            for chunk in _stream(ctx, request, options, window, size):
                piece = delta_of(chunk)
                if piece:
                    pieces.append(piece)
                    out.raw(piece)
                finish = _finish_of(chunk) or finish
                spent = chunk.get("usage") or spent
        except KeyboardInterrupt:
            out.raw("\n")
            out.note("Stopped.")
            if pieces:
                # What was shown stays in the transcript, so the next question can refer to it.
                transcript += [turn, {"role": "assistant", "content": "".join(pieces)}]
                held = None
            continue
        except (errors.AuthenticationError, errors.InsufficientBalanceError):
            # Nothing typed next could succeed. main() says what to do about it, and exits.
            raise
        except errors.TilewardError as exc:
            if pieces:
                out.raw("\n")
            out.error(str(exc))
            continue
        out.raw("\n\n")
        if finish == "content_filter":
            out.warn("Refused by governance, so that turn is not kept.")
            continue
        answer = "".join(pieces)
        transcript += [turn, {"role": "assistant", "content": answer}]
        held = _tokens_of(spent)
        if finish == "length":
            out.note(_stopped(max_tokens, window))
        if remember:
            try:
                ctx.client.context.remember(line, role="user")
                if answer:
                    ctx.client.context.remember(answer, role="assistant")
            except errors.TilewardError as exc:
                out.warn(f"That turn was not written to Context: {exc}")


def _model_and_window(ctx: Ctx) -> Tuple[Optional[str], Optional[int]]:
    """The model a request will use, and its context length when the catalogue states one."""
    rows = ctx.client.models.list()
    model = ctx.client.default_model or next((str(r["id"]) for r in rows if r.get("id")), None)
    for row in rows:
        if row.get("id") == model:
            window = summarize(row).get("context_len")
            if isinstance(window, int) and window > 0:
                return model, window
    return model, None


def _options(
    model: Optional[str],
    max_tokens: Optional[int],
    temperature: Optional[float],
    top_p: Optional[float],
) -> Dict[str, Any]:
    given: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    return {name: value for name, value in given.items() if value is not None}


def _prompt_ceiling(messages: Messages) -> int:
    """An upper bound on these messages' tokens: a byte-level token spans at least one byte."""
    size = sum(len(str(message.get("content") or "").encode("utf-8")) + 8 for message in messages)
    return size + TEMPLATE_ALLOWANCE


def _first_ask(window: Optional[int], prompt: int) -> Optional[int]:
    """What the window leaves after the prompt, and never under a quarter of the window.

    A byte count can overstate a prompt several times over, which would give a small window a
    needlessly small ask. When a quarter really is too much, the window refuses it and it halves.
    """
    if window is None:
        return None
    return max(window - prompt, window // 4)


def _sized(send: Callable[[Optional[int]], T], window: Optional[int], prompt: int) -> T:
    """`send(ask)` for what the window leaves, halving the ask each time it is refused."""
    first = _first_ask(window, prompt)
    asks: List[Optional[int]] = (
        [None] if first is None else [max(first >> n, 1) for n in range(HALVINGS + 1)]
    )
    for ask in asks[:-1]:
        try:
            return send(ask)
        except errors.APIError as exc:
            if exc.code != "context_length_exceeded":
                raise
    return send(asks[-1])


def _with_ask(options: Dict[str, Any], ask: Optional[int]) -> Dict[str, Any]:
    return options if ask is None else {**options, "max_tokens": ask}


def _complete(
    ctx: Ctx, messages: Messages, options: Dict[str, Any], window: Optional[int], prompt: int
) -> Dict[str, Any]:
    def send(ask: Optional[int]) -> Dict[str, Any]:
        return ctx.client.chat.completions.create(messages, stream=False, **_with_ask(options, ask))

    return _sized(send, window, prompt)


def _stream(
    ctx: Ctx, messages: Messages, options: Dict[str, Any], window: Optional[int], prompt: int
) -> Iterator[Dict[str, Any]]:
    """The chunks of a streamed answer, with the request already sent.

    Sent before returning, so that a refused ask, which arrives as the response status ahead of
    any text, can be halved and sent again while nothing has been printed.
    """

    def send(ask: Optional[int]) -> Iterator[Dict[str, Any]]:
        sized = _with_ask(options, ask)
        return _opened(ctx.client.chat.completions.create(messages, stream=True, **sized))

    return _sized(send, window, prompt)


def _opened(chunks: Iterator[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    first = next(chunks, None)
    head: List[Dict[str, Any]] = [] if first is None else [first]
    return itertools.chain(head, chunks)


def _finish_of(payload: Dict[str, Any]) -> Optional[str]:
    """`finish_reason` from a completion or a stream chunk, when one is set."""
    for choice in payload.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason"):
            return str(choice["finish_reason"])
    return None


def _tokens_of(usage: Any) -> Optional[int]:
    """What the transcript holds once an answer joins it: that request's prompt plus its answer."""
    if not isinstance(usage, dict):
        return None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if isinstance(prompt, int) and isinstance(completion, int):
        return prompt + completion
    return None


def _with_recalled(messages: Messages, recalled: str) -> Messages:
    """Recalled context, added to the system prompt rather than to the user's words.

    The model should be able to tell what the person asked from what the store supplied. It joins
    the FIRST system message because some chat templates refuse a system message anywhere else.
    """
    note = f"Relevant context from earlier:\n{recalled}"
    if messages and messages[0].get("role") == "system":
        head = messages[0]
        return [{**head, "content": f"{head.get('content')}\n\n{note}"}, *messages[1:]]
    return [{"role": "system", "content": note}, *messages]


def _stopped(max_tokens: Optional[int], window: Optional[int]) -> str:
    if max_tokens:
        return f"Stopped at --max-tokens {max_tokens:,}."
    if window:
        return f"Stopped at the end of the model's {window:,}-token window."
    return "Stopped at the server's default length. --max-tokens asks for more."


def register(cli: click.Group) -> None:
    cli.add_command(chat)
