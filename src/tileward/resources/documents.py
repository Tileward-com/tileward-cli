"""Tileward Documents — answers from your own files.

Account-scoped, not conversation-scoped, so nothing here takes a `conversation`.
Delete drops a document's chunks but not what it contributed to a summary;
`context.purge_account()` is the one that removes data.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from ..errors import TilewardError
from .context import documents_of

INGEST_DOCUMENT = "tileward_ingest_document"
INGEST_FILE = "tileward_ingest_file"
LIST_DOCUMENTS = "tileward_list_documents"
DELETE_DOCUMENT = "tileward_delete_document"

# Files are sent base64 in a JSON-RPC body, which costs a third in size on the wire and is held in
# memory whole on both ends. The cap is this client's, chosen so a mistaken `twcli docs add *`
# against a directory of videos fails immediately with a readable message instead of after a long
# upload.
MAX_FILE_BYTES = 32 * 1024 * 1024


def list_args(
    *,
    folder: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    query: Optional[str] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> Dict[str, Any]:
    args: Dict[str, Any] = {}
    if folder:
        args["folder"] = folder
    if tags:
        args["tags"] = list(tags)
    if query:
        args["query"] = query
    if sort:
        args["sort"] = sort
    if order:
        args["order"] = order
    if limit is not None:
        args["limit"] = int(limit)
    if offset is not None:
        args["offset"] = int(offset)
    return args


def read_file(path: Union[str, os.PathLike[str]]) -> Dict[str, str]:
    """Read a file into the `{filename, content_b64}` an ingest call wants."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise TilewardError(f"{p} is not a file.")
    size = p.stat().st_size
    if size > MAX_FILE_BYTES:
        raise TilewardError(
            f"{p.name} is {size / 1_048_576:.1f} MB; the ingest limit in this client is "
            f"{MAX_FILE_BYTES // 1_048_576} MB. Split it, or ingest its text with "
            f"`documents.ingest_text()`."
        )
    return {
        "filename": p.name,
        "content_b64": base64.b64encode(p.read_bytes()).decode("ascii"),
    }


class Documents:
    def __init__(self, client: Any) -> None:
        self._client = client

    def _call(self, tool: str, args: Dict[str, Any]) -> Any:
        return self._client._context_transport.call(tool, args, conversation=None)

    def list(self, **kwargs: Any) -> List[Dict[str, Any]]:
        """Document rows: `source_id, title, type, locator, folder, tags, chunks, ts, disabled`."""
        return documents_of(self._call(LIST_DOCUMENTS, list_args(**kwargs)))

    def search(self, query: str, **kwargs: Any) -> List[Dict[str, Any]]:
        """Documents whose stamped metadata matches `query`.

        This filters the LISTING, it does not search the text. To get answers out of the contents,
        recall against them: `context.recall("...")` reads the ingested chunks.
        """
        return self.list(query=query, **kwargs)

    def ingest_text(
        self,
        title: str,
        text: str,
        *,
        url: Optional[str] = None,
        source_type: str = "doc",
        folder: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {"title": title, "text": text, "source_type": source_type}
        if url:
            args["url"] = url
        if folder:
            args["folder"] = folder
        if tags:
            args["tags"] = list(tags)
        return self._call(INGEST_DOCUMENT, args)

    def ingest_file(
        self,
        path: Union[str, os.PathLike[str]],
        *,
        folder: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = dict(read_file(path))
        if folder:
            args["folder"] = folder
        if tags:
            args["tags"] = list(tags)
        return self._call(INGEST_FILE, args)

    def delete(self, source_id: str) -> Dict[str, Any]:
        return self._call(DELETE_DOCUMENT, {"source_id": source_id})


class AsyncDocuments:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def _call(self, tool: str, args: Dict[str, Any]) -> Any:
        return await self._client._context_transport.acall(tool, args, conversation=None)

    async def list(self, **kwargs: Any) -> List[Dict[str, Any]]:
        return documents_of(await self._call(LIST_DOCUMENTS, list_args(**kwargs)))

    async def search(self, query: str, **kwargs: Any) -> List[Dict[str, Any]]:
        return await self.list(query=query, **kwargs)

    async def ingest_text(
        self,
        title: str,
        text: str,
        *,
        url: Optional[str] = None,
        source_type: str = "doc",
        folder: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = {"title": title, "text": text, "source_type": source_type}
        if url:
            args["url"] = url
        if folder:
            args["folder"] = folder
        if tags:
            args["tags"] = list(tags)
        return await self._call(INGEST_DOCUMENT, args)

    async def ingest_file(
        self,
        path: Union[str, os.PathLike[str]],
        *,
        folder: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        args: Dict[str, Any] = dict(read_file(path))
        if folder:
            args["folder"] = folder
        if tags:
            args["tags"] = list(tags)
        return await self._call(INGEST_FILE, args)

    async def delete(self, source_id: str) -> Dict[str, Any]:
        return await self._call(DELETE_DOCUMENT, {"source_id": source_id})
