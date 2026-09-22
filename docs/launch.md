# Coding agent CLIs

`twcli launch` opens Claude Code, Codex, or opencode with Tileward as their model backend.

```bash
twcli launch claude
twcli launch codex
twcli launch opencode
```

Each picks the account's configured default served model unless you pass `--model`:

```bash
twcli launch claude --model Tileward-Qwen3.8-27b
```

With no `--model` and no configured default, a single served model is used with no fuss. More
than one, and there's no way to guess which one you meant, so you're asked from a table of what's
served:

```console
$ twcli launch claude
                            Available models
#  id                        context  $/M in  $/M out  compression ratio
1  Tileward-Qwen3.6-35B-A3B  262,144  0.12    0.99     2.8
2  gpt-oss-20b               8,192    0.12    0.28     1
3  Tileward-Qwen3.8-27b      262,144  0.12    0.96     1.79
Pick a model [1-3]:
```

Answer once, or set a default so future launches skip the question:

```bash
twcli config set model Tileward-Qwen3.8-27b
```

Running from a script or CI with no default configured fails with a clear error instead of
prompting into a pipe that will never answer.

Extra arguments go straight to the underlying tool — put them after `--` if they could be mistaken
for one of `twcli`'s own flags:

```bash
twcli launch claude -- -p "explain this repo"
twcli launch codex -- exec "explain this repo"
twcli launch opencode -- run "explain this repo"
```

## Why this isn't just an environment variable

opencode already speaks Tileward's real `/v1/chat/completions` — `launch opencode` just writes a
`provider.tileward` block into opencode's own config and runs it. No proxy involved.

Claude Code and Codex don't. Claude Code only speaks the Anthropic Messages API; Codex dropped
support for chat completions entirely in Codex 0.122 and speaks only the OpenAI Responses API.
Tileward's own `/v1/messages` is a governed passthrough to a customer's *own* Anthropic key, by
deliberate decision, and there is no `/v1/responses` at all — neither reaches Tileward's own
served models. So `launch claude` and `launch codex` each start a small local HTTP server for the
run's lifetime, bound to `127.0.0.1` only, that translates the tool's own protocol into a Tileward
chat completion and back, then tear it down when the tool exits. The real `tw_live_…` key never
leaves `twcli`'s own process — the child only ever sees a random per-run token.

| | Talks to Tileward | How |
| --- | --- | --- |
| `launch opencode` | directly | opencode's own OpenAI-compatible provider |
| `launch claude` | through a local proxy | `ANTHROPIC_BASE_URL` points at `127.0.0.1` |
| `launch codex` | through a local proxy | a `tileward` provider + profile in `config.toml` |

## claude

Sets `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, and
`ANTHROPIC_SMALL_FAST_MODEL` (defaults to the same model; override with `--fast-model`) on the
child process only. An inherited `ANTHROPIC_API_KEY` is unset for that process, so a real Anthropic
key sitting in your shell can't silently take over and send the session to `api.anthropic.com`
instead.

```bash
twcli launch claude --model Tileward-Qwen3.6-35B-A3B --fast-model gpt-oss-20b
```

**It also points `CLAUDE_CONFIG_DIR` at `<config dir>/claude-launch`, never your real
`~/.claude`.** If Claude Code has an active `claude login` session on the machine, it keeps using
that Keychain-stored credential for the actual model call even with `ANTHROPIC_AUTH_TOKEN` set —
confirmed against a real proxy (the login's OAuth token showed up on the wire; the auth token never
did), a mismatch from Claude Code's own [documented
precedence](https://code.claude.com/docs/en/authentication#authentication-precedence). A separate,
empty config directory gives it nothing to fall back to. This is why the first `launch claude` on a
machine goes through tool-permission and trust prompts again — they're being asked once for that
isolated directory, not for your regular `claude` setup, and later launches reuse it.

**It declares the model's context window, less Claude Code's output reserve.** Claude Code can't
know a Tileward model's context size — for a model id it doesn't recognize it assumes 200k — and
it compacts the conversation a roughly fixed ~24.8k tokens below whatever window it has. Every
request also reserves Claude Code's output budget (32,000 tokens unless
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` says otherwise) out of the same served window, so declaring the
full served size isn't safe: at 262,144, compaction fires near 237.3k, while the server rejects any
prompt over 230,144 alongside a 32,000-token reserve. So `launch claude` sets
`CLAUDE_CODE_MAX_CONTEXT_TOKENS` to the served context less that reserve — 230,144 for a
262,144-token model — and compaction fires near 205.3k instead. Measured against a stub server
standing in for the model, not taken from docs. A `CLAUDE_CODE_MAX_CONTEXT_TOKENS` you set yourself
is left alone, and a model whose context can't hold the output reserve gets a warning and no value.

**It logs each compaction.** Claude Code runs hooks around a compaction: `PreCompact` before it,
`PostCompact` with the summary, and `SessionStart` (source `compact`) on the next turn. `launch
claude` passes one handler for all three as `--settings <json>`. It appends a line of metadata per
event — trigger, session id, transcript path, summary size, never the summary itself — to
`<config dir>/claude-launch/compactions.jsonl`, prints nothing, and always exits 0, so the session
behaves exactly as it would without it. Claude Code keeps only the *last* `--settings` it is given
rather than merging them, so if you pass your own after `--`, yours is used and the hooks are left
out for that run.

```bash
twcli launch claude --no-compaction-hooks
```

## codex

Codex reads its provider from `config.toml`, not the environment, so each launch writes a profile
named `tileward-<pid>` — `codex --profile <name>` layers `$CODEX_HOME/<name>.config.toml` on top
of the base config — carrying a `model_providers` table and the `model_provider`/`model`
selection, then runs `codex --profile tileward-<pid>`. Your real `config.toml` is never touched at
all: the file is written fresh for this process and deleted when it exits. It's keyed to the pid
rather than a fixed name so two `launch codex` sessions running at once can't race on the same
file and cross-wire which proxy each one talks to.

```bash
twcli launch codex --model Tileward-Qwen3.8-27b
```

## opencode

Merges a `provider.tileward` block into `~/.config/opencode/opencode.json` (or `$OPENCODE_CONFIG`,
or the existing `opencode.jsonc` if you have one). If that file isn't plain JSON — opencode also
accepts JSONC, comments and all, which this can't safely rewrite — nothing is written and the
block is printed for you to add by hand instead.

```bash
twcli launch opencode --model Tileward-Qwen3.6-35B-A3B
```

## What you give up going through claude or codex

Tool calling on Tileward's own models is measured, not assumed — see
[Tileward Models](models.md) for where that stands per model. A tool call the model itself
returns malformed JSON for degrades to an empty `{}` argument rather than crashing the proxy or the
session; that's the model's own reliability showing through the translation, not a bug in the
translation itself.

Governance still runs on every call — the proxy is a shape change in front of Tileward's API, not
a bypass of it. A refusal comes back as a normal turn (Claude Code sees a `refusal` stop reason;
Codex sees a completed response) rather than an HTTP error, matching how each tool's own provider
would report one.

A system-role message is only ever sent first, whatever position it arrived in — Claude Code's
`mid-conversation-system` beta can revise instructions partway through a long session, and
Tileward's backend 400s on a system message that isn't the first entry, so every one found is
folded into a single leading message instead. Found running a real agentic session through the
proxy, not a synthetic test case.
