"""Rendering messages: mrkdwn flattening, speakers, files, trailers."""

from __future__ import annotations

import json
from typing import Any

import pytest

from slack_scrollback.format import (
    entry_to_dict,
    format_entry,
    format_timestamp,
    iso_timestamp,
    message_body,
    render_channels,
    render_messages,
    render_text,
    speaker,
    truncation_notice,
    unescape,
)
from slack_scrollback.workspace import Conversation, Entry, FetchResult
from tests.conftest import conversation, message

USERS = {"U0EXAMPLE1": "alice", "U0EXAMPLE2": "bob"}
CHANNELS = {"C0EXAMPLE1": "general", "C0EXAMPLE9": "random"}


def user_of(user_id: str) -> str:
    return USERS.get(user_id, user_id)


def channel_of(channel_id: str) -> str:
    return CHANNELS.get(channel_id, channel_id)


def render(text: str) -> str:
    return render_text(text, resolve_user=user_of, resolve_channel=channel_of)


# -- escaping --------------------------------------------------------------


def test_only_slacks_three_escapes_are_undone() -> None:
    assert unescape("a &lt;b&gt; c &amp; d") == "a <b> c & d"


def test_html_entities_that_slack_does_not_escape_are_left_alone() -> None:
    """A generic HTML unescaper would corrupt these; they are literal user text."""
    assert unescape("&nbsp;&copy;&#39;&quot;") == "&nbsp;&copy;&#39;&quot;"


def test_ampersand_is_resolved_last() -> None:
    """&amp;lt; is somebody typing "&lt;" — it must not collapse into "<"."""
    assert unescape("&amp;lt;") == "&lt;"
    assert unescape("&amp;amp;") == "&amp;"


def test_entities_are_parsed_before_escapes_are_undone() -> None:
    """Text where someone literally typed <@U0EXAMPLE1> must not become a mention."""
    assert render("&lt;@U0EXAMPLE1&gt;") == "<@U0EXAMPLE1>"


# -- references ------------------------------------------------------------


def test_user_mentions_resolve_to_names() -> None:
    assert render("hi <@U0EXAMPLE1>") == "hi @alice"


def test_unknown_user_mentions_keep_the_id() -> None:
    assert render("hi <@U0NOBODY>") == "hi @U0NOBODY"


def test_a_mention_label_is_preferred_over_a_lookup() -> None:
    assert render("hi <@U0EXAMPLE1|ali>") == "hi @ali"


def test_channel_references_resolve() -> None:
    assert render("see <#C0EXAMPLE9>") == "see #random"
    assert render("see <#C0EXAMPLE9|rand>") == "see #rand"


@pytest.mark.parametrize(
    ("text", "expected"), [("<!here>", "@here"), ("<!channel>", "@channel"), ("<!everyone>", "@everyone")]
)
def test_broadcast_keywords(text: str, expected: str) -> None:
    assert render(text) == expected


def test_user_groups_render_as_their_handle() -> None:
    assert render("<!subteam^S0EXAMPLE|@ops>") == "@ops"


def test_dates_fall_back_to_their_authored_text() -> None:
    assert render("<!date^1392734382^{date_short}|Feb 18, 2014>") == "Feb 18, 2014"


def test_links_prefer_their_label() -> None:
    assert render("<https://example.com|the docs>") == "the docs"


def test_bare_links_render_as_the_url() -> None:
    assert render("<https://example.com>") == "https://example.com"


def test_mailto_links_render_readably() -> None:
    assert render("<mailto:a@example.com|a@example.com>") == "a@example.com"


def test_several_references_in_one_message() -> None:
    assert render("<@U0EXAMPLE1> see <#C0EXAMPLE9> at <https://x.test|here> &amp; tell <@U0EXAMPLE2>") == (
        "@alice see #random at here & tell @bob"
    )


def test_empty_text_renders_empty() -> None:
    assert render("") == ""


# -- speakers --------------------------------------------------------------


def test_a_human_speaker_is_resolved() -> None:
    assert speaker(message("1.0"), user_of) == "alice"


def test_bot_messages_prefer_the_per_message_username() -> None:
    msg = {"ts": "1.0", "bot_id": "B0EXAMPLE1", "username": "Deploy Bot", "subtype": "bot_message"}
    assert speaker(msg, user_of) == "Deploy Bot"


def test_bot_messages_fall_back_to_the_bot_profile_then_the_id() -> None:
    assert speaker({"bot_id": "B0EXAMPLE1", "bot_profile": {"name": "CI"}}, user_of) == "CI"
    assert speaker({"bot_id": "B0EXAMPLE1"}, user_of) == "B0EXAMPLE1"


def test_a_speakerless_message_still_renders() -> None:
    assert speaker({"ts": "1.0"}, user_of) == "unknown"


# -- bodies ----------------------------------------------------------------


def test_files_are_summarised_by_name() -> None:
    entry = Entry(
        message=message("1.0", text="see this", files=[{"id": "F1", "name": "budget.xlsx"}]),
        conversation=conversation(),
    )
    assert message_body(entry, resolve_user=user_of, resolve_channel=channel_of) == "see this [file: budget.xlsx]"


def test_a_degraded_file_still_renders() -> None:
    """Slack strips name/url from files aged out of retention, leaving id and mode."""
    entry = Entry(
        message=message("1.0", text="", files=[{"id": "F1", "mode": "hidden_by_limit"}]), conversation=conversation()
    )
    assert message_body(entry, resolve_user=user_of, resolve_channel=channel_of) == "[file: unavailable file]"


def test_attachments_prefer_their_plain_text_fallback() -> None:
    entry = Entry(
        message=message("1.0", text="", attachments=[{"fallback": "PR #12 merged", "title": "x"}]),
        conversation=conversation(),
    )
    assert message_body(entry, resolve_user=user_of, resolve_channel=channel_of) == "[attachment: PR #12 merged]"


def test_attachments_without_a_fallback_are_composed() -> None:
    entry = Entry(
        message=message("1.0", text="", attachments=[{"author_name": "alice", "title": "Report"}]),
        conversation=conversation(),
    )
    assert "alice — Report" in message_body(entry, resolve_user=user_of, resolve_channel=channel_of)


def test_an_empty_message_says_so_rather_than_rendering_blank() -> None:
    entry = Entry(message=message("1.0", text=""), conversation=conversation())
    assert message_body(entry, resolve_user=user_of, resolve_channel=channel_of) == "(no text)"


def test_newlines_are_flattened_so_one_message_is_one_line() -> None:
    entry = Entry(message=message("1.0", text="line one\nline two\n\nline three"), conversation=conversation())
    body = message_body(entry, resolve_user=user_of, resolve_channel=channel_of)
    assert "\n" not in body
    assert body == "line one line two line three"


# -- lines -----------------------------------------------------------------


def test_a_line_carries_time_speaker_and_text() -> None:
    entry = Entry(message=message("1700000000.000100", text="hi"), conversation=conversation())
    line = format_entry(entry, resolve_user=user_of, resolve_channel=channel_of)
    assert line.endswith("] alice: hi")
    assert line.startswith("[")


def test_replies_are_indented() -> None:
    entry = Entry(message=message("1700000000.000100", text="hi"), conversation=conversation(), depth=1)
    assert format_entry(entry, resolve_user=user_of, resolve_channel=channel_of).startswith("  [")


def test_the_channel_is_shown_only_when_asked() -> None:
    entry = Entry(message=message("1700000000.000100"), conversation=conversation())
    with_channel = format_entry(entry, resolve_user=user_of, resolve_channel=channel_of, show_channel=True)
    assert "#general alice:" in with_channel


def test_permalinks_are_appended_when_present() -> None:
    entry = Entry(message=message("1700000000.000100"), conversation=conversation())
    line = format_entry(entry, resolve_user=user_of, resolve_channel=channel_of, permalink="https://x.test/p1")
    assert line.endswith("https://x.test/p1")


def test_timestamps_survive_nonsense() -> None:
    assert format_timestamp("not-a-ts") == "????-??-?? ??:??"


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("UTC", "2023-11-14 22:13"),
        ("Europe/Zurich", "2023-11-14 23:13"),  # UTC+1 in November
        ("America/New_York", "2023-11-14 17:13"),  # UTC-5 in November
    ],
)
def test_timestamps_render_in_local_time(zone: str, expected: str, local_zone: Any) -> None:
    """Times are shown as the people who wrote them saw them, not in UTC."""
    local_zone(zone)
    assert format_timestamp("1700000000.000100") == expected


def test_the_iso_timestamp_carries_an_offset(local_zone: Any) -> None:
    local_zone("Europe/Zurich")
    assert iso_timestamp("1700000000.000100") == "2023-11-14T23:13:20+01:00"


# -- json ------------------------------------------------------------------


def test_json_keeps_the_timestamp_verbatim() -> None:
    """ts is the message's identity; a float round-trip would destroy its low digits."""
    entry = Entry(message=message("1700000000.000001"), conversation=conversation())
    payload = entry_to_dict(entry, resolve_user=user_of, resolve_channel=channel_of)
    assert payload["ts"] == "1700000000.000001"


def test_json_has_stable_keys() -> None:
    entry = Entry(message=message("1700000000.000100"), conversation=conversation())
    payload = entry_to_dict(entry, resolve_user=user_of, resolve_channel=channel_of)
    assert set(payload) == {
        "type",
        "ts",
        "time",
        "channel",
        "channel_id",
        "user_id",
        "user",
        "text",
        "thread_ts",
        "is_reply",
        "subtype",
        "files",
        "permalink",
    }


def test_json_announces_truncation_too() -> None:
    """A JSON consumer must not mistake a truncated read for a complete one.

    The trailer is the only signal that the answer is partial, so dropping it in
    --json would silently hand back a wrong answer that looks whole.
    """
    result = FetchResult(
        entries=[Entry(message=message("1700000000.000100"), conversation=conversation())],
        truncated=True,
    )
    lines = render_messages(result, resolve_user=user_of, resolve_channel=channel_of, limit=1, as_json=True)
    records = [json.loads(line) for line in lines]
    notices = [r for r in records if r["type"] == "notice"]
    assert len(notices) == 1
    assert notices[0]["truncated"] is True
    assert "--limit" in notices[0]["text"]


def test_json_announces_throttling_and_notes() -> None:
    result = FetchResult(entries=[], throttled=True, notes=["#x: stopped early"])
    records = [
        json.loads(line)
        for line in render_messages(result, resolve_user=user_of, resolve_channel=channel_of, limit=200, as_json=True)
    ]
    texts = " ".join(r["text"] for r in records if r["type"] == "notice")
    assert "stopped early" in texts
    assert "Marketplace" in texts


def test_json_messages_are_discriminated_from_notices() -> None:
    result = FetchResult(
        entries=[Entry(message=message("1700000000.000100"), conversation=conversation())],
        truncated=True,
    )
    records = [
        json.loads(line)
        for line in render_messages(result, resolve_user=user_of, resolve_channel=channel_of, limit=1, as_json=True)
    ]
    assert [r["type"] for r in records] == ["message", "notice"]


def test_a_complete_json_read_carries_no_notice() -> None:
    result = FetchResult(entries=[Entry(message=message("1700000000.000100"), conversation=conversation())])
    records = [
        json.loads(line)
        for line in render_messages(result, resolve_user=user_of, resolve_channel=channel_of, limit=200, as_json=True)
    ]
    assert all(r["type"] == "message" for r in records)


def test_permalinks_are_keyed_per_channel_not_per_timestamp() -> None:
    """A Slack ts is unique only within a channel, so search results can collide."""
    same_ts = "1700000000.000100"
    here = conversation("C0EXAMPLE1", "#general")
    there = conversation("C0EXAMPLE2", "#random")
    result = FetchResult(
        entries=[
            Entry(message=message(same_ts, "in general"), conversation=here),
            Entry(message=message(same_ts, "in random"), conversation=there),
        ]
    )
    links = {
        (here.id, same_ts): "https://acme.slack.com/archives/C0EXAMPLE1/p1700000000000100",
        (there.id, same_ts): "https://acme.slack.com/archives/C0EXAMPLE2/p1700000000000100",
    }
    lines = render_messages(
        result, resolve_user=user_of, resolve_channel=channel_of, limit=200, show_channel=True, permalinks=links
    )
    assert "C0EXAMPLE1" in lines[0] and "C0EXAMPLE2" in lines[1]


def test_json_output_is_one_object_per_line() -> None:
    result = FetchResult(
        entries=[Entry(message=message(f"170000000{i}.000100"), conversation=conversation()) for i in range(3)]
    )
    lines = render_messages(result, resolve_user=user_of, resolve_channel=channel_of, limit=200, as_json=True)
    assert len(lines) == 3
    assert all(json.loads(line)["user"] == "alice" for line in lines)


# -- trailers --------------------------------------------------------------


def test_the_truncation_trailer_names_the_flag_that_raises_the_cap() -> None:
    notice = truncation_notice(limit=200)
    assert "--limit" in notice
    assert "truncated" in notice


def test_truncation_is_announced_in_text_output() -> None:
    result = FetchResult(
        entries=[Entry(message=message("1700000000.000100"), conversation=conversation())], truncated=True
    )
    lines = render_messages(result, resolve_user=user_of, resolve_channel=channel_of, limit=1)
    assert any("truncated" in line for line in lines)


def test_throttling_is_announced_with_the_reason() -> None:
    result = FetchResult(entries=[], throttled=True)
    lines = render_messages(result, resolve_user=user_of, resolve_channel=channel_of, limit=200)
    assert any("Marketplace" in line for line in lines)


def test_notes_are_surfaced() -> None:
    result = FetchResult(entries=[], notes=["#x: stopped early"])
    lines = render_messages(result, resolve_user=user_of, resolve_channel=channel_of, limit=200)
    assert any("stopped early" in line for line in lines)


def test_no_results_says_so_explicitly() -> None:
    lines = render_messages(FetchResult(), resolve_user=user_of, resolve_channel=channel_of, limit=200)
    assert lines == ["(no messages found)"]


def test_json_output_stays_machine_readable_when_empty() -> None:
    assert (
        render_messages(FetchResult(), resolve_user=user_of, resolve_channel=channel_of, limit=200, as_json=True) == []
    )


# -- channels table --------------------------------------------------------


def test_the_channel_table_has_a_header_and_a_row() -> None:
    lines = render_channels([(conversation(), "1700000000.000100")])
    assert "CHANNEL" in lines[0]
    assert "#general" in lines[1]
    assert "C0EXAMPLE1" in lines[1]


def test_unknown_activity_renders_as_a_dash() -> None:
    assert "-" in render_channels([(conversation(), None)])[1]


def test_an_empty_workspace_explains_what_to_do() -> None:
    lines = render_channels([])
    assert "/invite" in lines[0]


def test_channels_as_json() -> None:
    payload = json.loads(render_channels([(conversation(), "1700000000.000100")], as_json=True)[0])
    assert payload["id"] == "C0EXAMPLE1"
    assert payload["name"] == "#general"
    assert payload["last_activity_ts"] == "1700000000.000100"


def test_long_channel_names_do_not_break_the_table() -> None:
    long_name = Conversation(id="C0EXAMPLE2", kind="public", raw={}, name="#" + "x" * 80)
    lines = render_channels([(long_name, None)])
    assert len(lines) == 2


# -- files as objects, enrichment, and provenance ------------------------------


def entry_with_file(**file_extra: Any) -> Entry:
    raw_file = {
        "id": "F0EXAMPLE1",
        "name": "plan.pdf",
        "mimetype": "application/pdf",
        "size": 1234,
        "permalink": "https://acme.slack.com/files/U0EXAMPLE1/F0EXAMPLE1/plan.pdf",
        "url_private": "https://files.slack.com/files-pri/T0EXAMPLE1-F0EXAMPLE1/plan.pdf",
        **file_extra,
    }
    return Entry(message=message("1700000000.000001", files=[raw_file]), conversation=conversation())


class TestFileObjects:
    """JSON `files` entries are objects an agent can act on, never bare names."""

    def test_shape_carries_identity_metadata_and_permalink(self) -> None:
        payload = entry_to_dict(entry_with_file(), resolve_user=user_of, resolve_channel=channel_of)
        (ref,) = payload["files"]
        assert ref == {
            "id": "F0EXAMPLE1",
            "name": "plan.pdf",
            "mimetype": "application/pdf",
            "size": 1234,
            "permalink": "https://acme.slack.com/files/U0EXAMPLE1/F0EXAMPLE1/plan.pdf",
            "local_path": None,
        }

    def test_url_private_never_leaks_into_the_record(self) -> None:
        payload = entry_to_dict(entry_with_file(), resolve_user=user_of, resolve_channel=channel_of)
        assert "files-pri" not in json.dumps(payload)

    def test_local_path_is_filled_by_the_lookup(self, tmp_path: Any) -> None:
        target = tmp_path / "media" / "F0EXAMPLE1" / "plan.pdf"
        payload = entry_to_dict(
            entry_with_file(),
            resolve_user=user_of,
            resolve_channel=channel_of,
            local_path_of=lambda file_id: target if file_id == "F0EXAMPLE1" else None,
        )
        assert payload["files"][0]["local_path"] == str(target)

    def test_a_retention_stub_still_yields_a_well_formed_object(self) -> None:
        entry = Entry(message=message("1700000000.000001", files=[{"id": "F0EXAMPLE2"}]), conversation=conversation())
        payload = entry_to_dict(entry, resolve_user=user_of, resolve_channel=channel_of)
        (ref,) = payload["files"]
        assert ref["id"] == "F0EXAMPLE2"
        assert ref["name"] is None
        assert ref["local_path"] is None


class TestFilePermalinksInTextMode:
    def test_appended_only_when_the_message_link_is_wanted(self) -> None:
        entry = entry_with_file()
        result = FetchResult(entries=[entry])
        linked = render_messages(
            result,
            resolve_user=user_of,
            resolve_channel=channel_of,
            limit=10,
            permalinks={("C0EXAMPLE1", "1700000000.000001"): "https://acme.slack.com/archives/C0EXAMPLE1/p1"},
        )
        assert linked[0].endswith(
            "https://acme.slack.com/archives/C0EXAMPLE1/p1 https://acme.slack.com/files/U0EXAMPLE1/F0EXAMPLE1/plan.pdf"
        )
        bare = render_messages(result, resolve_user=user_of, resolve_channel=channel_of, limit=10)
        assert "files/U0EXAMPLE1" not in bare[0]


class TestProvenance:
    """An answer from local disk must say so, in both renderings, last of all."""

    PROVENANCE = "from local archive, synced 2026-07-16 14:32 — pass --live to read Slack directly"

    def test_text_mode_ends_with_the_bracketed_trailer(self) -> None:
        result = FetchResult(entries=[Entry(message=message("1700000000.000001"), conversation=conversation())])
        result.truncated = True
        lines = render_messages(
            result, resolve_user=user_of, resolve_channel=channel_of, limit=1, provenance=self.PROVENANCE
        )
        assert lines[-1] == f"[{self.PROVENANCE}]"
        assert "truncated" in lines[-2]

    def test_json_mode_emits_a_notice_record_with_source_archive(self) -> None:
        result = FetchResult(entries=[])
        lines = render_messages(
            result, resolve_user=user_of, resolve_channel=channel_of, limit=1, as_json=True, provenance=self.PROVENANCE
        )
        record = json.loads(lines[-1])
        assert record == {"type": "notice", "source": "archive", "text": self.PROVENANCE}

    def test_channels_table_carries_the_trailer_too(self) -> None:
        rows = [(conversation(), "1700000000.000001")]
        lines = render_channels(rows, provenance=self.PROVENANCE)
        assert lines[-1] == f"[{self.PROVENANCE}]"
        json_lines = render_channels(rows, as_json=True, provenance=self.PROVENANCE)
        assert json.loads(json_lines[-1])["source"] == "archive"

    def test_no_provenance_means_no_trailer(self) -> None:
        result = FetchResult(entries=[])
        lines = render_messages(result, resolve_user=user_of, resolve_channel=channel_of, limit=1, as_json=True)
        assert lines == []
