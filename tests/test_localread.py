"""Archive-backed reads: parity with live reads, and the archive-only behaviours.

The archive's core promise is that switching backends changes nothing but the
row source: the same spec resolves to the same conversation, the same window
returns the same messages, and the same renderer produces the same lines — so
a recipe written against Slack works unchanged against local disk. The parity
tests here hold both backends against one shared fixture and compare whole
rendered line lists, because a difference in any character (a name, an indent,
a trailer) is a broken promise, not cosmetic drift.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from slack_scrollback.archive import Archive
from slack_scrollback.errors import UsageError
from slack_scrollback.format import format_timestamp, render_messages
from slack_scrollback.localread import ArchiveReader
from slack_scrollback.workspace import FetchResult, Workspace
from tests.conftest import (
    NOW,
    FakeSlack,
    channel,
    make_client,
    message,
    run_sync,
    thread_parent,
    thread_reply,
    ts_at,
)

GENERAL = "C0EXAMPLE1"
RANDOM = "C0EXAMPLE2"
WATERCOOLER = "C0EXAMPLE3"
DM_ID = "D0EXAMPLE1"

TS_MORNING = ts_at(100)
TS_BUDGET = ts_at(200)
TS_MENTION = ts_at(300)
TS_PARENT = ts_at(400)
TS_REPLY_1 = ts_at(410)
TS_REPLY_2 = ts_at(420)
TS_JOIN = ts_at(500)
TS_UMLAUT = ts_at(600)
TS_OK = ts_at(700)
TS_RANDOM = ts_at(800)
TS_DM = ts_at(900)


def build_fake() -> FakeSlack:
    """One workspace holding every message shape the two backends must agree on.

    Plain text, a live user mention, a thread, housekeeping noise, an umlaut
    (case-folding), a sub-3-character message (no trigram possible), a second
    channel, a DM, and a channel the bot can see but not read. The mention and
    the umlaut deliberately live in raw ``text``, because live search matches
    the raw text while the archive matches the rendered text — parity can only
    be asserted where the two coincide.
    """
    fake = FakeSlack(
        channels=[
            channel(GENERAL, "general"),
            channel(RANDOM, "random"),
            channel(WATERCOOLER, "watercooler", is_member=False),
            {"id": DM_ID, "is_im": True, "user": "U0EXAMPLE2"},
        ]
    )
    fake.messages[GENERAL] = [
        message(TS_MORNING, "good morning"),
        message(TS_BUDGET, "the budget is ready"),
        message(TS_MENTION, "ping <@U0EXAMPLE2>"),
        thread_parent(TS_PARENT, reply_count=2, latest_reply=TS_REPLY_2, text="rollout question"),
        message(TS_JOIN, "alice has joined the channel", subtype="channel_join"),
        message(TS_UMLAUT, "Übermorgen das budget", user="U0EXAMPLE2"),
        message(TS_OK, "ok"),
    ]
    fake.threads[(GENERAL, TS_PARENT)] = [
        thread_reply(TS_PARENT, TS_REPLY_1, "first answer"),
        thread_reply(TS_PARENT, TS_REPLY_2, "second answer", user="U0EXAMPLE2"),
    ]
    fake.messages[RANDOM] = [message(TS_RANDOM, "totally quiet in here")]
    fake.messages[DM_ID] = [message(TS_DM, "hello from the dm", user="U0EXAMPLE2")]
    return fake


def synced_reader(fake: FakeSlack, archive_dir: Path) -> tuple[ArchiveReader, Archive]:
    """Sync the fake once and open the resulting archive for reading."""
    _, archive, _ = run_sync(fake, archive_dir)
    return ArchiveReader(archive), archive


def live_workspace(fake: FakeSlack) -> Workspace:
    """A fresh live backend over the same fake, sharing no state with the sync."""
    client, _ = make_client(fake.handlers())
    return Workspace(client)


def render_live(workspace: Workspace, result: FetchResult, *, limit: int) -> list[str]:
    return render_messages(
        result, resolve_user=workspace.user_name, resolve_channel=workspace.channel_name, limit=limit, provenance=None
    )


def render_archive(reader: ArchiveReader, result: FetchResult, *, limit: int) -> list[str]:
    return render_messages(
        result, resolve_user=reader.user_name, resolve_channel=reader.channel_name, limit=limit, provenance=None
    )


def search_keys(result: FetchResult) -> list[tuple[str, str]]:
    """Message identity across backends: ts alone collides across channels."""
    return [(entry.conversation.id, str(entry.message["ts"])) for entry in result.entries]


# -- history parity ----------------------------------------------------------


@pytest.mark.parametrize(("spec", "expected_lines"), [("#general", 9), ("#random", 1), ("@bob", 1)])
def test_history_reads_identically_from_both_backends(
    spec: str, expected_lines: int, tmp_path: Path, local_zone: Callable[[str], None]
) -> None:
    """The core promise: an archive history is a live history, line for line."""
    local_zone("Europe/Zurich")
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    workspace = live_workspace(fake)

    live = workspace.fetch_history(workspace.resolve(spec), limit=50)
    local = reader.fetch_history(reader.resolve(spec), limit=50)

    live_lines = render_live(workspace, live, limit=50)
    assert len(live_lines) == expected_lines
    assert render_archive(reader, local, limit=50) == live_lines
    assert local.truncated == live.truncated


def test_a_since_until_subwindow_means_the_same_thing_in_both_backends(
    tmp_path: Path, local_zone: Callable[[str], None]
) -> None:
    """Window semantics are part of the contract: inclusive at both ends, everywhere."""
    local_zone("Europe/Zurich")
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    workspace = live_workspace(fake)

    live = workspace.fetch_history(workspace.resolve("#general"), oldest=TS_BUDGET, latest=TS_JOIN, limit=50)
    local = reader.fetch_history(reader.resolve("#general"), oldest=TS_BUDGET, latest=TS_JOIN, limit=50)

    live_lines = render_live(workspace, live, limit=50)
    # Both boundary messages plus the thread in between: 4 channel-level + 2 replies.
    assert len(live_lines) == 6
    assert render_archive(reader, local, limit=50) == live_lines


def test_a_truncating_limit_cuts_both_backends_identically(tmp_path: Path, local_zone: Callable[[str], None]) -> None:
    """Both must keep the same newest messages and announce the cut the same way."""
    local_zone("Europe/Zurich")
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    workspace = live_workspace(fake)

    live = workspace.fetch_history(workspace.resolve("#general"), limit=3)
    local = reader.fetch_history(reader.resolve("#general"), limit=3)

    assert live.truncated and local.truncated
    live_lines = render_live(workspace, live, limit=3)
    assert render_archive(reader, local, limit=3) == live_lines
    assert "truncated" in live_lines[-1]


@pytest.mark.parametrize("limit", [1, 2])
def test_replies_count_against_the_cap_exactly_as_live_counts_them(
    limit: int, tmp_path: Path, local_zone: Callable[[str], None]
) -> None:
    """A cap landing inside a thread's replies must truncate identically.

    limit=1 exhausts the budget on the parent itself; limit=2 leaves room for
    one of two replies — both are cuts that only an explicit flag reveals.
    """
    local_zone("Europe/Zurich")
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    workspace = live_workspace(fake)

    live = workspace.fetch_history(workspace.resolve("#general"), oldest=TS_PARENT, latest=TS_REPLY_2, limit=limit)
    local = reader.fetch_history(reader.resolve("#general"), oldest=TS_PARENT, latest=TS_REPLY_2, limit=limit)

    assert live.truncated and local.truncated
    assert render_archive(reader, local, limit=limit) == render_live(workspace, live, limit=limit)


def test_skipping_thread_expansion_reads_identically(tmp_path: Path, local_zone: Callable[[str], None]) -> None:
    local_zone("Europe/Zurich")
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    workspace = live_workspace(fake)

    live = workspace.fetch_history(workspace.resolve("#general"), limit=50, expand_threads=False)
    local = reader.fetch_history(reader.resolve("#general"), limit=50, expand_threads=False)

    live_lines = render_live(workspace, live, limit=50)
    assert len(live_lines) == 7
    assert not [line for line in live_lines if line.startswith("  ")]
    assert render_archive(reader, local, limit=50) == live_lines


# -- thread parity -----------------------------------------------------------


def test_a_thread_reads_identically_from_both_backends(tmp_path: Path, local_zone: Callable[[str], None]) -> None:
    local_zone("Europe/Zurich")
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    workspace = live_workspace(fake)

    live = workspace.fetch_thread(workspace.resolve("#general"), TS_PARENT)
    local = reader.fetch_thread(reader.resolve("#general"), TS_PARENT)

    live_lines = render_live(workspace, live, limit=200)
    assert len(live_lines) == 3
    assert not live_lines[0].startswith("  ")
    assert all(line.startswith("  ") for line in live_lines[1:])
    assert render_archive(reader, local, limit=200) == live_lines


# -- search parity -----------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "from_user", "expected"),
    [
        # Case-insensitive substring: 'budget' is inside 'Übermorgen das budget' too.
        ("budget", None, [("C0EXAMPLE1", ts_at(200)), ("C0EXAMPLE1", ts_at(600))]),
        # Unicode case-folding: 'über' must find 'Übermorgen' on every path.
        ("über", None, [("C0EXAMPLE1", ts_at(600))]),
        # Two characters cannot form a trigram, so this exercises the scan even with FTS.
        ("ok", None, [("C0EXAMPLE1", ts_at(700))]),
        # --from by the fragment of a name someone actually types, and by raw id.
        ("budget", "ali", [("C0EXAMPLE1", ts_at(200))]),
        ("budget", "U0EXAMPLE2", [("C0EXAMPLE1", ts_at(600))]),
        # Housekeeping subtypes are room traffic, not speech, on either backend.
        ("joined", None, []),
    ],
)
def test_search_returns_the_same_messages_live_with_fts_and_without(
    query: str, from_user: str | None, expected: list[tuple[str, str]], tmp_path: Path
) -> None:
    """The §-search-parity property: three code paths, one answer."""
    fake = build_fake()
    reader, archive = synced_reader(fake, tmp_path)
    workspace = live_workspace(fake)
    # If FTS were quietly missing, "with the index" would test the scan twice.
    assert archive.fts_usable

    live = workspace.search(query, conversations=workspace.readable_conversations(), from_user=from_user)
    with_index = reader.search(query, conversations=reader.readable_conversations(), from_user=from_user)
    archive.fts_unavailable_reason = "forced by test"
    scanned = reader.search(query, conversations=reader.readable_conversations(), from_user=from_user)

    assert search_keys(live) == expected
    assert search_keys(with_index) == expected
    assert search_keys(scanned) == expected


def test_the_scan_notice_appears_exactly_when_the_index_is_unavailable(tmp_path: Path) -> None:
    """The fallback is honest about happening, and silent about not happening."""
    fake = build_fake()
    reader, archive = synced_reader(fake, tmp_path)
    conversations = reader.readable_conversations()

    indexed = reader.search("budget", conversations=conversations)
    assert indexed.notes == []

    archive.fts_unavailable_reason = "forced by test"
    scanned = reader.search("budget", conversations=conversations)
    assert scanned.notes == ["archive search fell back to a full scan — forced by test"]


def test_from_matching_nobody_names_who_did_speak_in_both_backends(tmp_path: Path) -> None:
    """Both backends must turn an empty --from result into a correctable answer.

    The name lists are built differently — live resolves the authors it
    scanned, the archive reads stored sender names — so the assertion is on
    the phrasing and on a speaker both must know about, not on equality.
    """
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    workspace = live_workspace(fake)

    live = workspace.search("budget", conversations=workspace.readable_conversations(), from_user="zzz")
    local = reader.search("budget", conversations=reader.readable_conversations(), from_user="zzz")

    assert live.entries == []
    assert local.entries == []
    for notes in (live.notes, local.notes):
        text = " ".join(notes)
        assert "nobody matching 'zzz'" in text
        assert "alice" in text


# -- gone filtering ----------------------------------------------------------


def test_a_deletion_sync_stops_serving_the_message_everywhere(tmp_path: Path) -> None:
    """gone_at is a soft delete: the row survives, but no read may show it again."""
    fake = build_fake()
    run_sync(fake, tmp_path)

    # Slack-side deletions: one plain message and one thread reply vanish.
    fake.messages[GENERAL] = [
        thread_parent(TS_PARENT, reply_count=1, latest_reply=TS_REPLY_2, text="rollout question")
        if str(m["ts"]) == TS_PARENT
        else m
        for m in fake.messages[GENERAL]
        if str(m["ts"]) != TS_BUDGET
    ]
    fake.threads[(GENERAL, TS_PARENT)] = [
        reply for reply in fake.threads[(GENERAL, TS_PARENT)] if str(reply["ts"]) != TS_REPLY_1
    ]
    _, archive, _ = run_sync(fake, tmp_path, full=True)
    reader = ArchiveReader(archive)
    conversation = reader.resolve("#general")

    history_ts = [str(e.message["ts"]) for e in reader.fetch_history(conversation, limit=50).entries]
    assert TS_BUDGET not in history_ts
    assert TS_REPLY_1 not in history_ts
    assert TS_UMLAUT in history_ts

    thread_ts = [str(e.message["ts"]) for e in reader.fetch_thread(conversation, TS_PARENT).entries]
    assert thread_ts == [TS_PARENT, TS_REPLY_2]

    conversations = reader.readable_conversations()
    assert search_keys(reader.search("budget", conversations=conversations)) == [(GENERAL, TS_UMLAUT)]
    assert search_keys(reader.search("first answer", conversations=conversations)) == []


# -- provenance --------------------------------------------------------------


def test_provenance_names_the_sync_moment_and_the_way_back_to_slack(
    tmp_path: Path, local_zone: Callable[[str], None]
) -> None:
    """Pinned in full: this string is the output contract for archive answers."""
    local_zone("Europe/Zurich")
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)

    when = format_timestamp(f"{NOW:.6f}")
    assert when == "2023-11-14 23:13"
    assert reader.provenance() == f"from local archive, synced {when} — pass --live to read Slack directly"


# -- threads the archive does not hold ----------------------------------------


def test_an_unsynced_thread_says_how_to_ask_slack_instead(tmp_path: Path) -> None:
    """The archive lags reality, so its miss must point at --live, not shrug."""
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    with pytest.raises(UsageError) as caught:
        reader.fetch_thread(reader.resolve("#general"), "1699999999.000001")
    assert "--live" in str(caught.value)


# -- resolution ---------------------------------------------------------------


@pytest.mark.parametrize("spec", [GENERAL, "#general", "general"])
def test_a_channel_resolves_by_id_name_or_bare_name(spec: str, tmp_path: Path) -> None:
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    assert reader.resolve(spec).id == GENERAL


def test_a_dm_resolves_by_a_partial_person_name(tmp_path: Path) -> None:
    """ "@bo" must reach "@bob": people are named by whatever fragment is known."""
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    assert reader.resolve("@bo").id == DM_ID


def test_an_unknown_name_suggests_the_closest_channel(tmp_path: Path) -> None:
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    with pytest.raises(UsageError) as caught:
        reader.resolve("#genral")
    assert "general" in str(caught.value)


def test_a_stored_but_not_joined_channel_names_the_invite_fix(tmp_path: Path) -> None:
    """Sync stores non-member channels precisely so this error can be specific."""
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    with pytest.raises(UsageError) as caught:
        reader.resolve("#watercooler")
    assert "/invite" in str(caught.value)


def test_readable_conversations_match_live_in_content_and_order(tmp_path: Path) -> None:
    """Same membership filtering, same kind-then-name ordering as the live list."""
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    workspace = live_workspace(fake)

    live = [(c.id, c.name, c.kind) for c in workspace.readable_conversations()]
    local = [(c.id, c.name, c.kind) for c in reader.readable_conversations()]
    assert local == live
    assert [name for _, name, _ in local] == ["#general", "#random", "@bob"]


# -- names ---------------------------------------------------------------------


def test_name_lookups_fall_back_to_the_raw_id(tmp_path: Path) -> None:
    """An unknown id must still render as something traceable, like live does."""
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    assert reader.user_name("U0EXAMPLE1") == "alice"
    assert reader.user_name("U0GHOST") == "U0GHOST"
    assert reader.user_name(None) == "unknown"
    assert reader.channel_name(GENERAL) == "general"
    assert reader.channel_name("C0GHOST") == "C0GHOST"


# -- permalinks ------------------------------------------------------------------


def test_permalinks_match_live_including_the_thread_pane_query(tmp_path: Path) -> None:
    """Composed from stored auth.test state, byte-identical to the live composition."""
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    workspace = live_workspace(fake)
    live_conversation = workspace.resolve("#general")
    local_conversation = reader.resolve("#general")

    plain = message(TS_MORNING, "good morning")
    plain_link = reader.permalink(local_conversation, plain)
    assert plain_link == workspace.permalink(live_conversation, plain)
    assert plain_link is not None
    assert "?" not in plain_link

    reply = thread_reply(TS_PARENT, TS_REPLY_1, "first answer")
    reply_link = reader.permalink(local_conversation, reply)
    assert reply_link == workspace.permalink(live_conversation, reply)
    assert reply_link is not None
    assert reply_link.endswith(f"?thread_ts={TS_PARENT}&cid={GENERAL}")


# -- last activity ----------------------------------------------------------------


def test_last_activity_reports_the_newest_ts_per_conversation(tmp_path: Path) -> None:
    """Exactly the fixture's newest ts each — and nothing for the unreadable channel."""
    fake = build_fake()
    reader, _ = synced_reader(fake, tmp_path)
    assert reader.last_activity_map() == {GENERAL: TS_OK, RANDOM: TS_RANDOM, DM_ID: TS_DM}
