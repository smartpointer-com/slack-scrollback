"""Token resolution and the config file format."""

from __future__ import annotations

from pathlib import Path

import pytest

from slack_scrollback.config import parse_config, resolve_token
from slack_scrollback.errors import ConfigError

BOT = "xoxb-1111-2222-abcdef"
OTHER_BOT = "xoxb-9999-8888-zyxwvu"
USER_TOKEN = "xoxp-1111-2222-abcdef"


def _cfg(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "slack-scrollback.cfg"
    path.write_text(text, encoding="utf-8")
    return path


def test_flag_beats_environment_and_file(tmp_path: Path) -> None:
    path = _cfg(tmp_path, f"SLACK_BOT_TOKEN={OTHER_BOT}\n")
    assert resolve_token(flag=BOT, config_path=path, environ={"SLACK_BOT_TOKEN": OTHER_BOT}) == BOT


def test_environment_beats_file(tmp_path: Path) -> None:
    path = _cfg(tmp_path, f"SLACK_BOT_TOKEN={OTHER_BOT}\n")
    assert resolve_token(config_path=path, environ={"SLACK_BOT_TOKEN": BOT}) == BOT


def test_file_is_the_last_resort(tmp_path: Path) -> None:
    path = _cfg(tmp_path, f"SLACK_BOT_TOKEN={BOT}\n")
    assert resolve_token(config_path=path, environ={}) == BOT


def test_missing_token_names_every_way_to_supply_one(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_token(config_path=tmp_path / "absent.cfg", environ={})
    message = str(caught.value)
    assert "--token" in message
    assert "SLACK_BOT_TOKEN" in message
    assert "absent.cfg" in message


def test_absent_config_file_is_not_an_error(tmp_path: Path) -> None:
    assert resolve_token(config_path=tmp_path / "absent.cfg", environ={"SLACK_BOT_TOKEN": BOT}) == BOT


# -- token kind ------------------------------------------------------------


def test_user_tokens_are_rejected_by_design(tmp_path: Path) -> None:
    """A user token would read whatever the human can see, not what the bot was invited to."""
    with pytest.raises(ConfigError) as caught:
        resolve_token(flag=USER_TOKEN, config_path=tmp_path / "absent.cfg", environ={})
    message = str(caught.value)
    assert "user token" in message
    assert "xoxb-" in message


@pytest.mark.parametrize("bad", ["hunter2", "xoxa-123", "xoxe.xoxp-1-abc", "Bearer xoxb-1"])
def test_tokens_that_are_not_bot_tokens_are_rejected(bad: str, tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_token(flag=bad, config_path=tmp_path / "absent.cfg", environ={})
    assert "xoxb-" in str(caught.value)


def test_rejection_says_which_source_was_wrong(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_token(config_path=tmp_path / "absent.cfg", environ={"SLACK_BOT_TOKEN": USER_TOKEN})
    assert "$SLACK_BOT_TOKEN" in str(caught.value)

    path = _cfg(tmp_path, f"SLACK_BOT_TOKEN={USER_TOKEN}\n")
    with pytest.raises(ConfigError) as caught:
        resolve_token(config_path=path, environ={})
    assert str(path) in str(caught.value)


def test_surrounding_whitespace_is_tolerated(tmp_path: Path) -> None:
    assert resolve_token(flag=f"  {BOT}  ", config_path=tmp_path / "absent.cfg", environ={}) == BOT


def test_empty_values_fall_through_to_the_next_source(tmp_path: Path) -> None:
    path = _cfg(tmp_path, f"SLACK_BOT_TOKEN={BOT}\n")
    assert resolve_token(flag=None, config_path=path, environ={"SLACK_BOT_TOKEN": ""}) == BOT


# -- config file format ----------------------------------------------------


def test_parses_keys_comments_and_blank_lines() -> None:
    parsed = parse_config(
        "\n".join(
            [
                "# a comment",
                "",
                f"SLACK_BOT_TOKEN={BOT}",
                "   # indented comment",
                "OTHER = spaced out ",
            ]
        )
    )
    assert parsed == {"SLACK_BOT_TOKEN": BOT, "OTHER": "spaced out"}


@pytest.mark.parametrize("quoted", [f'"{BOT}"', f"'{BOT}'"])
def test_quotes_are_stripped(quoted: str) -> None:
    assert parse_config(f"SLACK_BOT_TOKEN={quoted}")["SLACK_BOT_TOKEN"] == BOT


def test_mismatched_quotes_are_left_alone() -> None:
    assert parse_config("K=\"value'")["K"] == "\"value'"


def test_values_may_contain_equals_signs() -> None:
    assert parse_config("K=a=b=c")["K"] == "a=b=c"


def test_a_line_without_equals_is_an_error_that_shows_the_line() -> None:
    with pytest.raises(ConfigError) as caught:
        parse_config("SLACK_BOT_TOKEN\n")
    message = str(caught.value)
    assert "line 1" in message
    assert "KEY=VALUE" in message


def test_the_format_is_data_not_shell() -> None:
    """No interpolation, no export, no substitution — the value is taken literally."""
    parsed = parse_config("K=$HOME/x\nJ=`whoami`\nL=${OTHER}\n")
    assert parsed == {"K": "$HOME/x", "J": "`whoami`", "L": "${OTHER}"}
