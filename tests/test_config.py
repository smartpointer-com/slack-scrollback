"""Token resolution and the config file format."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from slack_scrollback.config import (
    config_candidates,
    default_config_path,
    parse_config,
    resolve_token,
)
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


# -- a token held in someone else's store ----------------------------------


def _json_store(tmp_path: Path, payload: object, name: str = "secrets.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_json_store_can_supply_the_token(tmp_path: Path) -> None:
    """Point at a store rather than copying out of it: a copy goes stale on rotation."""
    store = _json_store(tmp_path, {"slack_bot_token": BOT, "other_key": "ignored"})
    assert resolve_token(config_path=tmp_path / "absent.cfg", environ={"SLACK_BOT_TOKEN_JSON_PATH": str(store)}) == BOT


def test_the_json_path_can_come_from_the_config_file(tmp_path: Path) -> None:
    store = _json_store(tmp_path, {"slack_bot_token": BOT})
    cfg = _cfg(tmp_path, f"SLACK_BOT_TOKEN_JSON_PATH={store}\n")
    assert resolve_token(config_path=cfg, environ={}) == BOT


def test_a_literal_token_beats_the_json_store(tmp_path: Path) -> None:
    store = _json_store(tmp_path, {"slack_bot_token": OTHER_BOT})
    cfg = _cfg(tmp_path, f"SLACK_BOT_TOKEN={BOT}\nSLACK_BOT_TOKEN_JSON_PATH={store}\n")
    assert resolve_token(config_path=cfg, environ={}) == BOT


def test_the_json_store_is_the_last_resort(tmp_path: Path) -> None:
    store = _json_store(tmp_path, {"slack_bot_token": OTHER_BOT})
    cfg = _cfg(tmp_path, f"SLACK_BOT_TOKEN_JSON_PATH={store}\n")
    assert resolve_token(config_path=cfg, environ={"SLACK_BOT_TOKEN": BOT}) == BOT


def test_a_json_store_token_is_validated_like_any_other(tmp_path: Path) -> None:
    store = _json_store(tmp_path, {"slack_bot_token": USER_TOKEN})
    with pytest.raises(ConfigError) as caught:
        resolve_token(config_path=tmp_path / "absent.cfg", environ={"SLACK_BOT_TOKEN_JSON_PATH": str(store)})
    assert "user token" in str(caught.value)


def test_a_missing_json_store_names_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(ConfigError) as caught:
        resolve_token(config_path=tmp_path / "absent.cfg", environ={"SLACK_BOT_TOKEN_JSON_PATH": str(missing)})
    message = str(caught.value)
    assert str(missing) in message
    assert "does not exist" in message


def test_a_json_store_without_the_field_says_which_field(tmp_path: Path) -> None:
    store = _json_store(tmp_path, {"some_other_token": BOT})
    with pytest.raises(ConfigError) as caught:
        resolve_token(config_path=tmp_path / "absent.cfg", environ={"SLACK_BOT_TOKEN_JSON_PATH": str(store)})
    assert "slack_bot_token" in str(caught.value)


def test_a_json_store_that_is_not_json_says_so(tmp_path: Path) -> None:
    store = tmp_path / "broken.json"
    store.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        resolve_token(config_path=tmp_path / "absent.cfg", environ={"SLACK_BOT_TOKEN_JSON_PATH": str(store)})
    assert "not valid JSON" in str(caught.value)


@pytest.mark.parametrize("payload", [["a", "list"], {"slack_bot_token": 42}, {"slack_bot_token": None}])
def test_a_json_store_of_the_wrong_shape_is_reported(payload: object, tmp_path: Path) -> None:
    store = _json_store(tmp_path, payload)
    with pytest.raises(ConfigError):
        resolve_token(config_path=tmp_path / "absent.cfg", environ={"SLACK_BOT_TOKEN_JSON_PATH": str(store)})


def test_an_unreadable_json_store_blames_permissions(tmp_path: Path) -> None:
    store = _json_store(tmp_path, {"slack_bot_token": BOT})
    store.chmod(0o000)
    try:
        with pytest.raises(ConfigError) as caught:
            resolve_token(config_path=tmp_path / "absent.cfg", environ={"SLACK_BOT_TOKEN_JSON_PATH": str(store)})
        assert "cannot read" in str(caught.value)
    finally:
        store.chmod(0o600)


def test_the_json_store_token_never_leaks_into_an_error(tmp_path: Path) -> None:
    store = _json_store(tmp_path, {"slack_bot_token": "xoxp-secret-user-token"})
    with pytest.raises(ConfigError) as caught:
        resolve_token(config_path=tmp_path / "absent.cfg", environ={"SLACK_BOT_TOKEN_JSON_PATH": str(store)})
    assert "xoxp-secret-user-token" not in str(caught.value)


def test_the_no_token_error_offers_the_json_route(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_token(config_path=tmp_path / "absent.cfg", environ={})
    assert "SLACK_BOT_TOKEN_JSON_PATH" in str(caught.value)


# -- where the config file is looked for -----------------------------------


def test_config_is_looked_for_in_config_then_secrets(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}
    with mock.patch.dict(os.environ, environ, clear=True):
        found = config_candidates()
    assert [p.name for p in found] == ["slack-scrollback.cfg", "slack-scrollback.env"]
    assert found[0].parent.name == ".config"
    assert found[1].parent.name == ".secrets"


def test_a_secrets_env_file_is_used_when_no_config_exists(tmp_path: Path) -> None:
    """Credentials commonly live in ~/.secrets rather than alongside settings."""
    secrets = tmp_path / ".secrets"
    secrets.mkdir()
    (secrets / "slack-scrollback.env").write_text(f"SLACK_BOT_TOKEN={BOT}\n", encoding="utf-8")
    with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True):
        assert default_config_path().parent.name == ".secrets"
        assert resolve_token(environ={}) == BOT


def test_the_config_dir_wins_when_both_exist(tmp_path: Path) -> None:
    (tmp_path / ".config").mkdir()
    (tmp_path / ".config" / "slack-scrollback.cfg").write_text(f"SLACK_BOT_TOKEN={BOT}\n", encoding="utf-8")
    (tmp_path / ".secrets").mkdir()
    (tmp_path / ".secrets" / "slack-scrollback.env").write_text(f"SLACK_BOT_TOKEN={OTHER_BOT}\n", encoding="utf-8")
    with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True):
        assert resolve_token(environ={}) == BOT


def test_the_config_env_var_overrides_both(tmp_path: Path) -> None:
    explicit = _cfg(tmp_path, f"SLACK_BOT_TOKEN={BOT}\n")
    assert config_candidates({"SLACK_SCROLLBACK_CONFIG": str(explicit)}) == [explicit]


def test_the_conventional_path_is_named_when_nothing_exists(tmp_path: Path) -> None:
    with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=True):
        assert default_config_path().parent.name == ".config"


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
