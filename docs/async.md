# Async

Every method has an awaitable twin on `AsyncTileward`, with the same names and the same arguments.

```python
from tileward import AsyncTileward

async with AsyncTileward() as tw:
    print(await tw.chat.say("Hello"))

    async for piece in tw.chat.stream("Count to five."):
        print(piece, end="", flush=True)
```

Outside a context manager, close it yourself:

```python
tw = AsyncTileward()
try:
    ...
finally:
    await tw.aclose()
```

## The whole surface

```python
await tw.models.list()
await tw.models.default()

await tw.chat.say("...")
await tw.chat.completions.create("...", max_tokens=200)

await tw.guard.allows("...", allow=["customer_support"])

await tw.context.remember("...")
await tw.context.recall_text("...")

await tw.documents.ingest_file("handbook.pdf")
await tw.documents.list()

await tw.keys.list()
await tw.account.get()
```

`with_conversation()` returns an `AsyncTileward` view sharing the parent's connections:

```python
thread = tw.with_conversation("thread-42")
await thread.context.recall_text("...")
```

## Streaming

`stream()` is an async generator — iterate it, do not await it:

```python
async for piece in tw.chat.stream("..."):
    ...
```

`completions.create(stream=True)` needs the extra await, because `create` itself is a coroutine
that returns the stream:

```python
stream = await tw.chat.completions.create("...", stream=True)
async for chunk in stream:
    ...
```

Awaiting the generator instead of iterating it would buffer the whole answer before you saw a
token.

## Concurrency

One client is safe to share across tasks and pools its connections. Building one per request
throws that away.

```python
import asyncio

async with AsyncTileward() as tw:
    answers = await asyncio.gather(*(tw.chat.say(q) for q in questions))
```

To bring your own pool limits or proxy, pass an `httpx.AsyncClient`:

```python
AsyncTileward(http_client=my_async_client)
```

The client will not close a connection it did not open.
