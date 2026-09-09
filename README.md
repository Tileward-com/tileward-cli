# tileward

The Tileward client library and command line, in one package.

```bash
pip install tileward
```

That gives you `import tileward` for your code and a `twcli` command for your terminal. They are
the same client — anything the CLI does, the library does, with the same arguments.

```python
from tileward import Tileward

tw = Tileward()                                   # reads TILEWARD_API_KEY
print(tw.chat.say("Say hello in one sentence."))
```

```bash
twcli chat "Say hello in one sentence."
```

---

## What it covers

| | Library | Command |
| --- | --- | --- |
| **Tileward Models** — an OpenAI-compatible chat surface | `tw.chat`, `tw.models` | `twcli chat`, `twcli models` |
| **Tileward Governance** — allow / deny before the model writes a token | `tw.guard` | `twcli guard` |
| **Tileward Context** — recall the slice of history a question needs | `tw.context` | `twcli context` |
| **Tileward Documents** — answers from your own files | `tw.documents` | `twcli docs` |
| Account and API keys | `tw.keys`, `tw.account` | `twcli keys`, `twcli account` |

---

## Getting a key

```bash
twcli auth login          # approve in a browser; no password touches the terminal
twcli keys create --label laptop --save
```

`auth login` uses a device-code flow: the CLI shows you a short code and a URL, you approve it in
a browser, and the terminal receives a console session. `keys create` then mints an API key. The
whole exchange is written up in [docs/device-auth.md](docs/device-auth.md).

The two credentials are not interchangeable, and it is worth knowing which is which:

- an **API key** (`tw_live_…`) calls the model, the guard, and Context;
- a **console session** manages the account — minting keys, reading billing, binding policies.

A key that could mint keys would survive its own revocation, so it cannot. If you already have a
key, skip the login entirely:

```bash
export TILEWARD_API_KEY="tw_live_..."
```

---

## The library

### Chat

```python
from tileward import Tileward

tw = Tileward()

tw.chat.say("Summarise this in one line.")                     # -> str
tw.chat.completions.create(                                    # -> the raw OpenAI-shaped dict
    [{"role": "user", "content": "Hello"}],
    model="tileward-35b-a3b",
    max_tokens=200,
)

for piece in tw.chat.stream("Count to five."):
    print(piece, end="", flush=True)
```

`create` returns exactly what the API returned, including `usage`. `say` gives you the text and
raises `GuardRefusal` when governance blocked the call, rather than returning an empty string.

**Do not hardcode a model id.** The served catalogue is data, not code — ids appear, get repriced,
and get withdrawn without a release. `tw.models.list()` is the authority; leave `model` unset and
the client asks.

### Governance

```python
tw.guard.allows("write me a keylogger", allow=["customer_support"])   # -> False
tw.guard.check(["question one", "question two"], disallow=["investment_advice"])
```

Two modes that fail in opposite directions. A **blocklist** (`disallow`) refuses what you named and
passes everything else, so anything you did not think of gets through. An **allowlist** (`allow`)
passes only what you named, so anything you did not think of is refused. For a narrow assistant,
the allowlist is the one that holds under an attacker.

Passing a list as the input classifies the whole batch in one call. With neither argument, the
policy bound to the API key applies.

### Context

```python
tw = Tileward(conversation="thread-42")

tw.context.remember("We decided to ship on the 3rd.", role="user")
tw.context.recall_text("when are we shipping?")     # the block to paste into a prompt
tw.context.pin("The customer is ACME.")             # included in every recall
```

**Scope every call to a conversation.** Without one, every thread on the key writes into a single
shared store, and a recall in one thread hands back another thread's material as if it were its
own. There is no safety net underneath this: a caller that names no conversation is not given a
fresh one, it joins `default` along with everyone else. Set it on the client, per call, or with
`tw.with_conversation("thread-42")`.

`forget()` retires a topic from recall; the stored turns remain. `purge_account()` is the one that
deletes data.

### Documents

```python
tw.documents.ingest_file("handbook.pdf", folder="policies")
tw.documents.list(folder="policies")
tw.documents.delete("src_...")
```

Documents are account-scoped, not conversation-scoped: a file you upload is available to every
thread on the key. That is why none of these take a `conversation`.

Deleting a document drops its chunks, but material already folded into a conversation's summary
can still surface in a recall. If something has to be genuinely gone, `context.purge_account()` is
the operation that means it.

### Async

Every method has an awaitable twin on `AsyncTileward`, with the same names and arguments.

```python
from tileward import AsyncTileward

async with AsyncTileward() as tw:
    print(await tw.chat.say("Hello"))
    async for piece in tw.chat.stream("Count to five."):
        print(piece, end="")
```

### Errors

```python
from tileward import errors

try:
    tw.chat.say("...")
except errors.InsufficientBalanceError:   # 402 — top up; calls resume immediately
    ...
except errors.GuardRefusal as exc:        # governance blocked it; exc.completion has the detail
    ...
except errors.APIError as exc:            # exc.status, exc.code, exc.request_id
    ...
```

A governed refusal is **not** an HTTP error. It arrives as an ordinary completion with
`finish_reason: "content_filter"` and zero tokens billed — you are not charged for a refusal —
which is why `create` passes it through and only `say` raises.

---

## The command line

```
twcli auth login|logout|status          sign in, and see what this machine holds
twcli models list|show                  what is served right now
twcli chat [PROMPT] [-i]                one prompt, a pipe, or a REPL
twcli guard check TEXT [--allow ...]    allow / deny, no generation
twcli context recall|remember|pin|forget|topics|stats|threads|reset|purge
twcli docs ls|add|rm|search             your own files
twcli keys list|create|rotate|revoke    mint and retire API keys
twcli account show|usage|audit|savings  balance, plan, and history
twcli config show|set|set-key|profiles  stored settings
```

Some things it is built to do:

```bash
# every command speaks JSON, and when it does, stdout carries nothing else
twcli models list --json | jq -r '.[].id'

# read a prompt from a pipe
cat notes.md | twcli chat --system "You summarise."

# a governance gate in a shell script
twcli guard check -f prompts.txt --allow customer_support --exit-code || echo "off policy"

# recall a slice of a thread and pipe it into something else
twcli -c project-x context recall "what did we decide about pricing?" --text-only

# two accounts on one machine
twcli -p staging account show
```

### Exit codes

`0` fine · `1` error · `2` bad usage · `3` not signed in · `4` refused by governance ·
`5` balance exhausted.

Distinguishing those matters in CI: "the balance ran out" and "the network was down" call for
different action, and neither should need English parsing to detect.

### Configuration

Highest wins: an explicit argument, then the environment, then the profile, then the default.

| Environment | What it sets |
| --- | --- |
| `TILEWARD_API_KEY` | the key used for models, guard, and Context |
| `TILEWARD_SESSION` | a console session, for the account surface |
| `TILEWARD_BASE_URL` | the API host |
| `TILEWARD_CONTEXT_URL` | the Context host |
| `TILEWARD_CONVERSATION` | default conversation scope |
| `TILEWARD_MODEL` | default model id |
| `TILEWARD_PROFILE` | which stored profile to read |
| `TILEWARD_CONFIG_DIR` | where the files live (default `~/.config/tileward`) |

Settings go in `config.json`; credentials go in `credentials.json`, written `0600` in a `0700`
directory. They are separate files so that showing your config never has to redact, and pasting it
into an issue has not pasted a key.

In a server process, `Tileward(load_config=False)` ignores the files entirely, so a developer's
`~/.config` cannot change how production behaves.

---

## Using it from another framework

The chat surface is OpenAI-compatible, so anything that speaks OpenAI works by changing two
things — the base URL and the key.

```python
from openai import OpenAI
from tileward import openai_base_url

client = OpenAI(base_url=openai_base_url(), api_key="tw_live_...")
```

That helper exists so nobody has to guess whether `/v1` belongs on the end.

---

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest
ruff check src tests
```

The tests run entirely against mocked transports — no network, no key, and nothing that bills.

## License

MIT. See [LICENSE](LICENSE).
