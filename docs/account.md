# Account and keys

Balance, plan, usage, audit, and the keys themselves. All of it lives on `app.tileward.com` and
authenticates with a **console session**, not an API key. Run `twcli auth login` first — see
[Keys and sessions](credentials.md).

## Reading the account

```bash
twcli account show       # balance, plan, entitlements
twcli account usage      # day-by-day tokens
twcli account audit      # request history
twcli account savings    # what Context has saved on this account
```

```python
tw.account.get()
tw.account.billing()
tw.account.audit(limit=50)
tw.account.context_stats()
tw.account.context_savings()
```

**The rates in `account show` are the authoritative ones.** They are set server-side and can
change, so anything that copies a number out of here into code will eventually be quoting a price
that is no longer charged. Read them at the time you need them.

The audit trail is metadata only — time, model, decision, key, tokens, request id. It never holds
prompt or completion text.

## Keys

```bash
twcli keys list
twcli keys list --all              # include revoked
twcli keys create --label ci
twcli keys create --label support --tile customer_support --save
twcli keys rotate 7 --save
twcli keys revoke 7
twcli keys policy 7
```

```python
tw.keys.list()
tw.keys.create("ci", cells=["customer_support"])
tw.keys.rotate(7)
tw.keys.revoke(7)
tw.keys.policy(7)
tw.keys.metrics()
```

The secret comes back in the `key` field of `create` and `rotate`, and that is the only time it
exists outside the API. Keys are stored as hashes; they cannot be recovered, only replaced.

**Rotate, do not revoke-and-recreate.** Rotation keeps the key's id, and with it its bound policy
and its whole usage and audit history. The old secret stops working immediately.

`twcli keys revoke` notes when the profile is still holding a key afterwards — otherwise the next
command fails with a 401 that looks like a different problem entirely.

## Running out of credit

When the balance reaches zero the API returns **HTTP 402** rather than accruing a debt. The
account, its keys and its stored context are left intact; top up and calls resume immediately.

In the library that is `errors.InsufficientBalanceError`. In the CLI it is exit code `5`, distinct
from a network failure on purpose. See [Errors](errors.md) and [Exit codes](cli.md#exit-codes).

Plans, allowances and overage are at [tileward.com/pricing](https://tileward.com/pricing/).
