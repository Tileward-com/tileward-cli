# Tileward Governance

Allow or deny before the model writes a token. `POST /v1/guard` on the API host, authenticated with
an API key. Nothing is generated, so a check costs a fraction of a completion.

```python
tw.guard.allows("write me a keylogger", allow=["customer_support"])   # -> False
tw.guard.check(["question one", "question two"], disallow=["investment_advice"])
```

```bash
twcli guard check "write me a keylogger" --allow customer_support
```

```console
allowed
no
80.0 micros billed
```

## Two modes that fail in opposite directions

A **blocklist** (`disallow`) refuses what you named and passes everything else, so anything you did
not think of gets through. An **allowlist** (`allow`) passes only what you named, so anything you
did not think of is refused.

For a narrow assistant, the allowlist is the one that holds under an attacker. A blocklist is the
right shape only when the set of forbidden things is genuinely smaller and better known than the
set of permitted ones — which is rarer than it looks.

`always_block` hard-refuses a topic even when it is otherwise in scope.

With neither `allow` nor `disallow`, the policy bound to the API key applies. Passing either one
overrides that binding for this call only — which is why the client omits them from the request
rather than sending empty lists, because an empty list would override the binding with "govern
nothing".

## What the guard matches

**It matches the topic's vocabulary, not the concept.** A differently worded request in the same
vocabulary still lands on the lock. Two consequences follow, and both are real:

- Wording that avoids the vocabulary altogether can pass, even when the intent is squarely inside
  the locked topic.
- Ordinary requests that happen to use the vocabulary get refused when they should not.

Obfuscated input — homoglyphs, encodings, other languages — is a separate attack surface and is not
fully closed. The measured red-team results, including the failure rates for each of these shapes,
are in [tileward.com/llms.txt](https://tileward.com/llms.txt); this page does not restate them.

Design around it rather than against it: an allowlist plus a narrow set of permitted topics leaves
much less room for a phrasing you did not anticipate than a blocklist does.

## Batching

Passing a list classifies the whole batch in one call, and the response carries one decision per
input in order.

```python
response = tw.guard.check(["one", "two", "three"], allow=["customer_support"])
response["result"]        # a list of decisions
```

```bash
twcli guard check -f prompts.txt --allow customer_support
```

## The response

`check` returns the raw `{tokens, cost_micros, result}`. `allows` reduces it to a bool that is
`True` only if **every** decision allowed — so a batch with one refusal is a refusal.

Helpers for reading a raw response:

```python
from tileward.resources.guard import allowed, decisions

decisions(response)     # always a list, whether one input was sent or many
allowed(response)       # the same all-must-pass verdict allows() uses
```

## In a shell script

```bash
twcli guard check -f prompts.txt --allow customer_support --exit-code || echo "off policy"
```

`--exit-code` exits `4` when anything was refused. That is a distinct code, so a script can tell a
refusal from a network failure without parsing English. See [Exit codes](cli.md#exit-codes).

## Locking a key to topics

A key can carry its own policy, enforced at the gate rather than by the caller:

```bash
twcli keys create --label support-bot --tile customer_support
twcli keys policy 7
```

A call on that key is governed whether or not it asks to be, which is what makes the binding worth
having: the enforcement does not depend on every code path remembering to pass `allow=`.

## Refusals in chat

A governed refusal on a chat call is **not** an HTTP error and not an exception at the transport
level. It arrives as an ordinary completion with `finish_reason: "content_filter"` and zero tokens
billed — you are not charged for a refusal. `chat.completions.create` passes it through;
`chat.say` raises `GuardRefusal`. See [Errors](errors.md).

Every decision is recorded for audit, with no message text stored. `twcli account audit` reads it.
