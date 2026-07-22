"""``sync``: what one run archives, and what the next run repairs.

Every test drives a real :class:`Syncer` against a real SQLite archive in a
temporary directory, through the mutable :class:`FakeSlack` workspace: set the
workspace up, sync, mutate it, sync again. The incremental machinery — the
recheck window, thread re-asks, soft deletes, download healing — is exercised
exactly the way production runs it, and assertions read the committed rows.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from slack_scrollback.api import HttpResponse
from slack_scrollback.errors import ScrollbackError
from slack_scrollback.format import format_timestamp
from slack_scrollback.syncer import ConversationSummary, SyncReport, render_sync_report
from tests.conftest import (
    NOW,
    FakeFileHost,
    FakeSlack,
    capped_handlers,
    channel,
    file_body,
    message,
    ok,
    run_sync,
    slack_file,
    thread_parent,
    thread_reply,
    ts_at,
)

CHANNEL = "C0EXAMPLE1"
OTHER_CHANNEL = "C0EXAMPLE2"

DAY = 86400.0


def rows(archive: Any, sql: str, *params: object) -> list[Any]:
    """Rows straight from the archive's own connection: tests assert on what was committed."""
    return list(archive._con.execute(sql, params))


def stored_message(archive: Any, ts: str, channel_id: str = CHANNEL) -> Any:
    """The raw message row, soft-deleted or not — ``gone_at`` is part of what tests inspect."""
    found = rows(archive, "SELECT * FROM messages WHERE channel_id = ? AND ts = ?", channel_id, ts)
    return found[0] if found else None


def summary_of(report: Any, name: str = "#general") -> Any:
    """One conversation's line of the report."""
    return next(c for c in report.conversations if c.name == name)


def history_oldest(transport: Any) -> str:
    """The ``oldest`` bound the run sent when asking for the channel's history."""
    call = next(c for c in transport.calls if c.method == "conversations.history")
    return str(call.params.get("oldest"))


# -- the first run -----------------------------------------------------------


def test_a_first_run_archives_messages_conversations_users_and_meta(tmp_path: Path) -> None:
    """Everything one run learned lands in the store, including the workspace's own identity."""
    ts = ts_at(29 * DAY)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts, "hello world")]

    report, archive, _ = run_sync(fake, tmp_path)

    row = stored_message(archive, ts)
    assert row is not None
    assert row["text"] == "hello world"
    assert row["sender_name"] == "alice"
    assert row["user_id"] == "U0EXAMPLE1"
    assert row["gone_at"] is None

    conversation = rows(archive, "SELECT * FROM conversations WHERE id = ?", CHANNEL)[0]
    assert (conversation["name"], conversation["kind"], conversation["is_member"]) == ("#general", "public", 1)

    assert archive.user_names() == {"U0EXAMPLE1": "alice"}
    assert archive.get_meta("team_id") == "T0EXAMPLE1"
    assert archive.get_meta("team_url") == "https://acme.slack.com"  # the trailing slash is dropped
    assert archive.get_meta("created_at") == f"{NOW:.6f}"
    assert archive.get_meta("last_sync_at") == f"{NOW:.6f}"
    assert report.archive_path == str(tmp_path)


def test_the_cursor_tracks_the_newest_channel_level_ts_and_replies_count_as_new(tmp_path: Path) -> None:
    """A reply newer than every channel-level message must not advance the cursor.

    ``last_ts`` seeds the next run's history window, and replies never appear
    in a history response — a cursor pushed past the channel-level frontier
    would open a permanent gap. The reply still counts in the report: it is a
    new message by any reader's measure.
    """
    parent_ts = ts_at(20 * DAY)
    newest_channel_ts = ts_at(29 * DAY)
    reply_ts = ts_at(29 * DAY + 60)  # the newest message overall, but a reply
    fake = FakeSlack()
    fake.messages[CHANNEL] = [
        message(newest_channel_ts, "latest channel-level word"),
        thread_parent(parent_ts, reply_count=1, latest_reply=reply_ts),
    ]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, reply_ts)]

    report, archive, _ = run_sync(fake, tmp_path)

    assert archive.last_ts(CHANNEL) == newest_channel_ts
    assert summary_of(report).new == 3


def test_a_non_member_channel_is_recorded_but_never_fetched(tmp_path: Path) -> None:
    """Membership is the access boundary: the roster keeps the channel, the sync never asks it for history."""
    fake = FakeSlack(channels=[channel(), channel(OTHER_CHANNEL, "random", is_member=False)])
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]

    _, archive, transport = run_sync(fake, tmp_path)

    listed = rows(archive, "SELECT * FROM conversations WHERE id = ?", OTHER_CHANNEL)[0]
    assert listed["is_member"] == 0
    asked = [c.params.get("channel") for c in transport.calls if c.method == "conversations.history"]
    assert asked == [CHANNEL]


def test_a_quiet_second_run_changes_nothing_and_resolves_no_names(tmp_path: Path) -> None:
    """Names seeded from the archive make an unchanged workspace cost zero ``users.info`` calls."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY), "ping <@U0EXAMPLE2>")]
    _, _, first_transport = run_sync(fake, tmp_path)
    assert "users.info" in first_transport.methods

    report, _, transport = run_sync(fake, tmp_path)

    assert all(c.new == c.edited == c.gone == 0 for c in report.conversations)
    assert "users.info" not in transport.methods


def test_stored_text_is_rendered_with_mentions_resolved_and_file_markers(tmp_path: Path) -> None:
    """The archive reads like live output: rendered at sync time, so indexers search names, not IDs."""
    ts = ts_at(29 * DAY)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts, "ping <@U0EXAMPLE2>", files=[slack_file()])]

    _, archive, _ = run_sync(fake, tmp_path)

    assert stored_message(archive, ts)["text"] == "ping @bob [file: plan.pdf]"


def test_a_message_with_no_text_and_no_files_stores_empty_text(tmp_path: Path) -> None:
    """The renderer's "(no text)" stand-in must stay out of the store, or indexers would index the prop."""
    ts = ts_at(29 * DAY)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts, "")]

    _, archive, _ = run_sync(fake, tmp_path)

    assert stored_message(archive, ts)["text"] == ""


# -- the incremental window ----------------------------------------------------


def test_a_fresh_cursor_still_rereads_the_whole_recheck_window(tmp_path: Path) -> None:
    """oldest = min(cursor, now - recheck): with yesterday's cursor, the seven-day recheck bound wins.

    Resuming exactly at the cursor would never re-read the recent past, and
    edits and deletions only reveal themselves on a re-read.
    """
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]
    run_sync(fake, tmp_path)

    _, _, transport = run_sync(fake, tmp_path)

    assert history_oldest(transport) == f"{NOW - 7 * DAY:.6f}"


def test_a_lagging_cursor_wins_over_the_recheck_cutoff(tmp_path: Path) -> None:
    """A channel silent for a month resumes from its cursor, not from now - recheck.

    Starting at the recheck bound unconditionally would leave a gap between
    the last archived message and the window — anything said there lost until
    the next --full.
    """
    old_ts = ts_at(0)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(old_ts)]
    run_sync(fake, tmp_path)

    _, _, transport = run_sync(fake, tmp_path)

    assert history_oldest(transport) == old_ts


def test_an_edit_inside_the_window_updates_text_and_edited_ts(tmp_path: Path) -> None:
    """An edit only shows up on the re-read: the changed ``edited.ts`` must reach the row and the report."""
    ts = ts_at(29 * DAY)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts, "first draft")]
    run_sync(fake, tmp_path)

    stamp = ts_at(29 * DAY + 300)
    fake.messages[CHANNEL] = [message(ts, "second draft", edited={"ts": stamp})]
    report, archive, _ = run_sync(fake, tmp_path)

    row = stored_message(archive, ts)
    assert row["text"] == "second draft"
    assert row["edited_ts"] == stamp
    assert summary_of(report).edited == 1
    assert summary_of(report).new == 0


def test_a_deletion_inside_the_window_is_marked_gone(tmp_path: Path) -> None:
    """A held row absent above the oldest served message is Slack saying it was deleted.

    The surviving message is deliberately the *older* of the two: it anchors
    the evidence — Slack demonstrably serves history from below the deleted
    message, so its absence cannot be retention hiding.
    """
    kept = message(ts_at(28 * DAY), "kept")
    dropped = message(ts_at(29 * DAY), "dropped")
    fake = FakeSlack()
    fake.messages[CHANNEL] = [kept, dropped]
    run_sync(fake, tmp_path)

    fake.messages[CHANNEL] = [kept]
    report, archive, _ = run_sync(fake, tmp_path)

    assert stored_message(archive, str(dropped["ts"]))["gone_at"] is not None
    assert stored_message(archive, str(kept["ts"]))["gone_at"] is None
    assert summary_of(report).gone == 1


def test_a_deletion_outside_the_window_is_not_evidence(tmp_path: Path) -> None:
    """The run never asked about the old stretch, so absence there proves nothing and the row stays."""
    ancient = message(ts_at(0), "from before the window")
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY)), ancient]
    run_sync(fake, tmp_path)

    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]
    report, archive, _ = run_sync(fake, tmp_path)

    assert stored_message(archive, str(ancient["ts"]))["gone_at"] is None
    assert summary_of(report).gone == 0


def test_a_gone_message_served_again_is_unmarked(tmp_path: Path) -> None:
    """``gone_at`` records an inference; a message Slack serves again was never deleted, so the mark is retracted."""
    anchor = message(ts_at(28 * DAY), "still here")
    ts = ts_at(29 * DAY)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [anchor, message(ts, "now you see me")]
    run_sync(fake, tmp_path)

    fake.messages[CHANNEL] = [anchor]
    _, archive, _ = run_sync(fake, tmp_path)
    assert stored_message(archive, ts)["gone_at"] is not None

    fake.messages[CHANNEL] = [anchor, message(ts, "now you see me")]
    _, archive, _ = run_sync(fake, tmp_path)
    assert stored_message(archive, ts)["gone_at"] is None


# -- threads --------------------------------------------------------------------


def test_thread_replies_are_stored_flat_under_the_parents_ts(tmp_path: Path) -> None:
    """Replies are rows like any other, tied to their thread by ``thread_ts`` — the only nesting Slack has."""
    parent_ts = ts_at(27 * DAY)
    first, second = ts_at(27 * DAY + 60), ts_at(27 * DAY + 120)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [thread_parent(parent_ts, reply_count=2, latest_reply=second)]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, first), thread_reply(parent_ts, second)]

    _, archive, _ = run_sync(fake, tmp_path)

    for ts in (first, second):
        row = stored_message(archive, ts)
        assert row["thread_ts"] == parent_ts
        assert row["gone_at"] is None
    assert stored_message(archive, parent_ts)["thread_ts"] == parent_ts


def test_a_new_reply_is_fetched_when_the_parents_counters_move(tmp_path: Path) -> None:
    """History never carries replies; the parent's moved ``reply_count``/``latest_reply`` is the only tell."""
    parent_ts = ts_at(28 * DAY)
    first = ts_at(28 * DAY + 60)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [thread_parent(parent_ts, reply_count=1, latest_reply=first)]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, first)]
    run_sync(fake, tmp_path)

    late = ts_at(29 * DAY)
    fake.messages[CHANNEL] = [thread_parent(parent_ts, reply_count=2, latest_reply=late)]
    fake.threads[(CHANNEL, parent_ts)].append(thread_reply(parent_ts, late, "late addition"))
    report, archive, _ = run_sync(fake, tmp_path)

    assert stored_message(archive, late)["thread_ts"] == parent_ts
    assert summary_of(report).new == 1


def test_a_deleted_reply_is_marked_gone(tmp_path: Path) -> None:
    """A replies response is a complete read of one thread, so a stored reply absent from it is gone."""
    parent_ts = ts_at(28 * DAY)
    kept, dropped = ts_at(28 * DAY + 60), ts_at(28 * DAY + 120)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [thread_parent(parent_ts, reply_count=2, latest_reply=dropped)]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, kept), thread_reply(parent_ts, dropped)]
    run_sync(fake, tmp_path)

    fake.messages[CHANNEL] = [thread_parent(parent_ts, reply_count=1, latest_reply=kept)]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, kept)]
    report, archive, _ = run_sync(fake, tmp_path)

    assert stored_message(archive, dropped)["gone_at"] is not None
    assert stored_message(archive, kept)["gone_at"] is None
    assert summary_of(report).gone == 1


def test_reply_count_dropping_to_zero_marks_every_stored_reply_gone(tmp_path: Path) -> None:
    """A parent whose ``reply_count`` reads zero has lost its whole tail, and the archive must say so."""
    parent_ts = ts_at(28 * DAY)
    first, second = ts_at(28 * DAY + 60), ts_at(28 * DAY + 120)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [thread_parent(parent_ts, reply_count=2, latest_reply=second)]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, first), thread_reply(parent_ts, second)]
    run_sync(fake, tmp_path)

    fake.messages[CHANNEL] = [thread_parent(parent_ts, reply_count=0, latest_reply="")]
    fake.threads[(CHANNEL, parent_ts)] = []
    report, archive, _ = run_sync(fake, tmp_path)

    assert stored_message(archive, first)["gone_at"] is not None
    assert stored_message(archive, second)["gone_at"] is not None
    assert stored_message(archive, parent_ts)["gone_at"] is None
    assert summary_of(report).gone == 2


def test_a_deleted_parent_takes_its_replies_with_it(tmp_path: Path) -> None:
    """``thread_not_found`` is Slack's word that the parent is gone, and a thread cannot outlive its parent."""
    anchor = message(ts_at(27 * DAY), "older than the doomed thread")
    parent_ts = ts_at(28 * DAY)
    first, second = ts_at(28 * DAY + 60), ts_at(28 * DAY + 120)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [anchor, thread_parent(parent_ts, reply_count=2, latest_reply=second)]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, first), thread_reply(parent_ts, second)]
    run_sync(fake, tmp_path)

    fake.messages[CHANNEL] = [anchor]
    del fake.threads[(CHANNEL, parent_ts)]
    report, archive, _ = run_sync(fake, tmp_path)

    for ts in (parent_ts, first, second):
        assert stored_message(archive, ts)["gone_at"] is not None
    assert summary_of(report).gone >= 3


def test_a_reply_to_an_old_thread_is_found_through_stored_activity(tmp_path: Path) -> None:
    """A windowed history response carries no trace of a reply to an old parent.

    The parent sits outside the window, so its counters are never seen; the
    archive's own record of recent activity in that thread is what earns the
    ``conversations.replies`` call that finds the new reply.
    """
    parent_ts = ts_at(0)  # a month old: far outside the seven-day recheck window
    recent_reply = ts_at(25 * DAY)  # stored thread activity inside the window
    fake = FakeSlack()
    fake.messages[CHANNEL] = [
        thread_parent(parent_ts, reply_count=1, latest_reply=recent_reply),
        message(ts_at(29 * DAY), "keeps the cursor fresh"),
    ]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, recent_reply)]
    run_sync(fake, tmp_path)

    revival = ts_at(29 * DAY + 120)
    fake.threads[(CHANNEL, parent_ts)].append(thread_reply(parent_ts, revival, "thread revival"))
    report, archive, _ = run_sync(fake, tmp_path)

    assert stored_message(archive, revival)["thread_ts"] == parent_ts
    assert stored_message(archive, parent_ts)["gone_at"] is None
    assert summary_of(report).new == 1


# -- files and media --------------------------------------------------------------


def test_two_files_on_one_message_make_two_file_rows_and_two_links(tmp_path: Path) -> None:
    """Files and their sightings are separate facts: one row per file, one junction row per share."""
    ts = ts_at(29 * DAY)
    plan = slack_file("F0EXAMPLE1", "plan.pdf")
    notes = slack_file("F0EXAMPLE2", "notes.txt", mimetype="text/plain")
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts, "two attachments", files=[plan, notes])]

    _, archive, _ = run_sync(fake, tmp_path)

    assert {r["id"] for r in rows(archive, "SELECT id FROM files")} == {"F0EXAMPLE1", "F0EXAMPLE2"}
    links = rows(archive, "SELECT * FROM message_files WHERE channel_id = ? AND ts = ?", CHANNEL, ts)
    assert {r["file_id"] for r in links} == {"F0EXAMPLE1", "F0EXAMPLE2"}


def test_a_reshared_file_keeps_one_row_two_links_and_one_download(tmp_path: Path) -> None:
    """Re-sharing into a second channel is a new sighting of the same bytes, never a second fetch."""
    shared = slack_file("F0EXAMPLE1", "plan.pdf", size=16)
    url = str(shared["url_private"])
    fake = FakeSlack(channels=[channel(), channel(OTHER_CHANNEL, "random")])
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY), "have a look", files=[shared])]
    fake.messages[OTHER_CHANNEL] = [message(ts_at(29 * DAY + 60), "sharing here too", files=[shared])]
    host = FakeFileHost(responses={url: file_body(b"%PDF-1.4".ljust(16, b"."))})

    report, archive, _ = run_sync(fake, tmp_path, media_tiers=frozenset({"documents"}), downloads=host)

    assert len(rows(archive, "SELECT id FROM files")) == 1
    assert len(rows(archive, "SELECT * FROM message_files WHERE file_id = 'F0EXAMPLE1'")) == 2
    assert [c.url for c in host.calls] == [url]
    assert report.files_downloaded == 1


def test_external_and_tombstone_files_keep_metadata_but_never_download(tmp_path: Path) -> None:
    """External files have no Slack-hosted bytes and tombstones are deletion stubs — neither is queued."""
    external = slack_file(
        "F0EXAMPLE3",
        "shared doc",
        mimetype="application/vnd.google-apps.document",
        mode="external",
        url_private="https://docs.google.com/document/d/e2Xample/edit",
    )
    stub = slack_file("F0EXAMPLE4", "expired.pdf", mode="tombstone")
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY), "links", files=[external, stub])]
    host = FakeFileHost()  # no stubbed URLs: any request at all fails the test loudly

    report, archive, _ = run_sync(fake, tmp_path, media_tiers=frozenset({"documents", "images"}), downloads=host)

    assert host.calls == []
    assert report.files_downloaded == 0
    external_row = rows(archive, "SELECT * FROM files WHERE id = 'F0EXAMPLE3'")[0]
    assert (external_row["mode"], external_row["name"]) == ("external", "shared doc")
    assert external_row["local_path"] is None
    stub_row = rows(archive, "SELECT * FROM files WHERE id = 'F0EXAMPLE4'")[0]
    assert stub_row["mode"] == "tombstone"
    assert stub_row["gone_at"] is not None


def test_a_download_lands_under_media_and_fills_local_path(tmp_path: Path) -> None:
    """Bytes are archived beside the database, under ``media/<id>/<name>``, and the row records where."""
    payload = b"%PDF-1.4 synthetic example content".ljust(64, b".")
    doc = slack_file("F0EXAMPLE1", "plan.pdf", size=64)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY), "the plan", files=[doc])]
    host = FakeFileHost(responses={str(doc["url_private"]): file_body(payload)})

    report, archive, _ = run_sync(fake, tmp_path, media_tiers=frozenset({"documents"}), downloads=host)

    dest = tmp_path / "media" / "F0EXAMPLE1" / "plan.pdf"
    assert dest.read_bytes() == payload
    row = rows(archive, "SELECT * FROM files WHERE id = 'F0EXAMPLE1'")[0]
    assert row["local_path"] == str(dest)
    assert row["downloaded_at"] is not None
    assert report.files_downloaded == 1
    assert report.bytes_downloaded == 64


def test_a_failed_download_is_reported_committed_around_and_retried(tmp_path: Path) -> None:
    """One bad file costs exactly that file: the run still commits, and the next run asks again."""
    doc = slack_file("F0EXAMPLE1", "plan.pdf", size=64)
    url = str(doc["url_private"])
    ts = ts_at(29 * DAY)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts, "the plan", files=[doc])]
    host = FakeFileHost(responses={url: file_body(b"far too short")})

    report, archive, _ = run_sync(fake, tmp_path, media_tiers=frozenset({"documents"}), downloads=host)

    assert len(report.download_failures) == 1
    assert "plan.pdf" in report.download_failures[0]
    assert rows(archive, "SELECT local_path FROM files WHERE id = 'F0EXAMPLE1'")[0]["local_path"] is None
    assert stored_message(archive, ts) is not None  # the failure took nothing else with it
    assert archive.get_meta("last_sync_at") == f"{NOW:.6f}"

    run_sync(fake, tmp_path, media_tiers=frozenset({"documents"}), downloads=host)
    assert [c.url for c in host.calls] == [url, url]


def test_media_tiers_choose_which_files_are_downloaded(tmp_path: Path) -> None:
    """Tier filtering happens per file at queue time: images-only leaves the PDF as metadata."""
    pdf = slack_file("F0EXAMPLE1", "plan.pdf", size=8)
    png = slack_file("F0EXAMPLE2", "chart.png", mimetype="image/png", size=8)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY), "both kinds", files=[pdf, png])]
    host = FakeFileHost(responses={str(png["url_private"]): file_body(b"\x89PNG\r\n\x1a\n", "image/png")})

    report, archive, _ = run_sync(fake, tmp_path, media_tiers=frozenset({"images"}), downloads=host)

    assert [c.url for c in host.calls] == [str(png["url_private"])]
    assert rows(archive, "SELECT local_path FROM files WHERE id = 'F0EXAMPLE1'")[0]["local_path"] is None
    assert rows(archive, "SELECT local_path FROM files WHERE id = 'F0EXAMPLE2'")[0]["local_path"] is not None
    assert report.files_downloaded == 1


def test_the_size_cap_skips_files_larger_than_allowed(tmp_path: Path) -> None:
    """The cap is a promise about disk and time, so an oversized file is not even requested."""
    doc = slack_file("F0EXAMPLE1", "plan.pdf", size=64)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY), "too big", files=[doc])]
    host = FakeFileHost(responses={str(doc["url_private"]): file_body(b"." * 64)})

    report, _, _ = run_sync(fake, tmp_path, media_tiers=frozenset({"documents"}), media_max_bytes=16, downloads=host)

    assert host.calls == []
    assert report.files_downloaded == 0


def test_no_tiers_means_no_download_requests_at_all(tmp_path: Path) -> None:
    """Downloads disabled still records metadata — and touches no file host whatsoever."""
    doc = slack_file("F0EXAMPLE1", "plan.pdf", size=8)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY), "metadata only", files=[doc])]
    host = FakeFileHost(responses={str(doc["url_private"]): file_body(b"%PDF-1.4")})

    _, archive, _ = run_sync(fake, tmp_path, media_tiers=frozenset(), downloads=host)

    assert host.calls == []
    assert len(rows(archive, "SELECT id FROM files")) == 1


# -- atomicity ---------------------------------------------------------------------


class _UndecodableSecondChannel(FakeSlack):
    """A workspace whose second channel's history dies mid-run with undecodable bytes."""

    def _history(self, params: dict[str, str]) -> Any:
        if params.get("channel") == OTHER_CHANNEL:
            return HttpResponse(status=200, headers={}, body=b"not json")
        return super()._history(params)


def test_a_run_that_dies_midway_leaves_the_previous_runs_state(tmp_path: Path) -> None:
    """One run is one transaction: a crash after real upserts leaves the archive exactly as last committed."""
    both = [channel(), channel(OTHER_CHANNEL, "random")]
    settled = message(ts_at(28 * DAY), "settled history")
    fake = FakeSlack(channels=both, messages={CHANNEL: [settled], OTHER_CHANNEL: []})
    _, archive, _ = run_sync(fake, tmp_path)

    fresh_ts = ts_at(29 * DAY)
    broken = _UndecodableSecondChannel(
        channels=both,
        messages={CHANNEL: [settled, message(fresh_ts, "arrived mid-crash")], OTHER_CHANNEL: []},
    )
    with pytest.raises(ScrollbackError):
        run_sync(broken, tmp_path, now=NOW + 3600)

    # #general sorts before #random, so its new message was upserted before the
    # crash — and must still be invisible, every cursor where run one left it.
    assert stored_message(archive, fresh_ts) is None
    assert archive.get_meta("last_sync_at") == f"{NOW:.6f}"
    assert archive.last_ts(CHANNEL) == str(settled["ts"])
    state = rows(archive, "SELECT * FROM sync_state WHERE channel_id = ?", CHANNEL)[0]
    assert state["last_run_at"] == NOW


# -- throttling ----------------------------------------------------------------------


def test_a_silently_capped_page_is_reported_as_throttling(tmp_path: Path) -> None:
    """Sync asks for 1000; fifteen-with-more can only be Slack's cap, and the report must say so."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY + i)) for i in range(16)]
    report, _, _ = run_sync(fake, tmp_path, handlers=capped_handlers(fake))

    assert report.throttled is True
    assert any("capped" in note for note in report.notes)


# -- --full ------------------------------------------------------------------------


def test_a_full_run_rerenders_sender_names_an_incremental_run_leaves_stale(tmp_path: Path) -> None:
    """A rename reaches old rows only through --full's re-render; that is the schema's documented bargain.

    Incremental runs seed names from the archive precisely so an unchanged
    workspace costs no ``users.info`` calls — which means they cannot notice a
    rename. ``--full`` deliberately seeds nothing and re-reads everything, so
    stored rows are where the new name catches up.
    """
    ts = ts_at(29 * DAY)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts, "hello")]
    run_sync(fake, tmp_path)

    fake.users["U0EXAMPLE1"] = "alexandra"
    _, archive, _ = run_sync(fake, tmp_path)
    assert stored_message(archive, ts)["sender_name"] == "alice"  # stale, and rightly so

    _, archive, _ = run_sync(fake, tmp_path, full=True)
    assert archive.user_names()["U0EXAMPLE1"] == "alexandra"
    assert stored_message(archive, ts)["sender_name"] == "alexandra"


def test_a_dropped_conversation_is_marked_gone_on_any_run(tmp_path: Path) -> None:
    """Reconciling the roster is an every-run repair now: the listing is
    fetched every run anyway, gone_at is soft, and a re-listed conversation
    un-marks itself — so there is nothing to save for --full."""
    dropped = channel(OTHER_CHANNEL, "random")
    fake = FakeSlack(channels=[channel(), dropped])
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]
    run_sync(fake, tmp_path)

    fake.channels = [channel()]
    report, archive, _ = run_sync(fake, tmp_path)
    row = rows(archive, "SELECT * FROM conversations WHERE id = ?", OTHER_CHANNEL)[0]
    assert row["gone_at"] is not None
    assert any("no longer visible" in note for note in report.notes)

    fake.channels = [channel(), dropped]
    _, archive, _ = run_sync(fake, tmp_path)
    row = rows(archive, "SELECT * FROM conversations WHERE id = ?", OTHER_CHANNEL)[0]
    assert row["gone_at"] is None


def test_a_full_run_stamps_last_full_at(tmp_path: Path) -> None:
    """``last_full_at`` records when deep repairs last ran; incremental runs must not touch it."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]
    _, archive, _ = run_sync(fake, tmp_path)
    state = rows(archive, "SELECT last_full_at FROM sync_state WHERE channel_id = ?", CHANNEL)[0]
    assert state["last_full_at"] is None

    _, archive, _ = run_sync(fake, tmp_path, full=True, now=NOW + 3600)
    state = rows(archive, "SELECT last_full_at FROM sync_state WHERE channel_id = ?", CHANNEL)[0]
    assert state["last_full_at"] == NOW + 3600


# -- report rendering ------------------------------------------------------------------


def _busy_report() -> SyncReport:
    """A report with one changed and one quiet conversation, one failure, one note."""
    return SyncReport(
        archive_path="/somewhere/archive",
        conversations=[ConversationSummary(name="#general", new=2, edited=1), ConversationSummary(name="#random")],
        download_failures=["download of plan.pdf (F0EXAMPLE1) failed"],
        duration_seconds=1.23,
        synced_at=NOW,
        notes=["the recheck window was noisy"],
    )


def test_the_text_report_speaks_only_of_changed_conversations() -> None:
    """A quiet workspace makes a quiet report — while the summary line still states every total out loud."""
    lines = render_sync_report(_busy_report())

    assert "#general: 2 new, 1 edited, 0 gone" in lines
    assert not any(line.startswith("#random") for line in lines)
    assert "download failed: download of plan.pdf (F0EXAMPLE1) failed" in lines
    assert "synced 2 conversations (1 changed) in 1.2s — archive: /somewhere/archive" in lines
    assert "[note: the recheck window was noisy]" in lines


def test_the_json_report_is_line_parseable_records_with_honest_totals() -> None:
    """Every line is one JSON record with a ``type`` discriminator, so notes cannot be mistaken for data."""
    lines = render_sync_report(_busy_report(), as_json=True)

    records = [json.loads(line) for line in lines]
    assert [r["type"] for r in records] == ["conversation", "download_failure", "summary", "notice"]
    assert records[0] == {"type": "conversation", "name": "#general", "new": 2, "edited": 1, "gone": 0}
    assert records[1] == {"type": "download_failure", "text": "download of plan.pdf (F0EXAMPLE1) failed"}
    summary = records[2]
    assert summary["conversations"] == 2
    assert summary["changed"] == 1
    assert summary["new"] == 2
    assert summary["edited"] == 1
    assert summary["gone"] == 0
    assert summary["files_downloaded"] == 0
    assert summary["bytes_downloaded"] == 0
    assert summary["download_failures"] == 1
    assert summary["duration_seconds"] == 1.23
    assert summary["archive"] == "/somewhere/archive"
    assert summary["synced_at"] == format_timestamp(f"{NOW:.6f}")
    assert records[3] == {"type": "notice", "text": "the recheck window was noisy"}


# -- retention vs deletion: absence is only evidence where Slack served history --


def test_an_empty_window_marks_nothing_gone(tmp_path: Path) -> None:
    """A quiet channel whose archived history ages past a retention horizon
    answers with an empty window — exactly like a quiet channel whose history
    was never touched. Nothing distinguishes the two, so nothing is marked;
    the archive's whole point is to outlive retention."""
    old = message(ts_at(1 * DAY), "kept beyond retention")
    fake = FakeSlack()
    fake.messages[CHANNEL] = [old]
    run_sync(fake, tmp_path)

    fake.messages[CHANNEL] = []  # hidden by retention, from the API's view
    report, archive, _ = run_sync(fake, tmp_path)

    assert stored_message(archive, str(old["ts"]))["gone_at"] is None
    assert summary_of(report).gone == 0


def test_absence_below_the_oldest_served_message_is_not_deletion(tmp_path: Path) -> None:
    """A --full sync against a retention-limited workspace serves only the
    recent stretch; everything archived below the oldest served message must
    survive, while a real deletion above that line is still caught."""
    beyond_retention = message(ts_at(1 * DAY), "aged out")
    survivor = message(ts_at(28 * DAY), "still served")
    deleted = message(ts_at(29 * DAY), "really deleted")
    fake = FakeSlack()
    fake.messages[CHANNEL] = [beyond_retention, survivor, deleted]
    run_sync(fake, tmp_path, full=True)

    fake.messages[CHANNEL] = [survivor]  # retention hid the old one; a user deleted the new one
    report, archive, _ = run_sync(fake, tmp_path, full=True)

    assert stored_message(archive, str(beyond_retention["ts"]))["gone_at"] is None
    assert stored_message(archive, str(deleted["ts"]))["gone_at"] is not None
    assert summary_of(report).gone == 1


def test_thread_not_found_below_the_evidence_floor_marks_nothing(tmp_path: Path) -> None:
    """A parent that aged out of retention answers ``thread_not_found`` just
    like a deleted one; without served history reaching down to its ts, the
    thread and its replies stay."""
    parent_ts = ts_at(1 * DAY)
    reply_ts = ts_at(25 * DAY)  # recent stored activity keeps the thread re-checked
    fresh = message(ts_at(29 * DAY), "recent unrelated traffic")
    fake = FakeSlack()
    fake.messages[CHANNEL] = [thread_parent(parent_ts, reply_count=1, latest_reply=reply_ts), fresh]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, reply_ts)]
    run_sync(fake, tmp_path)

    del fake.messages[CHANNEL][0]  # the parent fell out of retention...
    del fake.threads[(CHANNEL, parent_ts)]  # ...so replies now answer thread_not_found
    report, archive, _ = run_sync(fake, tmp_path)

    assert stored_message(archive, parent_ts)["gone_at"] is None
    assert stored_message(archive, reply_ts)["gone_at"] is None
    assert summary_of(report).gone == 0


# -- thread fetch gating: unmoved counters cost nothing --------------------------


def test_unmoved_thread_counters_trigger_no_replies_fetch(tmp_path: Path) -> None:
    """The incremental algorithm exists to avoid re-reading settled threads; a
    parent whose reply_count/latest_reply match the archive must not cost a
    ``conversations.replies`` call on the next run."""
    parent_ts = ts_at(29 * DAY)
    reply_ts = ts_at(29 * DAY + 60)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [thread_parent(parent_ts, reply_count=1, latest_reply=reply_ts)]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, reply_ts)]
    run_sync(fake, tmp_path)

    # The stored-activity recheck legitimately re-asks threads alive inside
    # the window; what must NOT happen is a fetch driven by unmoved counters
    # once the thread has left the window.
    later = NOW + 30 * DAY
    _, _, transport = run_sync(fake, tmp_path, now=later)
    replies_calls = [c for c in transport.calls if c.method == "conversations.replies"]
    assert replies_calls == []


# -- FTS5-less hosts: warn, archive anyway, heal later ----------------------------


def test_sync_without_fts5_warns_and_still_archives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The index is an accelerator, never a requirement: a host whose SQLite
    lacks FTS5 must archive everything and say why search will scan."""
    import slack_scrollback.archive as archive_module

    monkeypatch.setattr(archive_module, "fts5_trigram_available", lambda: False)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY), "kept without an index")]
    report, archive, _ = run_sync(fake, tmp_path)

    assert "this Python's SQLite lacks FTS5; archive search will fall back to a full scan" in report.notes
    assert stored_message(archive, str(fake.messages[CHANNEL][0]["ts"])) is not None


def test_an_indexed_archive_survives_a_sync_on_an_fts5less_host_and_heals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive built on a capable host carries FTS triggers. A lesser host
    must drop them and keep archiving — not crash on the missing module — and
    the next capable host must rebuild the index to cover what was written
    while it was dark."""
    import slack_scrollback.archive as archive_module

    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(28 * DAY), "indexed normally")]
    run_sync(fake, tmp_path)  # capable host: index + triggers exist

    monkeypatch.setattr(archive_module, "fts5_trigram_available", lambda: False)
    fake.messages[CHANNEL].append(message(ts_at(29 * DAY), "written in the dark"))
    report, _, _ = run_sync(fake, tmp_path)  # must not raise
    assert any("lacks FTS5" in note for note in report.notes)

    monkeypatch.undo()
    _, archive, _ = run_sync(fake, tmp_path)
    assert archive.fts_usable
    rows, used_fts = archive.search_candidates("written in the dark", channel_ids=[CHANNEL], oldest=0.0, latest=NOW + 1)
    assert used_fts and len(rows) == 1


# -- report accuracy ----------------------------------------------------------------


def test_gone_counts_each_row_once_even_when_a_thread_teardown_renames_it(tmp_path: Path) -> None:
    """The window diff marks a deleted parent; the thread teardown then names
    it again alongside the replies. One deletion is one unit of news."""
    anchor = message(ts_at(27 * DAY), "evidence anchor")
    parent_ts = ts_at(28 * DAY)
    reply_ts = ts_at(28 * DAY + 60)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [anchor, thread_parent(parent_ts, reply_count=1, latest_reply=reply_ts)]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, reply_ts)]
    run_sync(fake, tmp_path)

    fake.messages[CHANNEL] = [anchor]
    del fake.threads[(CHANNEL, parent_ts)]
    report, _, _ = run_sync(fake, tmp_path)

    assert summary_of(report).gone == 2  # the parent and its reply, each once


def test_the_throttle_note_carries_no_doubled_prefix(tmp_path: Path) -> None:
    """Report notes render as '[note: ...]'; a note stored with its own
    'note: ' prefix would read '[note: note: ...]' — a small tell that nobody
    proofread the output."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY + i), f"m{i}") for i in range(15)]
    report, _, _ = run_sync(fake, tmp_path, handlers=capped_handlers(fake))

    assert report.throttled
    (throttle_note,) = [n for n in report.notes if "capped" in n]
    assert not throttle_note.startswith("note:")
    rendered = render_sync_report(report)
    assert any(line.startswith("[note: Slack capped") for line in rendered)
    assert not any("note: note:" in line for line in rendered)


# -- progress ------------------------------------------------------------------


def test_progress_narrates_conversations_threads_and_downloads(tmp_path: Path) -> None:
    """The callback is the run's only sign of life before the report: it must
    name what is being fetched, position it in the whole, and count downloads."""
    parent_ts = ts_at(29 * DAY)
    reply_ts = ts_at(29 * DAY + 60)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [
        thread_parent(parent_ts, reply_count=1, latest_reply=reply_ts),
        message(ts_at(29 * DAY + 120), "with a file", files=[slack_file()]),
    ]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, reply_ts)]

    ticks: list[str] = []
    downloads = FakeFileHost(responses={str(slack_file()["url_private"]): file_body(b"abcdef")})
    run_sync(fake, tmp_path, media_tiers=frozenset({"documents"}), downloads=downloads, progress=ticks.append)

    assert ticks[0] == "listing conversations"
    assert "#general (1/1)" in ticks
    assert "#general (1/1) — thread 1/1" in ticks
    assert "downloading plan.pdf (1/1)" in ticks


def test_no_progress_callback_means_no_narration(tmp_path: Path) -> None:
    """Silence is the default: schedulers and tests get exactly the report."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]
    report, _, _ = run_sync(fake, tmp_path)  # run_sync passes no progress; reaching here is the assertion
    assert report.conversations


# -- the Slackbot DM -------------------------------------------------------------


def test_the_slackbot_dm_is_rostered_but_never_fetched(tmp_path: Path) -> None:
    """Slack answers channel_not_found for Slackbot DM history, to every bot,
    every time. Fetching it would only manufacture the same note forever, so
    the roster keeps it and the sync passes it by — without a note."""
    slackbot_dm = {"id": "D0EXAMPLE9", "is_im": True, "user": "USLACKBOT"}
    fake = FakeSlack(channels=[channel(), slackbot_dm])
    fake.users["USLACKBOT"] = "Slackbot"
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]

    report, archive, transport = run_sync(fake, tmp_path)

    asked = [c.params.get("channel") for c in transport.calls if c.method == "conversations.history"]
    assert asked == [CHANNEL]
    listed = rows(archive, "SELECT * FROM conversations WHERE id = ?", "D0EXAMPLE9")
    assert len(listed) == 1
    assert report.notes == []
    assert all(c.name != "@Slackbot" for c in report.conversations)


# -- the repair sweep: tiling and boundaries --------------------------------------


def sweep_slice_calls(transport: Any) -> list[str]:
    """The `latest` bound of each sweep slice this run.

    Slice fetches are the history calls WITHOUT `inclusive` — the window
    always sends inclusive=true; the sweep never does, because its tiling
    depends on the exclusive bound.
    """
    return [
        str(c.params["latest"])
        for c in transport.calls
        if c.method == "conversations.history" and "inclusive" not in c.params
    ]


def old_days(*days: int) -> list[dict[str, Any]]:
    return [message(ts_at(d * DAY), f"day {d}") for d in days]


def test_sweep_slices_tile_history_without_gap_overlap_or_false_gone(tmp_path: Path) -> None:
    """One lap in three slices: each starts exactly where the last ended
    (exclusive bound), every old message is re-verified exactly once, and —
    the boundary case — the previous slice's oldest message is never
    falsely gone-marked by the next slice's deletion diff."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [*old_days(1, 2, 3, 4, 5, 6, 7), message(ts_at(29 * DAY), "recent")]
    day = {d: ts_at(d * DAY) for d in range(1, 8)}
    lap_start = f"{NOW - 7 * DAY:.6f}"

    _, archive, transport = run_sync(fake, tmp_path, sweep_pages=1, sweep_page_size=3)
    assert sweep_slice_calls(transport) == [lap_start]
    assert archive.sweep_state(CHANNEL) == (day[5], None)

    _, archive, transport = run_sync(fake, tmp_path, sweep_pages=1, sweep_page_size=3)
    assert sweep_slice_calls(transport) == [day[5]]
    assert archive.sweep_state(CHANNEL) == (day[2], None)

    report, archive, transport = run_sync(fake, tmp_path, sweep_pages=1, sweep_page_size=3)
    assert sweep_slice_calls(transport) == [day[2]]
    before, lap_at = archive.sweep_state(CHANNEL)
    assert before is None and lap_at == NOW
    assert report.sweep_lap_completed

    gone = rows(archive, "SELECT ts FROM messages WHERE gone_at IS NOT NULL")
    assert gone == []

    # The next run wraps: a fresh lap starts back at the recheck boundary.
    _, _, transport = run_sync(fake, tmp_path, sweep_pages=1, sweep_page_size=3)
    assert sweep_slice_calls(transport) == [lap_start]


def test_deep_edits_and_deletions_land_on_the_lap_that_reaches_them(tmp_path: Path) -> None:
    """The bounded-staleness promise, concretely: an old edit lands when its
    slice is served, an old deletion is marked when its slice's evidence
    covers it — and neither happens a run earlier."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [*old_days(1, 2, 3, 4, 5, 6, 7), message(ts_at(29 * DAY), "recent")]
    day = {d: ts_at(d * DAY) for d in range(1, 8)}
    for _ in range(3):  # complete one clean lap
        run_sync(fake, tmp_path, sweep_pages=1, sweep_page_size=3)

    fake.messages[CHANNEL] = [
        m
        for m in fake.messages[CHANNEL]
        if str(m["ts"]) != day[3]  # deleted deep in history
    ]
    for m in fake.messages[CHANNEL]:
        if str(m["ts"]) == day[6]:
            m["text"] = "day 6, corrected"
            m["edited"] = {"ts": ts_at(29 * DAY + 60)}

    report, archive, _ = run_sync(fake, tmp_path, sweep_pages=1, sweep_page_size=3)
    assert stored_message(archive, day[6])["text"] == "day 6, corrected"
    assert summary_of(report).edited == 1
    assert stored_message(archive, day[3])["gone_at"] is None  # slice has not reached it yet

    report, archive, _ = run_sync(fake, tmp_path, sweep_pages=1, sweep_page_size=3)
    assert stored_message(archive, day[3])["gone_at"] is not None
    assert summary_of(report).gone == 1
    assert stored_message(archive, day[2])["gone_at"] is None
    assert stored_message(archive, day[4])["gone_at"] is None


# -- the repair sweep: revived threads and the fixed budget ------------------------


def test_a_revived_thread_is_caught_on_the_run_whose_slice_serves_its_parent(tmp_path: Path) -> None:
    """A reply to a thread silent longer than the recheck window leaves no
    trace in any windowed response; the sweep re-serving the parent is the
    designed catch. The replies call must land on exactly the run whose slice
    contains the parent — an earlier slice holds no evidence, and asking
    without evidence is what the fixed budget forbids."""
    parent_ts = ts_at(2 * DAY)
    first_reply = ts_at(2 * DAY + 60)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [
        message(ts_at(1 * DAY), "day 1"),
        thread_parent(parent_ts, reply_count=1, latest_reply=first_reply),
        message(ts_at(3 * DAY), "day 3"),
        message(ts_at(4 * DAY), "day 4"),
        message(ts_at(5 * DAY), "day 5"),
        message(ts_at(29 * DAY), "recent"),
    ]
    fake.threads[(CHANNEL, parent_ts)] = [thread_reply(parent_ts, first_reply)]
    for _ in range(3):  # one complete lap: [day5, day4], [day3, parent], [day1]
        run_sync(fake, tmp_path, sweep_pages=1, sweep_page_size=2)

    revival = ts_at(29 * DAY + 300)
    for m in fake.messages[CHANNEL]:
        if str(m["ts"]) == parent_ts:
            m["reply_count"] = 2
            m["latest_reply"] = revival
    fake.threads[(CHANNEL, parent_ts)].append(thread_reply(parent_ts, revival, "thread revival"))

    _, archive, transport = run_sync(fake, tmp_path, sweep_pages=1, sweep_page_size=2)
    assert "conversations.replies" not in transport.methods  # this slice served day5/day4 only
    assert stored_message(archive, revival) is None

    report, archive, transport = run_sync(fake, tmp_path, sweep_pages=1, sweep_page_size=2)
    asked = [c.params.get("ts") for c in transport.calls if c.method == "conversations.replies"]
    assert asked == [parent_ts]
    assert stored_message(archive, revival)["thread_ts"] == parent_ts
    assert summary_of(report).new == 1


def test_a_slice_full_of_unchanged_parents_triggers_zero_replies_calls(tmp_path: Path) -> None:
    """The ``active_thread_ts`` union is deliberately absent from the sweep:
    over a slice it would re-ask every historical thread the slice touches and
    the fixed budget would explode. Unmoved counters must cost nothing — and
    the parents sit far below the recheck window, so the window's own
    recent-thread recheck cannot contribute calls either."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = []
    for d in range(1, 6):
        pts = ts_at(d * DAY)
        reply = ts_at(d * DAY + 60)
        fake.messages[CHANNEL].append(thread_parent(pts, reply_count=1, latest_reply=reply))
        fake.threads[(CHANNEL, pts)] = [thread_reply(pts, reply)]
    fake.messages[CHANNEL].append(message(ts_at(29 * DAY), "keeps the cursor recent"))
    run_sync(fake, tmp_path, sweep_pages=1)  # the seeding run legitimately fetches every thread once

    for _ in range(2):  # every later run laps a slice made entirely of settled parents
        report, _, transport = run_sync(fake, tmp_path, sweep_pages=1)
        assert "conversations.replies" not in transport.methods
        assert report.sweep_lap_completed  # honest: the slice really did re-serve the parents


def test_a_throttled_run_pauses_the_sweep_and_notes_it_once(tmp_path: Path) -> None:
    """Under the silent 15-message cap every request is precious: the window
    work runs, the sweep fetches not a single slice, and one note — not one
    per conversation — says the lap paused."""
    fake = FakeSlack(channels=[channel(), channel(OTHER_CHANNEL, "random")])
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY + i), f"m{i}") for i in range(15)]
    fake.messages[OTHER_CHANNEL] = [message(ts_at(28 * DAY + i), f"r{i}") for i in range(15)]
    report, archive, transport = run_sync(fake, tmp_path, sweep_pages=1, handlers=capped_handlers(fake))

    assert report.throttled
    assert sweep_slice_calls(transport) == []
    assert len([n for n in report.notes if "repair sweep was skipped" in n]) == 1
    assert report.sweep_lap_completed is False
    # Cheap, not skipped: both conversations' window messages are archived
    # despite the pause, including the one whose sync began after throttling
    # was first detected.
    stored = {str(r["text"]) for r in rows(archive, "SELECT text FROM messages")}
    assert {"m0", "m14", "r0", "r14"} <= stored


def test_a_full_run_resets_the_sweep_cursor_without_fetching_slices(tmp_path: Path) -> None:
    """``--full`` re-reads everything Slack still serves: that IS a lap, so a
    slice fetch on top would be waste — and the cursor must restart cleanly
    so the next incremental run begins a fresh lap."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [*old_days(1, 2, 3, 4, 5, 6, 7), message(ts_at(29 * DAY), "recent")]
    _, archive, _ = run_sync(fake, tmp_path, sweep_pages=1, sweep_page_size=3)
    assert archive.sweep_state(CHANNEL) == (ts_at(5 * DAY), None)  # mid-lap

    _, archive, transport = run_sync(fake, tmp_path, full=True, sweep_pages=1, now=NOW + 3600)
    assert sweep_slice_calls(transport) == []
    assert archive.sweep_state(CHANNEL) == (None, NOW + 3600)


def test_an_empty_slice_marks_nothing_and_still_completes_the_lap(tmp_path: Path) -> None:
    """A slice served empty is exactly what a retention policy hiding old
    history looks like; the archive exists to outlive retention, so nothing
    may be marked — but the lap must complete rather than jam on the silence."""
    old_one, old_two = message(ts_at(1 * DAY), "old one"), message(ts_at(2 * DAY), "old two")
    recent = message(ts_at(29 * DAY), "window evidence")
    fake = FakeSlack()
    fake.messages[CHANNEL] = [old_one, old_two, recent]
    run_sync(fake, tmp_path, sweep_pages=1)

    fake.messages[CHANNEL] = [recent]  # retention hid the old stretch, from the API's view
    report, archive, _ = run_sync(fake, tmp_path, sweep_pages=1)

    assert stored_message(archive, str(old_one["ts"]))["gone_at"] is None
    assert stored_message(archive, str(old_two["ts"]))["gone_at"] is None
    assert summary_of(report).gone == 0
    assert report.sweep_lap_completed is True
    assert archive.sweep_state(CHANNEL) == (None, NOW)


def test_a_conversation_smaller_than_one_page_laps_every_run(tmp_path: Path) -> None:
    """One slice per run at the lap start, lap complete every time: accepted
    as cheaper than special-casing tiny conversations, and the recorded state
    must say so on every run."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(1 * DAY), "the only message")]

    _, archive, transport = run_sync(fake, tmp_path, sweep_pages=1)
    assert sweep_slice_calls(transport) == [f"{NOW - 7 * DAY:.6f}"]
    assert archive.sweep_state(CHANNEL) == (None, NOW)

    later = NOW + 3600
    report, archive, transport = run_sync(fake, tmp_path, sweep_pages=1, now=later)
    assert sweep_slice_calls(transport) == [f"{later - 7 * DAY:.6f}"]
    assert archive.sweep_state(CHANNEL) == (None, later)
    assert report.sweep_lap_completed is True


def test_two_sweep_pages_fetch_two_chained_slices_in_one_run(tmp_path: Path) -> None:
    """``--sweep 2`` buys two slices per run, and they must tile exactly as
    two consecutive runs would: the second slice's latest bound is the first
    slice's oldest served message."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [*old_days(1, 2, 3, 4, 5, 6, 7), message(ts_at(29 * DAY), "recent")]

    report, archive, transport = run_sync(fake, tmp_path, sweep_pages=2, sweep_page_size=3)
    assert sweep_slice_calls(transport) == [f"{NOW - 7 * DAY:.6f}", ts_at(5 * DAY)]
    assert archive.sweep_state(CHANNEL) == (ts_at(2 * DAY), None)
    assert report.sweep_pages == 2


# -- the users micro-rota ------------------------------------------------------------


def test_the_rota_refreshes_exactly_the_stalest_user_each_run(tmp_path: Path) -> None:
    """One ``users.info`` per run, spent on the name unchecked longest — and
    the bump must be recorded, or the rota would re-ask the same user instead
    of rotating through the roster."""
    fake = FakeSlack()
    fake.users["U0EXAMPLE3"] = "carol"
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]
    _, archive, _ = run_sync(fake, tmp_path, sweep_pages=1)
    archive.upsert_user("U0EXAMPLE2", "bob", NOW - 3 * DAY)
    archive.upsert_user("U0EXAMPLE3", "carol", NOW - 5 * DAY)

    _, archive, transport = run_sync(fake, tmp_path, sweep_pages=1)
    assert [c.params.get("user") for c in transport.calls if c.method == "users.info"] == ["U0EXAMPLE3"]
    carol = rows(archive, "SELECT * FROM users WHERE id = ?", "U0EXAMPLE3")[0]
    assert carol["refreshed_at"] == NOW

    _, _, transport = run_sync(fake, tmp_path, sweep_pages=1)
    assert [c.params.get("user") for c in transport.calls if c.method == "users.info"] == ["U0EXAMPLE2"]


def test_users_fresher_than_a_day_earn_no_rota_calls(tmp_path: Path) -> None:
    """Churning a fresh name is a request spent on nothing; under a day of
    age the rota stays silent."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY), "ping <@U0EXAMPLE2>")]
    run_sync(fake, tmp_path, sweep_pages=1)

    _, _, transport = run_sync(fake, tmp_path, sweep_pages=1, now=NOW + 3600)
    assert "users.info" not in transport.methods


def test_a_rename_reaches_the_users_table_and_then_the_rows_via_the_lap(tmp_path: Path) -> None:
    """The rota's half of a rename is the users table; a stored row's
    ``sender_name`` may only move when a sweep slice actually re-serves it —
    that is the bounded-staleness bargain, not an oversight."""
    old_ts = ts_at(2 * DAY)
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(old_ts, "old words"), message(ts_at(29 * DAY), "recent")]
    run_sync(fake, tmp_path, sweep_pages=1)

    fake.users["U0EXAMPLE1"] = "alexandra"
    _, archive, transport = run_sync(fake, tmp_path, sweep_pages=1, now=NOW + 2 * DAY)
    assert [c.params.get("user") for c in transport.calls if c.method == "users.info"] == ["U0EXAMPLE1"]
    assert archive.user_names()["U0EXAMPLE1"] == "alexandra"
    assert stored_message(archive, old_ts)["sender_name"] == "alice"  # no slice has re-served it yet

    _, archive, _ = run_sync(fake, tmp_path, sweep_pages=1, now=NOW + 3 * DAY)
    assert stored_message(archive, old_ts)["sender_name"] == "alexandra"


def test_sweep_zero_disables_the_users_rota_too(tmp_path: Path) -> None:
    """One knob, one concept: repair off means no rota either — and the same
    stale user earns a call the moment the sweep is back on, proving the
    silence came from the knob, not from freshness."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]
    run_sync(fake, tmp_path)

    _, _, transport = run_sync(fake, tmp_path, now=NOW + 2 * DAY)  # sweep off: run_sync's default
    assert "users.info" not in transport.methods

    _, _, transport = run_sync(fake, tmp_path, sweep_pages=1, now=NOW + 2 * DAY)
    assert [c.params.get("user") for c in transport.calls if c.method == "users.info"] == ["U0EXAMPLE1"]


def test_a_dead_account_keeps_its_name_but_does_not_pin_the_rota(tmp_path: Path) -> None:
    """A failing ``users.info`` (deleted account) must not erase the archived
    name — and must still advance the clock, or one dead account would pin
    the rota forever while live renames wait behind it."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]
    _, archive, _ = run_sync(fake, tmp_path, sweep_pages=1)
    archive.upsert_user("U0EXAMPLE2", "bob", NOW - 6 * DAY)
    del fake.users["U0EXAMPLE2"]

    _, archive, transport = run_sync(fake, tmp_path, sweep_pages=1, now=NOW + 2 * DAY)
    assert [c.params.get("user") for c in transport.calls if c.method == "users.info"] == ["U0EXAMPLE2"]
    bob = rows(archive, "SELECT * FROM users WHERE id = ?", "U0EXAMPLE2")[0]
    assert bob["name"] == "bob"
    assert bob["refreshed_at"] == NOW + 2 * DAY

    _, _, transport = run_sync(fake, tmp_path, sweep_pages=1, now=NOW + 3 * DAY)
    assert [c.params.get("user") for c in transport.calls if c.method == "users.info"] == ["U0EXAMPLE1"]


# -- the roster reconcile guard --------------------------------------------------------


def test_an_incomplete_roster_listing_suspends_gone_marking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``paginate()`` stops silently at its backstop; on a roster that long,
    absence from the listing proves nothing, so gone-marking must stand down
    for the run and the report must say why."""
    from slack_scrollback.workspace import Workspace

    dropped = channel(OTHER_CHANNEL, "random")
    fake = FakeSlack(channels=[channel(), dropped])
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]
    run_sync(fake, tmp_path)

    fake.channels = [channel()]
    monkeypatch.setattr(Workspace, "conversations_listing_complete", property(lambda self: False))
    report, archive, _ = run_sync(fake, tmp_path)

    row = rows(archive, "SELECT * FROM conversations WHERE id = ?", OTHER_CHANNEL)[0]
    assert row["gone_at"] is None
    assert any("roster was too long" in note for note in report.notes)


# -- sweep telemetry -------------------------------------------------------------------


def test_sweep_telemetry_counts_slices_and_the_oldest_verified_ts(tmp_path: Path) -> None:
    """The report is what sizes a lap: pages actually fetched, how deep this
    run verified, and whether the lap closed — in the dataclass and in the
    JSON summary alike."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [*old_days(1, 2, 3, 4, 5, 6, 7), message(ts_at(29 * DAY), "recent")]

    report, _, transport = run_sync(fake, tmp_path, sweep_pages=2, sweep_page_size=3)
    assert report.sweep_pages == len(sweep_slice_calls(transport)) == 2
    assert report.sweep_oldest_verified == ts_at(2 * DAY)
    assert report.sweep_lap_completed is False
    records = [json.loads(line) for line in render_sync_report(report, as_json=True)]
    summary = next(r for r in records if r["type"] == "summary")
    assert summary["sweep"] == {"pages": 2, "oldest_verified": ts_at(2 * DAY), "lap_completed": False}

    report, _, transport = run_sync(fake, tmp_path, sweep_pages=2, sweep_page_size=3)
    assert report.sweep_pages == len(sweep_slice_calls(transport)) == 1  # day 1 ends the lap early
    assert report.sweep_oldest_verified == ts_at(1 * DAY)
    assert report.sweep_lap_completed is True


def test_sweep_off_reports_zero_pages_and_an_open_lap(tmp_path: Path) -> None:
    """``--sweep 0`` must be legible in the summary: zero pages, nothing
    verified, lap not completed — "repair did not run" must never read as
    "the lap is done"."""
    fake = FakeSlack()
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]

    report, _, transport = run_sync(fake, tmp_path)  # run_sync's default is sweep off
    assert sweep_slice_calls(transport) == []
    assert report.sweep_pages == 0
    assert report.sweep_oldest_verified is None
    records = [json.loads(line) for line in render_sync_report(report, as_json=True)]
    summary = next(r for r in records if r["type"] == "summary")
    assert summary["sweep"] == {"pages": 0, "oldest_verified": None, "lap_completed": False}


def test_a_roster_hitting_the_pagination_cap_disables_gone_marking_for_real(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The completeness guard exercised end to end, not via a patched flag: a
    conversations.list that still has a cursor when the page backstop hits is
    not the whole roster, and judging absences against it would gone-mark
    every conversation beyond the cap."""
    import slack_scrollback.api as api_module
    import slack_scrollback.workspace as workspace_module

    monkeypatch.setattr(api_module, "MAX_PAGES", 2)
    monkeypatch.setattr(workspace_module, "MAX_PAGES", 2)

    fake = FakeSlack(channels=[channel(), channel(OTHER_CHANNEL, "random")])
    fake.messages[CHANNEL] = [message(ts_at(29 * DAY))]
    run_sync(fake, tmp_path)  # both conversations archived under a complete roster

    pages: dict[str, dict[str, Any]] = {
        "": {"channels": [channel()], "cursor": "page2"},
        "page2": {"channels": [], "cursor": "page3"},  # OTHER_CHANNEL never listed before the cap
        "page3": {"channels": [channel(OTHER_CHANNEL, "random")], "cursor": ""},
    }

    def paginated_list(params: dict[str, Any]) -> dict[str, Any]:
        page = pages[str(params.get("cursor", ""))]
        return ok(channels=page["channels"], response_metadata={"next_cursor": page["cursor"]})

    handlers = fake.handlers()
    handlers["conversations.list"] = paginated_list
    report, archive, _ = run_sync(fake, tmp_path, now=NOW + 60, handlers=handlers)

    unlisted = rows(archive, "SELECT * FROM conversations WHERE id = ?", OTHER_CHANNEL)[0]
    assert unlisted["gone_at"] is None
    assert any("roster was too long" in note for note in report.notes)
