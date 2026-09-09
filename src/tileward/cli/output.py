"""Terminal output: tables for people, JSON for pipes.

With `--json`, stdout carries the JSON and nothing else; notes go to stderr.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Sequence
from typing import Any, Dict, List, Literal, Optional

from rich.console import Console
from rich.table import Table


def _color_system() -> Optional[Literal["auto"]]:
    """`None` disables colour entirely; "auto" lets rich decide from the terminal.

    `NO_COLOR` is the cross-tool convention and is honoured whatever its value, because escape
    codes in a log file are noise that survives forever.
    """
    if os.environ.get("NO_COLOR") or os.environ.get("TILEWARD_NO_COLOR"):
        return None
    return "auto"


class Out:
    """Everything a command prints goes through one of these."""

    def __init__(self, *, as_json: bool = False, quiet: bool = False, color: bool = True) -> None:
        self.as_json = as_json
        self.quiet = quiet
        system = _color_system() if color else None
        self.stdout = Console(color_system=system, soft_wrap=False)
        # `stderr=True` and not merely a second Console: the point is the file descriptor.
        self.stderr = Console(stderr=True, color_system=system)

    # ---- machine ------------------------------------------------------------------------
    def json(self, payload: Any) -> None:
        """Write JSON to stdout, unconditionally and unstyled."""
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=False, default=str) + "\n")
        sys.stdout.flush()

    # ---- human --------------------------------------------------------------------------
    def print(self, *args: Any, **kwargs: Any) -> None:
        if self.as_json:
            return
        self.stdout.print(*args, **kwargs)

    def raw(self, text: str) -> None:
        """Text with no markup interpretation.

        Model output, recall blocks — anything that might contain square brackets, which rich
        would otherwise read as a style tag and swallow.
        """
        if self.as_json:
            return
        sys.stdout.write(text)
        sys.stdout.flush()

    def note(self, message: str) -> None:
        """A progress or context line. Never stdout, so it cannot pollute a pipe."""
        if self.quiet:
            return
        self.stderr.print(f"[dim]{message}[/dim]")

    def warn(self, message: str) -> None:
        self.stderr.print(f"[yellow]![/yellow] {message}")

    def error(self, message: str) -> None:
        self.stderr.print(f"[red]✗[/red] {message}")

    def ok(self, message: str) -> None:
        if self.as_json or self.quiet:
            return
        self.stdout.print(f"[green]✓[/green] {message}")

    def table(
        self,
        rows: Sequence[Dict[str, Any]],
        columns: Sequence[str],
        *,
        title: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        empty: str = "Nothing to show.",
    ) -> None:
        if self.as_json:
            return
        if not rows:
            self.stdout.print(f"[dim]{empty}[/dim]")
            return
        table = Table(title=title, header_style="bold", box=None, pad_edge=False)
        for column in columns:
            table.add_column((headers or {}).get(column, column.replace("_", " ")))
        for row in rows:
            table.add_row(*[_cell(row.get(column)) for column in columns])
        self.stdout.print(table)

    def pairs(self, data: Dict[str, Any], *, title: Optional[str] = None) -> None:
        if self.as_json:
            return
        table = Table(title=title, box=None, show_header=False, pad_edge=False)
        table.add_column(style="dim")
        table.add_column()
        for key, value in data.items():
            table.add_row(str(key), _cell(value))
        self.stdout.print(table)


def _cell(value: Any) -> str:
    if value is None:
        return "[dim]—[/dim]"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "[dim]—[/dim]"
    if isinstance(value, dict):
        return json.dumps(value, default=str)
    return str(value)


def truncate(text: str, width: int = 72) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def rows_from(items: Iterable[Any]) -> List[Dict[str, Any]]:
    return [item for item in items if isinstance(item, dict)]
