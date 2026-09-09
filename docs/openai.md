# OpenAI-compatible clients

The chat surface is OpenAI-compatible, so anything that speaks OpenAI works against Tileward by
changing two things: the base URL and the key.

```python
from openai import OpenAI
from tileward import openai_base_url

client = OpenAI(base_url=openai_base_url(), api_key="tw_live_...")

client.chat.completions.create(
    model="tileward-35b-a3b",
    messages=[{"role": "user", "content": "Hello"}],
)
```

`openai_base_url()` exists so nobody has to guess whether `/v1` belongs on the end. It does — the
value is `https://api.tileward.com/v1`. For a self-hosted deployment, pass your API host:

```python
openai_base_url("https://api.internal.example")
```

## Anything else that takes a base URL

The same two changes work for the frameworks and gateways that wrap an OpenAI client. Set the base
URL to `https://api.tileward.com/v1` and the key to a `tw_live_…` key.

```bash
export OPENAI_BASE_URL="https://api.tileward.com/v1"
export OPENAI_API_KEY="tw_live_..."
```

Ask for the model id rather than assuming one — `twcli models list --json | jq -r '.[0].id'`. See
[Tileward Models](models.md).

Copy-paste recipes for specific chat clients, editors, agent frameworks and gateways are at
[tileward.com/docs](https://tileward.com/docs/).

## What you give up

An OpenAI client reaches Tileward Models only. Governance, Context and Documents are not part of
the OpenAI surface, so they stay on this package's client or on the MCP endpoint:

| | Through an OpenAI client | How to reach it |
| --- | --- | --- |
| Tileward Models | yes | `/v1/chat/completions` |
| Tileward Governance | no | `tw.guard`, or `POST /v1/guard` |
| Tileward Context | no | `tw.context`, or MCP at `context.tileward.com` |
| Tileward Documents | no | `tw.documents`, or MCP at `context.tileward.com` |

A key with a bound policy is still governed on a call made through an OpenAI client — the
enforcement is at the gate, not in the SDK. A refusal comes back as a completion with
`finish_reason: "content_filter"`, which most OpenAI clients surface as an empty message rather
than an error. Check the finish reason. See [Tileward Governance](governance.md).

## Mixing the two

Nothing stops you using both: an OpenAI client for generation, this one for everything around it.

```python
from openai import OpenAI
from tileward import Tileward, openai_base_url

key = "tw_live_..."
tw = Tileward(api_key=key, conversation="thread-42")
oai = OpenAI(base_url=openai_base_url(), api_key=key)

question = "when are we shipping?"
if not tw.guard.allows(question, allow=["customer_support"]):
    raise SystemExit("off policy")

recalled = tw.context.recall_text(question)
oai.chat.completions.create(
    model=tw.models.default(),
    messages=[
        {"role": "system", "content": f"Relevant context from earlier:\n{recalled}"},
        {"role": "user", "content": question},
    ],
)
```
