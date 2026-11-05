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


# A concrete model id: a gpt-oss or Qwen name, a Tileward-prefixed name (capitalised, as the
# catalogue spells them), or a lowercase Tileward alias that starts with a digit. Docs must use a
# placeholder or the served list. Sample output is the one place an id may appear literally.
# The capitalised form leaves out Tileward-com, tileward-cli, tileward.com and TILEWARD_* alone.
MODEL_ID = re.compile(
    r"(?i:\b(?:gpt-oss|qwen\d))[\w.-]*"
    r"|(?<![\w-])Tileward-[A-Z0-9][\w.-]*"
    r"|(?<![\w-])tileward-\d[\w.-]*"
)
SAMPLE_OUTPUT = "<!-- sample output -->"
PAGES = [DOCS.parent / "README.md", *sorted(DOCS.glob("*.md"))]


def model_ids_outside_sample_output(text: str) -> list:
    """Each (line number, line) that names a concrete model id outside a sample-output block.

    A fenced block counts as sample output when the line right above its opening fence is the
    marker.
    """
    found = []
    fenced = False
    sample = False
    previous = ""
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("```"):
            fenced = not fenced
            sample = fenced and previous == SAMPLE_OUTPUT
        elif MODEL_ID.search(line) and not (fenced and sample):
            found.append((number, line.strip()))
        if line.strip():
            previous = line.strip()
    return found


def test_the_model_id_check_allows_sample_output_and_flags_prose():
    assert model_ids_outside_sample_output(f"{SAMPLE_OUTPUT}\n```console\nqwen3 8k\n```\n") == []
    assert model_ids_outside_sample_output("Pass gpt-oss-20b here.\n") != []
    assert model_ids_outside_sample_output("```bash\ntwcli chat -m tileward-35b-a3b\n```\n") != []
    assert model_ids_outside_sample_output("Pass Tileward-Example-7b here.\n") != []
    assert model_ids_outside_sample_output("Pass Tileward-Alpha-Flash here.\n") != []
    for kept in [
        "https://github.com/Tileward-com/tileward-cli",
        "see tileward.com and tileward-cli",
        "export TILEWARD_API_KEY=tw_live_x",
        "the Tileward Models surface",
        "as `X-Tileward-Context-Saved`, a header",
    ]:
        assert model_ids_outside_sample_output(kept) == [], kept


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_docs_name_no_concrete_model_id(page):
    found = model_ids_outside_sample_output(page.read_text(encoding="utf-8"))
    assert found == [], f"{page.name} names a model id outside sample output: {found}"
