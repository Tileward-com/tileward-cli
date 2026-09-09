"""The docs claim a command set and an environment. Check they still exist.

Docs go stale silently; a test is the only thing that notices.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest

from tileward.cli.main import cli

DOCS = Path(__file__).resolve().parent.parent / "docs"
SOURCE = Path(__file__).resolve().parent.parent / "src" / "tileward"


def documented_commands() -> set:
    text = (DOCS / "cli.md").read_text(encoding="utf-8")
    index = text.split("<!-- command-index:start -->")[1].split("<!-- command-index:end -->")[0]
    return set(re.findall(r"`twcli ([a-z][a-z -]*?)`", index))


def real_commands() -> set:
    found = set()

    def walk(group: click.Group, prefix: str = "") -> None:
        for name, sub in group.commands.items():
            path = f"{prefix}{name}"
            if isinstance(sub, click.Group):
                walk(sub, path + " ")
            else:
                found.add(path)

    walk(cli)
    return found


def test_the_command_index_lists_every_command():
    assert real_commands() - documented_commands() == set()


def test_the_command_index_lists_nothing_that_does_not_exist():
    assert documented_commands() - real_commands() == set()


def test_every_environment_variable_is_documented():
    documented = set(re.findall(r"TILEWARD_[A-Z_]+", (DOCS / "configuration.md").read_text()))
    in_source = set()
    for path in SOURCE.rglob("*.py"):
        in_source.update(re.findall(r"TILEWARD_[A-Z_]+", path.read_text(encoding="utf-8")))
    assert in_source - documented == set()
    assert documented - in_source == set()


@pytest.mark.parametrize("page", sorted(p.name for p in DOCS.glob("*.md")))
def test_internal_links_resolve(page):
    """mkdocs --strict catches these too, but only on a run that builds the site."""
    text = (DOCS / page).read_text(encoding="utf-8")
    for target in re.findall(r"\]\((?!https?:|#)([^)#]+)", text):
        assert (DOCS / target).exists(), f"{page} links to a missing {target}"
