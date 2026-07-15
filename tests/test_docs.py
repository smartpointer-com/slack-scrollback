"""The docs make executable claims, so they are checked like code.

A recipe in SKILL.md that does not parse is worse than no recipe: a small model
copies it verbatim, gets a usage error, and has nothing to correct towards. The
same goes for a flag a document mentions and the parser has never heard of.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
from pathlib import Path

import pytest

from slack_scrollback import cli
from slack_scrollback.api import READ_ONLY_METHODS

ROOT = Path(__file__).parent.parent
SKILL = (ROOT / "SKILL.md").read_text()
README = (ROOT / "README.md").read_text()

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z-]+")
_PREFIX = "slack-scrollback "


def _code_fragments(text: str) -> list[str]:
    """Every span the reader would recognise as a command: inline code and shell lines."""
    fragments = [m.group(1).strip() for m in _INLINE_CODE.finditer(text)]
    fragments += [line.strip().removeprefix("$ ").strip() for line in text.splitlines()]
    return fragments


def _commands(text: str) -> list[str]:
    seen: list[str] = []
    for fragment in _code_fragments(text):
        if not fragment.startswith(_PREFIX):
            continue
        # shlex handles the trailing "# comment" without eating '#general'.
        try:
            argv = shlex.split(fragment, comments=True)
        except ValueError:
            continue
        # A <placeholder> stands for a value the caller supplies; substitute one
        # rather than dropping it, or a template loses a required argument and
        # looks broken when it is not.
        argv = ["PLACEHOLDER" if a.startswith("<") else a for a in argv[1:]]
        if argv and " ".join(argv) not in seen:
            seen.append(" ".join(argv))
    return seen


def _documented_flags(text: str) -> set[str]:
    """Flags the document presents as real, ignoring prose and unrelated tooling."""
    flags: set[str] = set()
    for fragment in _code_fragments(text):
        if fragment.startswith(_PREFIX) or (fragment.startswith("--") and " " not in fragment.split("=")[0]):
            flags.update(_FLAG.findall(fragment))
    return flags


def _parses(argv: list[str]) -> bool:
    parser = cli.build_parser()
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parser.parse_args(argv)
        except SystemExit:
            return False
    return True


@pytest.mark.parametrize("command", _commands(SKILL))
def test_every_command_in_the_skill_parses(command: str) -> None:
    assert _parses(shlex.split(command)), f"SKILL.md documents an unparseable command: slack-scrollback {command}"


@pytest.mark.parametrize("command", _commands(README))
def test_every_command_in_the_readme_parses(command: str) -> None:
    assert _parses(shlex.split(command)), f"README.md documents an unparseable command: slack-scrollback {command}"


def test_the_skill_has_a_recipe_for_every_subcommand() -> None:
    documented = {command.split()[0] for command in _commands(SKILL)}
    assert {"channels", "history", "thread", "search"} <= documented


def _real_flags(parser: argparse.ArgumentParser) -> set[str]:
    found: set[str] = set()
    for action in parser._actions:
        found.update(opt for opt in action.option_strings if opt.startswith("--"))
    for sub in (a for a in parser._actions if isinstance(a, argparse._SubParsersAction)):
        for child in sub.choices.values():
            found |= _real_flags(child)
    return found


REAL_FLAGS = _real_flags(cli.build_parser())


@pytest.mark.parametrize(("doc", "name"), [(SKILL, "SKILL.md"), (README, "README.md")])
def test_no_document_offers_a_flag_that_does_not_exist(doc: str, name: str) -> None:
    invented = _documented_flags(doc) - REAL_FLAGS
    assert not invented, f"{name} offers flags the CLI does not have: {sorted(invented)}"


def test_the_skill_documents_every_flag_an_agent_could_want() -> None:
    """A flag the model never hears about may as well not exist."""
    operator_only = {"--token", "--config", "--timeout", "--version", "--help"}
    missing = REAL_FLAGS - _documented_flags(SKILL) - operator_only
    assert not missing, f"SKILL.md never mentions: {sorted(missing)}"


def test_the_skill_description_is_sized_for_selection() -> None:
    """Name and description are the only signal a model gets when choosing a skill."""
    match = re.search(r"^description:\s*(.+)$", SKILL, re.M)
    assert match
    description = match.group(1).strip()
    assert 150 <= len(description) <= 350, f"description is {len(description)} chars"
    for keyword in ("slack", "history", "search", "channel", "thread", "said"):
        assert keyword in description.lower(), f"description omits the word {keyword!r}"


def test_the_readme_lists_exactly_the_allowlisted_methods() -> None:
    for method in READ_ONLY_METHODS:
        assert method in README, f"README omits the allowlisted method {method}"


def test_the_readme_never_presents_an_absent_method_as_available() -> None:
    claimed = set(re.findall(r"`(users\.\w+|conversations\.\w+|chat\.\w+|auth\.\w+)`", README))
    # Named only to explain why they are absent, or as an operator's own step.
    explained = {"chat.getPermalink", "users.list", "conversations.join"}
    assert claimed - explained <= READ_ONLY_METHODS
