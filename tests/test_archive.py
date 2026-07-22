"""The storage layer: schema lifecycle, upserts, sync queries, media bookkeeping, FTS, and the view.

Everything here drives :class:`Archive` directly — no Slack fakes. The archive's
connection is autocommit in these tests; production wraps runs in one
transaction, but the storage semantics under test are the same either way.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import slack_scrollback.archive as archive_module
from slack_scrollback.archive import (
    HOUSEKEEPING_SUBTYPES,
    SCHEMA_VERSION,
    Archive,
    fts5_trigram_available,
    resolve_media_path,
    sync_lock,
)
from slack_scrollback.errors import ScrollbackError, UsageError

NOW = 1_700_000_000.0
CHANNEL = "C0EXAMPLE1"
OTHER_CHANNEL = "C0EXAMPLE2"
FILE_ID = "F0EXAMPLE1"
FOREVER = 9_999_999_999.0

requires_fts = pytest.mark.skipif(
    not fts5_trigram_available(), reason="this Python's SQLite lacks FTS5 with the trigram tokenizer"
)


def run_sql(archive: Archive, sql: str, *params: object) -> list[sqlite3.Row]:
    """Plain SQL against the open archive — PRAGMAs and the view are asserted raw."""
    return list(archive._con.execute(sql, params))


def put_conversation(archive: Archive, *, conversation_id: str = CHANNEL, name: str = "#general") -> None:
    archive.upsert_conversation(conversation_id=conversation_id, name=name, kind="public", is_member=True, now=NOW)


def put_message(
    archive: Archive,
    *,
    channel_id: str = CHANNEL,
    ts: str = "100.000100",
    thread_ts: str | None = None,
    subtype: str | None = None,
    user_id: str | None = "U0EXAMPLE1",
    sender_name: str = "alice",
    text: str = "hello",
    raw: dict[str, Any] | None = None,
    edited_ts: str | None = None,
    now: float = NOW,
) -> str:
    return archive.upsert_message(
        channel_id=channel_id,
        ts=ts,
        thread_ts=thread_ts,
        subtype=subtype,
        user_id=user_id,
        sender_name=sender_name,
        text=text,
        raw=raw if raw is not None else {"ts": ts, "text": text},
        edited_ts=edited_ts,
        now=now,
    )


def put_file(
    archive: Archive,
    *,
    file_id: str = FILE_ID,
    name: str | None = "plan.pdf",
    mimetype: str | None = "application/pdf",
    filetype: str | None = "pdf",
    size: int | None = 6,
    mode: str | None = "hosted",
    permalink: str | None = "https://acme.slack.com/files/U0EXAMPLE1/F0EXAMPLE1/plan.pdf",
    url_private: str | None = "https://files.slack.com/files-pri/T0EXAMPLE1-F0EXAMPLE1/plan.pdf",
    now: float = NOW,
) -> None:
    archive.upsert_file(
        file_id=file_id,
        name=name,
        mimetype=mimetype,
        filetype=filetype,
        size=size,
        mode=mode,
        permalink=permalink,
        url_private=url_private,
        now=now,
    )


def tombstone(archive: Archive, file_id: str, *, now: float) -> None:
    """A deletion stub as Slack sends it: mode alone, every other field empty."""
    archive.upsert_file(
        file_id=file_id,
        name=None,
        mimetype=None,
        filetype=None,
        size=None,
        mode="tombstone",
        permalink=None,
        url_private=None,
        now=now,
    )


def must_file_row(archive: Archive, file_id: str) -> sqlite3.Row:
    row = archive.file_row(file_id)
    assert row is not None
    return row


@pytest.fixture
def archive(tmp_path: Path) -> Iterator[Archive]:
    opened = Archive.open_rw(tmp_path)
    yield opened
    opened.close()


# -- schema and lifecycle ----------------------------------------------------


def test_open_rw_builds_the_whole_archive_in_one_step(tmp_path: Path) -> None:
    """Every reader trusts this exact shape, so a single first open must build all of it."""
    opened = Archive.open_rw(tmp_path)
    assert (tmp_path / "archive.db").is_file()
    assert (tmp_path / "media").is_dir()
    tables = {str(row["name"]) for row in run_sql(opened, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meta", "conversations", "users", "messages", "files", "message_files", "sync_state"} <= tables
    views = {str(row["name"]) for row in run_sql(opened, "SELECT name FROM sqlite_master WHERE type='view'")}
    assert "messages_flat" in views
    assert run_sql(opened, "PRAGMA user_version")[0][0] == SCHEMA_VERSION
    assert opened.get_meta("schema_version") == str(SCHEMA_VERSION)
    opened.close()


def test_the_journal_mode_is_delete_not_wal(archive: Archive) -> None:
    """WAL makes readers create -shm/-wal files beside the db; read-only consumers cannot."""
    assert run_sql(archive, "PRAGMA journal_mode")[0][0] == "delete"


def test_reopening_an_existing_archive_changes_nothing(tmp_path: Path) -> None:
    first = Archive.open_rw(tmp_path)
    put_conversation(first)
    put_message(first, ts="100.000100", text="still here")
    first.close()
    again = Archive.open_rw(tmp_path)
    assert [str(row["text"]) for row in run_sql(again, "SELECT text FROM messages")] == ["still here"]
    assert run_sql(again, "PRAGMA user_version")[0][0] == SCHEMA_VERSION
    again.close()


def test_open_ro_refuses_to_conjure_an_archive_nobody_built(tmp_path: Path) -> None:
    """An empty answer from a database nobody synced would read as "nothing was said"."""
    with pytest.raises(UsageError) as caught:
        Archive.open_ro(tmp_path)
    assert "slack-scrollback sync" in str(caught.value)


@pytest.mark.parametrize("opener", [Archive.open_rw, Archive.open_ro], ids=["rw", "ro"])
def test_a_newer_schema_is_refused_by_every_opener(tmp_path: Path, opener: Callable[[Path], Archive]) -> None:
    """Guessing at a future schema could corrupt it; the only safe move is to name the fix."""
    Archive.open_rw(tmp_path).close()
    bump = sqlite3.connect(tmp_path / "archive.db")
    bump.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    bump.commit()
    bump.close()
    with pytest.raises(ScrollbackError) as caught:
        opener(tmp_path)
    text = str(caught.value)
    assert "newer" in text
    assert "upgrade" in text


# -- upsert_message ------------------------------------------------------------


def test_a_first_write_is_new_and_an_identical_rewrite_is_unchanged(archive: Archive) -> None:
    """Sync re-reads a trailing window every run; stable rows must not count as churn."""
    assert put_message(archive, ts="100.000100") == "new"
    assert put_message(archive, ts="100.000100") == "unchanged"


def test_a_changed_rendered_text_counts_as_an_edit(archive: Archive) -> None:
    put_message(archive, ts="100.000100", text="hello")
    assert put_message(archive, ts="100.000100", text="hello, but reworded") == "edited"


def test_a_changed_edit_timestamp_counts_as_an_edit(archive: Archive) -> None:
    """Slack can re-save identical text; the edit stamp is still reader-visible content."""
    put_message(archive, ts="100.000100", text="hello")
    result = put_message(
        archive,
        ts="100.000100",
        text="hello",
        raw={"ts": "100.000100", "text": "hello", "edited": {"ts": "200.000100"}},
        edited_ts="200.000100",
    )
    assert result == "edited"


def test_raw_churn_with_the_same_content_is_unchanged_but_still_stored(archive: Archive) -> None:
    """Reply counts and reactions drift constantly — not edits, but raw must not go stale."""
    put_message(archive, ts="100.000100", raw={"ts": "100.000100", "text": "hello", "reply_count": 1})
    result = put_message(archive, ts="100.000100", raw={"ts": "100.000100", "text": "hello", "reply_count": 2})
    assert result == "unchanged"
    stored = json.loads(str(run_sql(archive, "SELECT raw FROM messages WHERE ts = ?", "100.000100")[0]["raw"]))
    assert stored["reply_count"] == 2


def test_a_gone_row_that_slack_serves_again_is_unmarked(archive: Archive) -> None:
    """Deletion detection can misfire; a message Slack still returns was never deleted."""
    put_message(archive, ts="100.000100")
    archive.mark_messages_gone(CHANNEL, ["100.000100"], NOW + 10)
    assert run_sql(archive, "SELECT gone_at FROM messages WHERE ts = ?", "100.000100")[0]["gone_at"] == NOW + 10
    put_message(archive, ts="100.000100")
    assert run_sql(archive, "SELECT gone_at FROM messages WHERE ts = ?", "100.000100")[0]["gone_at"] is None


# -- window and thread queries ---------------------------------------------------


def test_channel_level_ts_mirrors_what_a_history_response_holds(archive: Archive) -> None:
    """Deletion detection diffs this set against conversations.history: a plain reply in it
    would be marked gone on every run, and a broadcast missing from it never would be."""
    put_message(archive, ts="100.000100")
    put_message(archive, ts="110.000100", thread_ts="110.000100")
    put_message(archive, ts="120.000100", thread_ts="110.000100", subtype="thread_broadcast")
    put_message(archive, ts="130.000100", thread_ts="110.000100")
    put_message(archive, ts="140.000100")
    archive.mark_messages_gone(CHANNEL, ["140.000100"], NOW)
    put_message(archive, ts="500.000100")
    assert archive.channel_level_ts_between(CHANNEL, 90.0, 200.0) == {"100.000100", "110.000100", "120.000100"}


def test_reply_queries_exclude_the_parent_and_the_gone(archive: Archive) -> None:
    """Slack's replies response includes the parent; counting it would inflate every thread."""
    put_message(archive, ts="110.000100", thread_ts="110.000100")
    put_message(archive, ts="130.000100", thread_ts="110.000100")
    put_message(archive, ts="150.000100", thread_ts="110.000100")
    put_message(archive, ts="160.000100", thread_ts="110.000100")
    archive.mark_messages_gone(CHANNEL, ["160.000100"], NOW)
    assert archive.reply_ts(CHANNEL, "110.000100") == {"130.000100", "150.000100"}
    assert archive.reply_stats(CHANNEL, "110.000100") == (2, "150.000100")


def test_a_replyless_thread_has_zero_stats_and_no_newest_ts(archive: Archive) -> None:
    put_message(archive, ts="110.000100", thread_ts="110.000100")
    assert archive.reply_stats(CHANNEL, "110.000100") == (0, None)


def test_active_threads_are_those_with_any_recent_parent_or_reply(archive: Archive) -> None:
    """A reply to an old thread is invisible in windowed history; recency of any stored row
    in the thread is the only signal that the thread needs re-asking."""
    put_message(archive, ts="100.000100", thread_ts="100.000100")
    put_message(archive, ts="110.000100", thread_ts="100.000100")
    put_message(archive, ts="200.000100", thread_ts="200.000100")
    put_message(archive, ts="300.000100", thread_ts="200.000100")
    put_message(archive, ts="400.000100")
    put_message(archive, ts="500.000100", thread_ts="450.000100")
    archive.mark_messages_gone(CHANNEL, ["500.000100"], NOW)
    assert archive.active_thread_ts(CHANNEL, 250.0) == {"200.000100"}


def test_threads_with_replies_skip_lone_parents_and_gone_replies(archive: Archive) -> None:
    put_message(archive, ts="100.000100", thread_ts="100.000100")
    put_message(archive, ts="200.000100", thread_ts="200.000100")
    put_message(archive, ts="210.000100", thread_ts="200.000100")
    put_message(archive, ts="300.000100", thread_ts="300.000100")
    put_message(archive, ts="310.000100", thread_ts="300.000100")
    archive.mark_messages_gone(CHANNEL, ["310.000100"], NOW)
    assert archive.thread_ts_with_replies(CHANNEL) == {"200.000100"}


# -- files -------------------------------------------------------------------------


def test_refreshed_metadata_never_clobbers_downloaded_bytes(archive: Archive) -> None:
    """Every sync re-upserts file metadata; forgetting where the bytes are would re-download all."""
    put_file(archive)
    stored = str(archive.media_dir / FILE_ID / "plan.pdf")
    archive.set_local_path(FILE_ID, stored, NOW)
    put_file(archive, size=999)
    row = must_file_row(archive, FILE_ID)
    assert row["local_path"] == stored
    assert row["downloaded_at"] == NOW
    assert row["size"] == 999


def test_a_tombstone_keeps_the_metadata_an_earlier_sync_recorded(archive: Archive) -> None:
    """The archived copy is still servable after Slack deletes; the stub must not erase it,
    and the recorded deletion time must not drift on later sightings of the same stub."""
    put_file(archive)
    tombstone(archive, FILE_ID, now=NOW + 10)
    row = must_file_row(archive, FILE_ID)
    assert (row["name"], row["mimetype"], row["size"]) == ("plan.pdf", "application/pdf", 6)
    assert row["mode"] == "tombstone"
    assert row["gone_at"] == NOW + 10
    tombstone(archive, FILE_ID, now=NOW + 99)
    assert must_file_row(archive, FILE_ID)["gone_at"] == NOW + 10


def test_a_tombstone_as_first_sighting_inserts_a_gone_stub(archive: Archive) -> None:
    """A file deleted before any sync saw it whole still deserves a row: the reference exists."""
    tombstone(archive, "F0EXAMPLE2", now=NOW)
    row = must_file_row(archive, "F0EXAMPLE2")
    assert row["mode"] == "tombstone"
    assert row["gone_at"] == NOW


def test_a_real_upsert_after_a_tombstone_brings_the_file_back(archive: Archive) -> None:
    put_file(archive)
    tombstone(archive, FILE_ID, now=NOW + 10)
    put_file(archive)
    row = must_file_row(archive, FILE_ID)
    assert row["gone_at"] is None
    assert row["mode"] == "hosted"


def test_linking_a_file_twice_leaves_one_junction_row(archive: Archive) -> None:
    """Re-syncing a message re-links its files; the junction must absorb that silently."""
    archive.link_file(CHANNEL, "100.000100", FILE_ID)
    archive.link_file(CHANNEL, "100.000100", FILE_ID)
    assert run_sql(archive, "SELECT COUNT(*) AS n FROM message_files")[0]["n"] == 1


def test_the_download_queue_respects_media_tiers(archive: Archive) -> None:
    """Unlabelled bytes count as documents — the indexable default — and no tiers means
    downloads are off entirely, with metadata still recorded."""
    put_file(archive, file_id="F0IMAGE001", name="pic.png", mimetype="image/png")
    put_file(archive, file_id="F0AUDIO001", name="talk.mp3", mimetype="audio/mpeg")
    put_file(archive, file_id="F0VIDEO001", name="demo.mp4", mimetype="video/mp4")
    put_file(archive, file_id="F0DOCUM001", name="plan.pdf", mimetype="application/pdf")
    put_file(archive, file_id="F0MYSTERY1", name="blob.bin", mimetype=None)

    def queued(tiers: frozenset[str]) -> set[str]:
        return {str(row["id"]) for row in archive.download_queue(tiers=tiers, max_bytes=10**9)}

    assert queued(frozenset({"images"})) == {"F0IMAGE001"}
    assert queued(frozenset({"audio"})) == {"F0AUDIO001"}
    assert queued(frozenset({"video"})) == {"F0VIDEO001"}
    assert queued(frozenset({"documents"})) == {"F0DOCUM001", "F0MYSTERY1"}
    assert queued(frozenset()) == set()


def test_the_size_cap_excludes_big_files_but_not_unsized_ones(archive: Archive) -> None:
    """Slack sometimes omits size; treating unknown as too-big would silently skip documents."""
    put_file(archive, file_id="F0SMALL001", size=100)
    put_file(archive, file_id="F0EXACT001", size=1000)
    put_file(archive, file_id="F0LARGE001", size=1001)
    put_file(archive, file_id="F0NOSIZE01", size=None)
    ids = {str(row["id"]) for row in archive.download_queue(tiers=frozenset({"documents"}), max_bytes=1000)}
    assert ids == {"F0SMALL001", "F0EXACT001", "F0NOSIZE01"}


def test_external_tombstoned_gone_and_urlless_files_are_never_queued(archive: Archive) -> None:
    """External files have no Slack-hosted bytes, tombstones and gone rows are deletions,
    and without url_private there is nothing to fetch — queueing any of them wastes requests."""
    put_file(archive, file_id="F0EXTERN01", mode="external")
    put_file(archive, file_id="F0TOMBST01")
    tombstone(archive, "F0TOMBST01", now=NOW)
    put_file(archive, file_id="F0GONE0001")
    run_sql(archive, "UPDATE files SET gone_at = ? WHERE id = ?", NOW, "F0GONE0001")
    put_file(archive, file_id="F0NOURL001", url_private=None)
    put_file(archive, file_id="F0OKAY0001")
    ids = {str(row["id"]) for row in archive.download_queue(tiers=frozenset({"documents"}), max_bytes=10**9)}
    assert ids == {"F0OKAY0001"}


def test_bytes_on_disk_at_the_expected_size_satisfy_the_queue(archive: Archive) -> None:
    """A stored path is a claim, the file is the fact: partial or lost downloads must requeue."""
    put_file(archive, size=6)
    target = archive.media_dir / FILE_ID / "plan.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"123456")
    archive.set_local_path(FILE_ID, str(target), NOW)
    documents = frozenset({"documents"})
    assert archive.download_queue(tiers=documents, max_bytes=10**9) == []
    target.write_bytes(b"123")
    assert [str(row["id"]) for row in archive.download_queue(tiers=documents, max_bytes=10**9)] == [FILE_ID]
    target.unlink()
    assert [str(row["id"]) for row in archive.download_queue(tiers=documents, max_bytes=10**9)] == [FILE_ID]


def test_a_relocated_archive_still_resolves_its_media(tmp_path: Path) -> None:
    """Stored paths go stale when the directory moves; only the part after /media/ is trusted."""
    new_media = tmp_path / "media"
    resolved = resolve_media_path("/old/place/media/F0EXAMPLE1/plan.pdf", new_media)
    assert resolved == new_media / "F0EXAMPLE1" / "plan.pdf"


def test_a_path_without_a_media_segment_falls_back_to_its_basename(tmp_path: Path) -> None:
    new_media = tmp_path / "media"
    assert resolve_media_path("/somewhere/else/plan.pdf", new_media) == new_media / "plan.pdf"


def test_local_path_of_answers_only_when_bytes_are_really_there(archive: Archive) -> None:
    """Serving a path to a missing file would hand an agent a dead reference."""
    assert archive.local_path_of("F0UNKNOWN1") is None
    put_file(archive)
    assert archive.local_path_of(FILE_ID) is None
    target = archive.media_dir / FILE_ID / "plan.pdf"
    archive.set_local_path(FILE_ID, str(target), NOW)
    assert archive.local_path_of(FILE_ID) is None
    target.parent.mkdir(parents=True)
    target.write_bytes(b"123456")
    assert archive.local_path_of(FILE_ID) == target


# -- last activity -------------------------------------------------------------


def test_last_activity_is_the_newest_message_still_standing(archive: Archive) -> None:
    """A deleted newest message must not report activity, and a channel of only deletions
    must not appear at all — silence and absence are different answers."""
    put_message(archive, ts="100.000100")
    put_message(archive, ts="200.000100")
    put_message(archive, ts="300.000100")
    archive.mark_messages_gone(CHANNEL, ["300.000100"], NOW)
    put_message(archive, channel_id=OTHER_CHANNEL, ts="400.000100")
    archive.mark_messages_gone(OTHER_CHANNEL, ["400.000100"], NOW)
    assert archive.last_activity_by_channel() == {CHANNEL: "200.000100"}


# -- the sync lock ---------------------------------------------------------------


def test_the_sync_lock_admits_one_writer_at_a_time(tmp_path: Path) -> None:
    """Overlapping scheduled runs must see "held" and bow out, not interleave writes."""
    with sync_lock(tmp_path) as first:
        assert first is True
        with sync_lock(tmp_path) as second:
            assert second is False
    with sync_lock(tmp_path) as again:
        assert again is True


# -- the full-text index -----------------------------------------------------------


@requires_fts
def test_open_rw_builds_the_index_and_its_trigger_triple(archive: Archive) -> None:
    tables = {str(row["name"]) for row in run_sql(archive, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "messages_fts" in tables
    triggers = {str(row["name"]) for row in run_sql(archive, "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert {"messages_fts_ai", "messages_fts_au", "messages_fts_ad"} <= triggers
    assert archive.fts_usable is True
    assert archive.fts_unavailable_reason is None


@requires_fts
def test_short_needles_scan_and_longer_ones_use_the_index(archive: Archive) -> None:
    """Two characters cannot form a trigram; the scan fallback must still surface the row."""
    put_message(archive, ts="100.000100", text="the budget is fine")
    put_message(archive, ts="200.000100", text="unrelated chatter")
    via_index, used_fts = archive.search_candidates("budget", channel_ids=[CHANNEL], oldest=0.0, latest=FOREVER)
    assert used_fts is True
    assert [str(row["ts"]) for row in via_index] == ["100.000100"]
    via_scan, used_scan = archive.search_candidates("bu", channel_ids=[CHANNEL], oldest=0.0, latest=FOREVER)
    assert used_scan is False
    assert "100.000100" in {str(row["ts"]) for row in via_scan}


@requires_fts
def test_the_index_folds_case_and_diacritics(archive: Archive) -> None:
    """The documented search semantics are case-insensitive substring — 'überm' must find
    'Übermorgen', or the index silently changes what matching means."""
    put_message(archive, ts="100.000100", text="wir sehen uns Übermorgen")
    rows, used_fts = archive.search_candidates("überm", channel_ids=[CHANNEL], oldest=0.0, latest=FOREVER)
    assert used_fts is True
    assert [str(row["ts"]) for row in rows] == ["100.000100"]


@requires_fts
def test_an_edit_moves_the_row_in_the_index(archive: Archive) -> None:
    """A stale index entry would resurrect pre-edit text in search results forever."""
    put_message(archive, ts="100.000100", text="the quarterly zebra report")
    put_message(archive, ts="100.000100", text="the quarterly walrus report")
    old_rows, _ = archive.search_candidates("zebra", channel_ids=[CHANNEL], oldest=0.0, latest=FOREVER)
    assert old_rows == []
    new_rows, used_fts = archive.search_candidates("walrus", channel_ids=[CHANNEL], oldest=0.0, latest=FOREVER)
    assert used_fts is True
    assert [str(row["ts"]) for row in new_rows] == ["100.000100"]


@requires_fts
def test_an_archive_built_without_fts_heals_on_the_next_capable_open(tmp_path: Path) -> None:
    """A database first synced on a lesser host must come home: the next capable open
    creates the index and backfills the rows that predate it."""
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(archive_module, "fts5_trigram_available", lambda: False)
        crippled = Archive.open_rw(tmp_path)
        put_message(crippled, ts="100.000100", text="the quarterly zebra report")
        tables = {str(row["name"]) for row in run_sql(crippled, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "messages_fts" not in tables
        assert crippled.fts_usable is False
        assert crippled.fts_unavailable_reason is not None
        assert "FTS5" in crippled.fts_unavailable_reason
        crippled.close()
    healed = Archive.open_rw(tmp_path)
    tables = {str(row["name"]) for row in run_sql(healed, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "messages_fts" in tables
    assert healed.fts_unavailable_reason is None
    rows, used_fts = healed.search_candidates("zebra", channel_ids=[CHANNEL], oldest=0.0, latest=FOREVER)
    assert used_fts is True
    assert [str(row["ts"]) for row in rows] == ["100.000100"]
    healed.close()


@requires_fts
def test_open_ro_names_the_missing_index_and_who_will_build_it(tmp_path: Path) -> None:
    """A read-only open cannot create the index itself; the reason must say sync will."""
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(archive_module, "fts5_trigram_available", lambda: False)
        crippled = Archive.open_rw(tmp_path)
        put_message(crippled, ts="100.000100")
        crippled.close()
    reader = Archive.open_ro(tmp_path)
    assert reader.fts_usable is False
    assert reader.fts_unavailable_reason is not None
    assert "the next sync will build one" in reader.fts_unavailable_reason
    reader.close()


@requires_fts
def test_search_candidates_never_surface_deleted_messages(archive: Archive) -> None:
    """The gone filter lives in the SQL on both paths, not in the caller's re-verification."""
    put_message(archive, ts="100.000100", text="the quarterly zebra report")
    put_message(archive, ts="200.000100", text="another zebra sighting")
    archive.mark_messages_gone(CHANNEL, ["200.000100"], NOW)
    via_index, _ = archive.search_candidates("zebra", channel_ids=[CHANNEL], oldest=0.0, latest=FOREVER)
    assert [str(row["ts"]) for row in via_index] == ["100.000100"]
    via_scan, _ = archive.search_candidates("ze", channel_ids=[CHANNEL], oldest=0.0, latest=FOREVER)
    assert "200.000100" not in {str(row["ts"]) for row in via_scan}


# -- the messages_flat view ---------------------------------------------------------


def test_the_view_serves_one_flat_row_per_living_message(archive: Archive) -> None:
    """messages_flat is a public read contract: external indexers depend on these exact
    columns, so every one of them is pinned here."""
    put_conversation(archive)
    put_message(archive, ts="1700000100.000100", text="hello world", sender_name="alice")
    rows = run_sql(archive, "SELECT * FROM messages_flat")
    assert len(rows) == 1
    row = rows[0]
    assert row["msg_id"] == f"{CHANNEL}:1700000100.000100"
    assert row["chat_jid"] == CHANNEL
    assert row["chat_name"] == "#general"
    assert isinstance(row["ts"], float)
    assert row["ts"] == float("1700000100.000100")
    assert row["sender_name"] == "alice"
    assert row["text"] == "hello world"
    assert row["media_type"] is None
    assert row["filename"] is None
    assert row["mime_type"] is None
    assert row["local_path"] is None


def test_housekeeping_subtypes_stay_out_of_the_view(archive: Archive) -> None:
    """Joins and renames would pollute an external search index; speech subtypes like
    thread_broadcast must survive the same filter."""
    put_conversation(archive)
    for i, subtype in enumerate(HOUSEKEEPING_SUBTYPES):
        put_message(archive, ts=f"{100 + i}.000100", subtype=subtype)
    put_message(archive, ts="500.000100", thread_ts="400.000100", subtype="thread_broadcast")
    msg_ids = [str(row["msg_id"]) for row in run_sql(archive, "SELECT msg_id FROM messages_flat")]
    assert msg_ids == [f"{CHANNEL}:500.000100"]


def test_a_deleted_message_leaves_the_view(archive: Archive) -> None:
    """Soft deletes keep the row in the table but must stop it being served: the view is
    how deletions propagate to external indexes."""
    put_conversation(archive)
    put_message(archive, ts="100.000100")
    archive.mark_messages_gone(CHANNEL, ["100.000100"], NOW)
    assert run_sql(archive, "SELECT * FROM messages_flat") == []


def test_a_message_without_a_conversation_row_is_not_served(archive: Archive) -> None:
    """The inner JOIN needs a conversations row for chat_name; without one the message is
    invisible to the view — asserted as-is, since indexers rely on the JOIN semantics."""
    put_message(archive, ts="100.000100")
    assert run_sql(archive, "SELECT * FROM messages_flat") == []


def test_file_rows_appear_only_once_bytes_are_local(archive: Archive) -> None:
    """A file row with no local bytes would send an indexer chasing bytes it cannot open;
    blank text keeps the row out of chat windows once it does appear."""
    put_conversation(archive)
    put_message(archive, ts="100.000100")
    put_file(archive)
    archive.link_file(CHANNEL, "100.000100", FILE_ID)
    assert run_sql(archive, "SELECT * FROM messages_flat WHERE media_type IS NOT NULL") == []
    archive.set_local_path(FILE_ID, str(archive.media_dir / FILE_ID / "plan.pdf"), NOW)
    file_rows = run_sql(archive, "SELECT * FROM messages_flat WHERE media_type IS NOT NULL")
    assert len(file_rows) == 1
    assert file_rows[0]["msg_id"] == f"{CHANNEL}:100.000100:{FILE_ID}"
    assert file_rows[0]["text"] == ""


def test_a_gone_message_or_gone_file_hides_the_file_row(archive: Archive) -> None:
    put_conversation(archive)
    put_message(archive, ts="100.000100")
    put_file(archive)
    archive.link_file(CHANNEL, "100.000100", FILE_ID)
    archive.set_local_path(FILE_ID, str(archive.media_dir / FILE_ID / "plan.pdf"), NOW)
    assert len(run_sql(archive, "SELECT * FROM messages_flat WHERE media_type IS NOT NULL")) == 1
    run_sql(archive, "UPDATE files SET gone_at = ? WHERE id = ?", NOW, FILE_ID)
    assert run_sql(archive, "SELECT * FROM messages_flat WHERE media_type IS NOT NULL") == []
    run_sql(archive, "UPDATE files SET gone_at = NULL WHERE id = ?", FILE_ID)
    archive.mark_messages_gone(CHANNEL, ["100.000100"], NOW)
    assert run_sql(archive, "SELECT * FROM messages_flat WHERE media_type IS NOT NULL") == []


@pytest.mark.parametrize(
    ("mimetype", "expected"),
    [
        ("image/png", "image"),
        ("application/pdf", "document"),
        (None, "document"),
        ("video/mp4", "other"),
        ("audio/mpeg", "other"),
    ],
)
def test_the_view_classifies_media_for_the_indexer(archive: Archive, mimetype: str | None, expected: str) -> None:
    """'document' marks the standalone-indexable bytes. A NULL mimetype lands there too:
    LIKE against NULL is never true, so unlabelled files fall through to the ELSE arm —
    the right call, since unknown bytes are more likely a document than media."""
    put_conversation(archive)
    put_message(archive, ts="100.000100")
    put_file(archive, mimetype=mimetype)
    archive.link_file(CHANNEL, "100.000100", FILE_ID)
    archive.set_local_path(FILE_ID, str(archive.media_dir / FILE_ID / "plan.pdf"), NOW)
    rows = run_sql(archive, "SELECT media_type FROM messages_flat WHERE media_type IS NOT NULL")
    assert [str(row["media_type"]) for row in rows] == [expected]


def test_a_file_shared_into_two_channels_gets_two_distinct_rows(archive: Archive) -> None:
    """msg_id is the indexer's identity; a re-share must not collide with the first share."""
    put_conversation(archive, conversation_id=CHANNEL, name="#general")
    put_conversation(archive, conversation_id=OTHER_CHANNEL, name="#random")
    put_message(archive, ts="100.000100")
    put_message(archive, channel_id=OTHER_CHANNEL, ts="200.000100")
    put_file(archive)
    archive.set_local_path(FILE_ID, str(archive.media_dir / FILE_ID / "plan.pdf"), NOW)
    archive.link_file(CHANNEL, "100.000100", FILE_ID)
    archive.link_file(OTHER_CHANNEL, "200.000100", FILE_ID)
    rows = run_sql(archive, "SELECT msg_id FROM messages_flat WHERE media_type IS NOT NULL")
    assert {str(row["msg_id"]) for row in rows} == {
        f"{CHANNEL}:100.000100:{FILE_ID}",
        f"{OTHER_CHANNEL}:200.000100:{FILE_ID}",
    }


# -- a read-only consumer discovers sync is not their command --------------------


def _read_only(path: Path) -> None:
    path.chmod(0o500)


def test_sync_lock_in_an_unwritable_directory_explains_instead_of_tracing(tmp_path: Path) -> None:
    """The lock file is a sync run's first write, so it is where a read-only
    consumer of a shared archive lands — the error must speak the tool's own
    words: which side runs sync, and which commands read instead."""
    shared = tmp_path / "shared"
    shared.mkdir()
    _read_only(shared)
    try:
        with pytest.raises(ScrollbackError) as caught, sync_lock(shared):
            pass
    finally:
        shared.chmod(0o700)
    message = str(caught.value)
    assert "cannot write to the archive directory" in message
    assert "owner runs sync" in message
    assert "--archive" in message or "search" in message


def test_open_rw_in_an_uncreatable_directory_explains_instead_of_tracing(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    _read_only(parent)
    try:
        with pytest.raises(ScrollbackError) as caught:
            Archive.open_rw(parent / "archive")
    finally:
        parent.chmod(0o700)
    assert "cannot write to the archive directory" in str(caught.value)


# -- schema migration -------------------------------------------------------------


def test_a_v1_archive_upgrades_in_place_with_data_intact(tmp_path: Path) -> None:
    """The field has v1 archives; opening one must add the sweep columns and
    bump both version stamps without touching a single stored row."""
    from slack_scrollback.archive import MIGRATIONS

    con = sqlite3.connect(tmp_path / "archive.db", isolation_level=None)
    con.executescript("BEGIN;\n" + MIGRATIONS[0] + "\nCOMMIT;")
    con.execute(
        "INSERT INTO messages (channel_id, ts, ts_epoch, sender_name, text, raw, first_seen_at) "
        "VALUES ('C0EXAMPLE1', '100.000001', 100.000001, 'alice', 'kept', '{}', 1.0)"
    )
    con.execute("INSERT INTO sync_state (channel_id, last_ts) VALUES ('C0EXAMPLE1', '100.000001')")
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('team_url', 'https://acme.slack.com')")
    con.close()

    archive = Archive.open_rw(tmp_path)
    assert run_sql(archive, "PRAGMA user_version")[0][0] == SCHEMA_VERSION
    assert archive.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert archive.get_meta("team_url") == "https://acme.slack.com"
    assert archive.last_ts("C0EXAMPLE1") == "100.000001"
    assert [str(r["text"]) for r in run_sql(archive, "SELECT text FROM messages")] == ["kept"]
    assert archive.sweep_state("C0EXAMPLE1") == (None, None)


def test_exclusive_top_excludes_the_boundary_row(tmp_path: Path) -> None:
    """latest_inclusive=False is the sweep's contract: the row AT the bound
    belongs to the previous slice and must not be expected of this one."""
    archive = Archive.open_rw(tmp_path)
    for ts in ("100.000001", "200.000001", "300.000001"):
        put_message(archive, ts=ts)
    inclusive = archive.channel_level_ts_between("C0EXAMPLE1", 100.0, 200.000001)
    exclusive = archive.channel_level_ts_between("C0EXAMPLE1", 100.0, 200.000001, latest_inclusive=False)
    assert inclusive == {"100.000001", "200.000001"}
    assert exclusive == {"100.000001"}
