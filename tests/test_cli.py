"""Argument parsing and end-to-end command behaviour."""

from __future__ import annotations

from typing import Any

import pytest

from slack_scrollback import cli
from slack_scrollback.errors import UsageError
from tests.conftest import TOKEN, FakeTransport, channel, err, message, ok

PERMALINK = "https://acme.slack.com/archives/C0EXAMPLE1/p1700000000123456"


# -- thread targets --------------------------------------------------------


def test_a_permalink_yields_its_channel_and_timestamp() -> None:
    assert cli.parse_thread_target([PERMALINK]) == ("C0EXAMPLE1", "1700000000.123456")


def test_a_permalink_to_a_reply_resolves_to_its_parent_thread() -> None:
    """Linking a reply should show the whole thread, not one orphaned message."""
    url = f"{PERMALINK}?thread_ts=1699999999.000100&cid=C0EXAMPLE1"
    assert cli.parse_thread_target([url]) == ("C0EXAMPLE1", "1699999999.000100")


def test_a_channel_and_timestamp_are_accepted() -> None:
    assert cli.parse_thread_target(["#general", "1700000000.123456"]) == ("#general", "1700000000.123456")


@pytest.mark.parametrize("bad", ["not-a-url", "https://acme.slack.com/", "https://acme.slack.com/archives/C0EXAMPLE1"])
def test_an_unusable_permalink_explains_the_shape_expected(bad: str) -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse_thread_target([bad])
    assert "archives" in str(caught.value)


def test_a_malformed_timestamp_is_named_as_such() -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse_thread_target(["#general", "yesterday"])
    assert "1700000000.123456" in str(caught.value)


def test_too_many_arguments_show_both_accepted_forms() -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse_thread_target(["a", "b", "c"])
    message_text = str(caught.value)
    assert "permalink" in message_text
    assert "1700000000.123456" in message_text


# -- parser ----------------------------------------------------------------


def test_every_subcommand_is_reachable() -> None:
    parser = cli.build_parser()
    for command in ("channels", "history", "thread", "search"):
        args = parser.parse_args([command, *(["x"] if command in ("history", "thread", "search") else [])])
        assert args.command == command


def test_every_subcommand_works_with_no_optional_flags() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["channels"]).json is False
    assert parser.parse_args(["history", "#general"]).limit == cli.DEFAULT_LIMIT
    assert parser.parse_args(["search", "budget"]).since == cli.DEFAULT_SEARCH_WINDOW


def test_search_scoping_flags_are_named_naturally() -> None:
    args = cli.build_parser().parse_args(["search", "budget", "--in", "#general", "--from", "@alice"])
    assert (args.in_channel, args.from_user) == ("#general", "@alice")


def test_no_command_prints_help_rather_than_failing_obscurely(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "usage:" in capsys.readouterr().out


def test_limit_must_be_positive() -> None:
    args = cli.build_parser().parse_args(["history", "#general", "--limit", "0"])
    with pytest.raises(UsageError):
        cli._limit_of(args)


def test_since_after_until_is_rejected() -> None:
    args = cli.build_parser().parse_args(["history", "#general", "--since", "2026-01-31", "--until", "2026-01-01"])
    with pytest.raises(UsageError) as caught:
        cli._window(args)
    assert "--since" in str(caught.value)


# -- end to end ------------------------------------------------------------

CONVERSATIONS = ok(channels=[channel("C0EXAMPLE1", "general")])
USER = ok(user={"id": "U0EXAMPLE1", "profile": {"display_name": "alice"}})


def run(monkeypatch: pytest.MonkeyPatch, argv: list[str], handlers: dict[str, Any]) -> tuple[int, str, str]:
    transport = FakeTransport(handlers=handlers)
    monkeypatch.setattr("slack_scrollback.cli.SlackClient", lambda token, **kw: _client(token, transport))
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    code = cli.main(argv)
    return code, "", ""


def _client(token: str, transport: FakeTransport) -> Any:
    from slack_scrollback.api import SlackClient

    return SlackClient(token, transport=transport, sleep=lambda _: None)


def test_history_prints_messages(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    handlers = {
        "conversations.list": CONVERSATIONS,
        "conversations.history": ok(messages=[message("1700000000.000100", "hello there")]),
        "users.info": USER,
    }
    code, _, _ = run(monkeypatch, ["history", "#general"], handlers)
    out = capsys.readouterr().out
    assert code == 0
    assert "alice: hello there" in out


def test_an_unknown_channel_exits_non_zero_with_one_actionable_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = run(monkeypatch, ["history", "#nope"], {"conversations.list": CONVERSATIONS, "users.info": USER})
    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1
    assert "slack-scrollback channels" in captured.err


def test_a_slack_error_is_reported_actionably(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handlers = {
        "conversations.list": CONVERSATIONS,
        "conversations.history": err("missing_scope", needed="channels:history"),
        "users.info": USER,
    }
    code, _, _ = run(monkeypatch, ["history", "#general"], handlers)
    assert code == 1
    assert "channels:history" in capsys.readouterr().err


def test_a_bad_token_never_reaches_the_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxp-user-token")
    code = cli.main(["channels"])
    assert code == 1
    assert "user token" in capsys.readouterr().err


def test_the_token_never_appears_in_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    handlers = {"conversations.list": err("invalid_auth")}
    code, _, _ = run(monkeypatch, ["channels"], handlers)
    captured = capsys.readouterr()
    assert code == 1
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


def test_channels_lists_readable_conversations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handlers = {
        "conversations.list": CONVERSATIONS,
        "conversations.history": ok(messages=[message("1700000000.000100")]),
    }
    code, _, _ = run(monkeypatch, ["channels", "--no-activity"], handlers)
    out = capsys.readouterr().out
    assert code == 0
    assert "#general" in out


def test_json_output_is_parseable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    handlers = {
        "conversations.list": CONVERSATIONS,
        "conversations.history": ok(messages=[message("1700000000.000100", "hi")]),
        "users.info": USER,
    }
    run(monkeypatch, ["history", "#general", "--json"], handlers)
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["text"] == "hi"
    assert payload["ts"] == "1700000000.000100"


def test_links_flag_appends_permalinks_and_its_absence_does_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handlers = {
        "conversations.list": CONVERSATIONS,
        "conversations.history": ok(messages=[message("1700000000.000100", "hi")]),
        "users.info": USER,
        "auth.test": ok(url="https://acme.slack.com/"),
    }
    run(monkeypatch, ["history", "#general", "--links"], handlers)
    assert "https://acme.slack.com/archives/C0EXAMPLE1/p1700000000000100" in capsys.readouterr().out
    run(monkeypatch, ["history", "#general"], handlers)
    assert "https://acme.slack.com" not in capsys.readouterr().out


def test_no_threads_flag_suppresses_the_replies_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = message("1700000000.000100", "q", thread_ts="1700000000.000100", reply_count=1)
    reply = message("1700000001.000100", "a", thread_ts="1700000000.000100")
    handlers = {
        "conversations.list": CONVERSATIONS,
        "conversations.history": ok(messages=[parent]),
        "conversations.replies": ok(messages=[parent, reply]),
        "users.info": USER,
    }
    run(monkeypatch, ["history", "#general"], handlers)
    assert "a" in capsys.readouterr().out
    run(monkeypatch, ["history", "#general", "--no-threads"], handlers)
    assert "q" in capsys.readouterr().out


def test_in_flag_restricts_the_search_to_one_conversation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    two = ok(channels=[channel("C0EXAMPLE1", "general"), channel("C0EXAMPLE2", "random")])
    per_channel = {
        "C0EXAMPLE1": [message("1700000000.000100", "budget in general")],
        "C0EXAMPLE2": [message("1700000001.000100", "budget in random")],
    }
    handlers = {
        "conversations.list": two,
        "conversations.history": lambda p: ok(messages=per_channel.get(p.get("channel", ""), [])),
        "users.info": USER,
    }
    run(monkeypatch, ["search", "budget", "--in", "#general"], handlers)
    out = capsys.readouterr().out
    assert "budget in general" in out
    assert "budget in random" not in out


def test_no_activity_flag_skips_the_per_conversation_lookup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handlers = {
        "conversations.list": CONVERSATIONS,
        "conversations.history": ok(messages=[message("1700000000.000100")]),
    }
    run(monkeypatch, ["channels"], handlers)
    assert "2023" in capsys.readouterr().out  # the message's real date is rendered
    run(monkeypatch, ["channels", "--no-activity"], handlers)
    assert "-" in capsys.readouterr().out


def test_channels_lists_the_most_recently_active_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    three = ok(
        channels=[
            channel("C0EXAMPLE1", "stale"),
            channel("C0EXAMPLE2", "busy"),
        ]
    )
    last = {"C0EXAMPLE1": "1600000000.000100", "C0EXAMPLE2": "1700000000.000100"}
    handlers = {
        "conversations.list": three,
        "conversations.history": lambda p: ok(messages=[message(last[p["channel"]])]),
    }
    run(monkeypatch, ["channels"], handlers)
    rows = [line for line in capsys.readouterr().out.splitlines() if line.startswith("#")]
    assert rows[0].startswith("#busy")
    assert rows[1].startswith("#stale")


def test_json_truncation_notice_reaches_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json as _json

    handlers = {
        "conversations.list": CONVERSATIONS,
        "conversations.history": ok(messages=[message(f"17000000{i:02d}.000100") for i in range(5)], has_more=True),
        "users.info": USER,
    }
    run(monkeypatch, ["history", "#general", "--json", "--limit", "2"], handlers)
    records = [_json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert any(r["type"] == "notice" and r["truncated"] for r in records)


def test_search_reports_no_matches_rather_than_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handlers = {
        "conversations.list": CONVERSATIONS,
        "conversations.history": ok(messages=[message("1700000000.000100", "hi")]),
    }
    code, _, _ = run(monkeypatch, ["search", "nothing-matches-this"], handlers)
    assert code == 0
    assert "(no messages found)" in capsys.readouterr().out
