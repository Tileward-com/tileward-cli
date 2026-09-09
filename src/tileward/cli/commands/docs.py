"""`twcli docs` — your own files, and what Tileward can answer from them.

DOCUMENTS ARE ACCOUNT-SCOPED. Unlike everything under `twcli context`, a document is visible to
every conversation on the key, so these commands ignore `-c/--conversation` rather than pretending
to honour it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click

from ..main import Ctx, common, pass_ctx
from ..output import rows_from, truncate

COLUMNS = ["source_id", "title", "folder", "type", "chunks", "tags"]


@click.group("docs")
def docs_group() -> None:
    """Upload, list, and remove documents."""


@docs_group.command("ls")
@click.option("--folder", help="Only this folder.")
@click.option("--tag", "tags", multiple=True, help="Only documents carrying this tag. Repeatable.")
@click.option("--query", "-q", help="Filter the listing by title, folder, or tag.")
@click.option("--limit", type=int, help="Cap the number of rows.")
@click.option("--sort", help="Field to sort on (default: folder).")
@click.option("--order", type=click.Choice(["asc", "desc"]), help="Sort direction.")
@common()
@pass_ctx
def ls(
    ctx: Ctx,
    folder: Optional[str],
    tags,
    query: Optional[str],
    limit: Optional[int],
    sort: Optional[str],
    order: Optional[str],
) -> None:
    """List documents on the account."""
    rows = ctx.client.documents.list(
        folder=folder,
        tags=list(tags) or None,
        query=query,
        limit=limit,
        sort=sort,
        order=order,
    )
    ctx.emit(rows)
    ctx.out.table(
        rows_from(rows),
        COLUMNS,
        headers={"source_id": "id"},
        empty="No documents. Add one with `twcli docs add <file>`.",
    )


@docs_group.command("add")
@click.argument("paths", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--folder", help="File the document under this folder.")
@click.option("--tag", "tags", multiple=True, help="Tag the document. Repeatable.")
@click.option("--title", help="Title when reading text from stdin.")
@common()
@pass_ctx
def add(ctx: Ctx, paths, folder: Optional[str], tags, title: Optional[str]) -> None:
    """Ingest one or more files, or text piped in.

    \b
      twcli docs add handbook.pdf notes.md --folder policies
      cat report.txt | twcli docs add --title "Q3 report"
    """
    tag_list = list(tags) or None

    if not paths:
        if sys.stdin.isatty():
            raise click.UsageError("Name a file, or pipe text in with --title.")
        text = sys.stdin.read()
        if not text.strip():
            raise click.UsageError("Nothing arrived on stdin.")
        if not title:
            raise click.UsageError("Text from stdin needs a --title.")
        result = ctx.client.documents.ingest_text(title, text, folder=folder, tags=tag_list)
        ctx.emit(result)
        ctx.out.ok(f"Ingested {title!r} ({len(text):,} characters).")
        return

    results = []
    for path in paths:
        # One file at a time, and a failure on one does not abandon the rest — a directory with a
        # single unreadable file should still upload the other nine.
        try:
            result = ctx.client.documents.ingest_file(path, folder=folder, tags=tag_list)
            results.append({"path": str(path), "ok": True, "result": result})
            ctx.out.ok(f"{path.name} → {result.get('source_id') or 'ingested'}")
        except Exception as exc:  # noqa: BLE001 - reported per file, summarised at the end
            results.append({"path": str(path), "ok": False, "error": str(exc)})
            ctx.out.error(f"{path.name}: {exc}")
    ctx.emit(results)
    failed = [r for r in results if not r["ok"]]
    if failed:
        raise SystemExit(1)


@docs_group.command("rm")
@click.argument("source_ids", nargs=-1, required=True)
@click.option("--yes", is_flag=True, help="Skip the confirmation.")
@common()
@pass_ctx
def rm(ctx: Ctx, source_ids, yes: bool) -> None:
    """Remove documents by source id.

    Removing a document drops its chunks. Material already folded into a conversation's summary
    can still surface in a recall — `twcli context purge` is what makes something unrecoverable.
    """
    if not yes:
        click.confirm(f"Remove {len(source_ids)} document(s)?", abort=True)
    results = []
    for source_id in source_ids:
        result = ctx.client.documents.delete(source_id)
        results.append({"source_id": source_id, "result": result})
        ctx.out.ok(f"Removed {source_id}.")
    ctx.emit(results)
    ctx.out.note("Deleting a document leaves the summary it contributed to. See `context purge`.")


@docs_group.command("search")
@click.argument("query")
@click.option("--limit", type=int, default=10, show_default=True)
@click.option(
    "--content",
    is_flag=True,
    help="Search the text rather than the listing, by recalling against the ingested chunks.",
)
@common()
@pass_ctx
def search(ctx: Ctx, query: str, limit: int, content: bool) -> None:
    """Find documents.

    By default this filters the LISTING — titles, folders, tags. `--content` recalls against the
    ingested text instead, which is what answers a question rather than finding a filename.
    """
    if content:
        bundle = ctx.client.context.recall(query, scope="all", max_items=limit)
        ctx.emit(bundle)
        if not ctx.out.as_json:
            from ...resources.context import text_of

            ctx.out.raw(text_of(bundle) + "\n")
        return
    rows = ctx.client.documents.search(query, limit=limit)
    ctx.emit(rows)
    ctx.out.table(
        rows_from(rows),
        COLUMNS,
        headers={"source_id": "id"},
        empty=f"No document matched {truncate(query, 40)!r}. Try --content to search the text.",
    )


def register(cli: click.Group) -> None:
    cli.add_command(docs_group)
