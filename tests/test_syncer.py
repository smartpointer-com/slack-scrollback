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


class _CappedWorkspace(FakeSlack):
    """History answers exactly 15 messages with more pending — the silent-cap signature."""

    def _history(self, params: dict[str, str]) -> dict[str, Any]:
        return ok(messages=[message(ts_at(29 * DAY + i)) for i in range(15)], has_more=True)


def test_a_silently_capped_page_is_reported_as_throttling(tmp_path: Path) -> None:
    """Sync asks for 1000; fifteen-with-more can only be Slack's cap, and the report must say so."""
    report, _, _ = run_sync(_CappedWorkspace(), tmp_path)

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


def test_a_dropped_conversation_is_marked_gone_only_by_a_full_run(tmp_path: Path) -> None:
    """An incremental run cannot tell "gone" from "not looked at", so only --full draws the conclusion."""
    fake = FakeSlack(channels=[channel(), channel(OTHER_CHANNEL, "random")])
    run_sync(fake, tmp_path)

    fake.channels = [channel()]
    _, archive, _ = run_sync(fake, tmp_path)
    assert rows(archive, "SELECT gone_at FROM conversations WHERE id = ?", OTHER_CHANNEL)[0]["gone_at"] is None

    report, archive, _ = run_sync(fake, tmp_path, full=True)
    assert rows(archive, "SELECT gone_at FROM conversations WHERE id = ?", OTHER_CHANNEL)[0]["gone_at"] is not None
    assert any("no longer visible" in note for note in report.notes)


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

    _, _, transport = run_sync(fake, tmp_path)
    replies_calls = [c for c in transport.calls if c.method == "conversations.replies"]
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

    assert "this SQLite lacks FTS5; archive search will fall back to a full scan" in report.notes
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
    original = fake._history

    def capped(params: dict[str, str]) -> dict[str, Any]:
        body = original(params)
        body["messages"] = body["messages"][:15]
        body["has_more"] = True
        return body

    fake_handlers = fake.handlers()
    fake_handlers["conversations.history"] = capped
    from slack_scrollback.archive import Archive
    from slack_scrollback.syncer import Syncer
    from slack_scrollback.workspace import Workspace
    from tests.conftest import TOKEN, make_client

    client, _ = make_client(fake_handlers)
    workspace = Workspace(client)
    archive = Archive.open_rw(tmp_path)
    report = Syncer(workspace, client, archive, token=TOKEN, now_fn=lambda: NOW).run()

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

    from slack_scrollback.archive import Archive
    from slack_scrollback.syncer import Syncer
    from slack_scrollback.workspace import Workspace
    from tests.conftest import TOKEN, make_client

    ticks: list[str] = []
    client, _ = make_client(fake.handlers())
    downloads = FakeFileHost(responses={str(slack_file()["url_private"]): file_body(b"abcdef")})
    Syncer(
        Workspace(client),
        client,
        Archive.open_rw(tmp_path),
        token=TOKEN,
        media_tiers=frozenset({"documents"}),
        now_fn=lambda: NOW,
        download_transport=downloads,
        progress=ticks.append,
    ).run()

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
