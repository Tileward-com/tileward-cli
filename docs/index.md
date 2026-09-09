# tileward

**Run large models on hardware you own, governed.**

One package, two faces. `import tileward` is a typed Python client; `twcli` is the command line
over the same client. Everything one can do, the other can do.

```bash
pip install tileward
```

Python 3.9 or newer. Three dependencies: `httpx`, `click`, `rich`.

## What it covers

| | Library | Command |
| --- | --- | --- |
| [Tileward Models](models.md) — an OpenAI-compatible chat surface | `tw.chat`, `tw.models` | `twcli chat`, `twcli models` |
| [Tileward Governance](governance.md) — allow / deny before the model writes a token | `tw.guard` | `twcli guard` |
| [Tileward Context](context.md) — recall the slice of history a question needs | `tw.context` | `twcli context` |
| [Tileward Documents](documents.md) — answers from your own files | `tw.documents` | `twcli docs` |
| [Account and keys](account.md) | `tw.keys`, `tw.account` | `twcli keys`, `twcli account` |

## Two lines to a first call

```python
from tileward import Tileward

tw = Tileward()                                   # reads TILEWARD_API_KEY
print(tw.chat.say("Say hello in one sentence."))
```

```bash
twcli chat "Say hello in one sentence."
```

## Three things to know before you start

**An API key and a console session are different credentials.** The key (`tw_live_…`) calls the
model, the guard, Context and Documents. Minting keys, reading billing and binding policies need a
console session from `twcli auth login`. A key that could mint keys would survive its own
revocation, so it cannot. See [Keys and sessions](credentials.md).

**There are three hosts and they do not overlap.** `api.tileward.com` serves `/v1` only,
`app.tileward.com` serves the account surface, and Context lives on `context.tileward.com`. The
client routes by which credential a call needs, so you never set this by hand — but a config that
points everything at one host will not work. See [Hosts](hosts.md).

**A governed refusal is not an HTTP error.** It arrives as an ordinary completion with
`finish_reason: "content_filter"` and zero tokens billed. `create()` passes it through; only
`say()` raises `GuardRefusal`. See [Errors](errors.md).

## Where to go next

- [Quickstart](quickstart.md) — from `pip install` to a first answer.
- [Command line](cli.md) — every command, the exit codes, and the JSON contract.
- [Configuration](configuration.md) — environment, files, and profiles.
- [OpenAI-compatible clients](openai.md) — using Tileward from a framework you already have.

## Elsewhere

- [tileward.com](https://tileward.com) — the product.
- [tileward.com/llms.txt](https://tileward.com/llms.txt) — what we ship and how each number was
  measured. It is the canonical statement; these pages do not restate its figures.
- [app.tileward.com/account](https://app.tileward.com/account) — dashboard, keys, billing.
- [Issues](https://github.com/Tileward-com/tileward-cli/issues) — bugs and requests.
