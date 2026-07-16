"""``sync``: mirror everything the bot can read into the local archive.

One run is one transaction. Every write between :meth:`Archive.begin` and
:meth:`Archive.commit` lands together or not at all, so a crash mid-run leaves
the archive exactly as the previous run left it, and re-running is always safe.

The incremental window re-reads a trailing stretch of history (``--recheck``,
default seven days) rather than resuming exactly at the cursor, because the
recent past is not settled: edits and deletions only reveal themselves by
re-reading. An edit shows up as a changed ``edited.ts``; a deletion shows up
as a message the archive holds that Slack no longer returns — but absence is
treated as evidence only inside its evidence window: the fetch must have been
paged to the end, and only down to the oldest message the response actually
served. Below that line, and in a window served empty, a missing message is
exactly what a retention policy hiding old history looks like, and the archive
exists to outlive retention — so nothing is marked, at the deliberate price of
sometimes serving a genuinely deleted message a while longer.

Thread replies need their own care. A windowed history response contains no
trace of a reply to an old thread, so replies are found two ways: parents seen
in the window carry ``reply_count``/``latest_reply`` and are re-read when those
moved past what the archive holds, and threads with any *stored* activity
inside the window are re-asked outright. What that still misses — a thread
silent for longer than the recheck window coming back to life — is caught when
the repair sweep re-serves its parent, within a lap. The one drift no lap can
see is an edit to an old reply, which moves no counter a history page carries;
``--full`` remains its repair, and the README says so.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .api import MAX_PAGES, SlackClient
from .archive import Archive
from .download import download_to, sanitized_filename
from .errors import DownloadError, ScrollbackError, SlackApiError
from .format import format_timestamp, message_body, speaker, throttle_notice
from .workspace import SLACKBOT_USER_ID, Conversation, Entry, Workspace

#: How long an archived user name is trusted before ``users.info`` re-asks.
USER_REFRESH_SECONDS = 30 * 24 * 3600.0

#: The repair sweep's slice size: one conversations.history page per slice.
SWEEP_PAGE_SIZE = 200

#: The name rota re-checks at most one user per run, and only one at least
#: this stale — churning fresher names would be requests spent on nothing.
USER_ROTA_MIN_AGE_SECONDS = 24 * 3600.0

DEFAULT_RECHECK = "7d"


@dataclass
class ConversationSummary:
    """What one conversation's sync changed."""

    name: str
    new: int = 0
    edited: int = 0
    gone: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.new or self.edited or self.gone)


@dataclass
class SyncReport:
    """Everything a run did, for the human or agent that launched it."""

    archive_path: str
    conversations: list[ConversationSummary] = field(default_factory=list)
    files_downloaded: int = 0
    bytes_downloaded: int = 0
    download_failures: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    synced_at: float = 0.0
    throttled: bool = False
    notes: list[str] = field(default_factory=list)
    #: Repair-sweep telemetry: slices fetched, the oldest ts re-verified this
    #: run, and whether every conversation's lap is complete as of this run.
    sweep_pages: int = 0
    sweep_oldest_verified: str | None = None
    sweep_lap_completed: bool = True


class Syncer:
    """One sync run against one archive.

    ``now_fn`` and ``download_transport`` are injectable so tests control the
    clock and the wire. ``progress``, when given, receives one short line per
    step — which conversation, which thread, which download — as the run's
    only sign of life before the report. The run stamps a single ``now`` at the start and uses
    it throughout — as the window's upper bound, and as the moment everything
    first seen or last seen is recorded against.
    """

    def __init__(
        self,
        workspace: Workspace,
        client: SlackClient,
        archive: Archive,
        *,
        token: str,
        full: bool = False,
        recheck_seconds: float = 7 * 24 * 3600.0,
        media_tiers: frozenset[str] = frozenset(),
        media_max_bytes: int | None = None,
        sweep_pages: int = 1,
        sweep_page_size: int = SWEEP_PAGE_SIZE,
        timeout: float = 30.0,
        now_fn: Callable[[], float] = time.time,
        download_transport: Any = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._workspace = workspace
        self._client = client
        self._archive = archive
        self._token = token
        self._full = full
        self._recheck = recheck_seconds
        self._tiers = media_tiers
        self._max_bytes = media_max_bytes
        self._sweep_pages = sweep_pages
        self._sweep_page_size = sweep_page_size
        self._timeout = timeout
        self._now_fn = now_fn
        self._transport = download_transport
        self._progress = progress
        self._seeded: set[str] = set()

    def run(self) -> SyncReport:
        started = time.monotonic()
        now = self._now_fn()
        report = SyncReport(archive_path=str(self._archive.directory), synced_at=now)

        # Seeding makes archived names answer ``users.info`` lookups for free.
        # A --full run deliberately seeds nothing: refreshing every speaker's
        # name is part of what "full" means.
        if not self._full:
            seeded = self._archive.fresh_user_names(now=now, max_age_seconds=USER_REFRESH_SECONDS)
            self._workspace.seed_user_names(seeded)
            self._seeded = set(seeded)

        self._archive.begin()
        try:
            self._tick("listing conversations")
            self._record_workspace(now)
            conversations = self._workspace.all_conversations()
            for conversation in conversations:
                self._archive.upsert_conversation(
                    conversation_id=conversation.id,
                    name=conversation.name,
                    kind=conversation.kind,
                    is_member=bool(conversation.raw.get("is_member")) or conversation.kind in ("dm", "group-dm"),
                    now=now,
                )
            # Reconciling the roster is safe on every run: the listing is
            # complete (guarded here), gone_at is soft and self-healing, and
            # a partial listing aborts the whole transaction anyway.
            if self._workspace.conversations_listing_complete:
                vanished = self._archive.mark_conversations_gone((c.id for c in conversations), now)
                if vanished:
                    report.notes.append(f"{vanished} conversations are no longer visible and were marked gone")
            else:
                report.notes.append(
                    "the conversation roster was too long to list completely; vanished conversations were not checked"
                )

            # The Slackbot DM stays in the roster but is never fetched:
            # Slack answers channel_not_found for its history, to every bot,
            # every time. Asking would only manufacture a recurring note
            # about a conversation that structurally has nothing to give.
            readable = [
                c
                for c in self._workspace.readable_conversations(include_archived=True)
                if c.counterpart_id != SLACKBOT_USER_ID
            ]
            for index, conversation in enumerate(readable, start=1):
                summary = self._sync_conversation(conversation, now, report, position=f"{index}/{len(readable)}")
                report.conversations.append(summary)

            self._store_user_names(now)
            if self._sweep_pages > 0 and not self._full:
                self._refresh_stalest_name(now)
            self._download_media(now, report)
            self._archive.set_meta("last_sync_at", f"{now:.6f}")
            self._archive.commit()
        except BaseException:
            # The connection dies with the run; an explicit rollback just makes
            # the one-transaction contract legible at the point it matters.
            self._archive.rollback()
            raise

        if self._sweep_pages == 0 and not self._full:
            report.sweep_lap_completed = False
        if self._archive.fts_unavailable_reason:
            report.notes.append("this SQLite lacks FTS5; archive search will fall back to a full scan")
        report.duration_seconds = time.monotonic() - started
        return report

    # -- workspace-level state -------------------------------------------------

    def _record_workspace(self, now: float) -> None:
        body = self._client.call("auth.test")
        if body.get("team_id"):
            self._archive.set_meta("team_id", str(body["team_id"]))
        if body.get("url"):
            self._archive.set_meta("team_url", str(body["url"]).rstrip("/"))
        if self._archive.get_meta("created_at") is None:
            self._archive.set_meta("created_at", f"{now:.6f}")

    def _store_user_names(self, now: float) -> None:
        """Persist every name this run actually resolved.

        Seeded names are skipped: they came *from* the users table, and writing
        them back would refresh a timestamp that no request justified.
        """
        for user_id, name in self._workspace.known_user_names().items():
            if user_id not in self._seeded:
                self._archive.upsert_user(user_id, name, now)

    # -- one conversation --------------------------------------------------------

    def _sync_conversation(
        self, conversation: Conversation, now: float, report: SyncReport, *, position: str
    ) -> ConversationSummary:
        label = f"{conversation.name} ({position})"
        self._tick(label)
        summary = ConversationSummary(name=conversation.name)
        last_ts = self._archive.last_ts(conversation.id)
        oldest_epoch = 0.0 if self._full else min(float(last_ts), now - self._recheck)
        oldest_epoch = max(oldest_epoch, 0.0)

        seen_ts: set[str] = set()
        newest_seen = last_ts
        thread_meta: dict[str, tuple[int, str]] = {}
        pages = 0
        try:
            for page, throttled in self._workspace.history_pages(
                conversation,
                oldest=f"{oldest_epoch:.6f}" if oldest_epoch > 0 else None,
                latest=f"{now:.6f}",
                page_limit=1000,
            ):
                pages += 1
                if throttled and not report.throttled:
                    report.throttled = True
                    report.notes.append(throttle_notice().strip("[]").removeprefix("note: "))
                for message in page.get("messages") or []:
                    ts = str(message.get("ts") or "")
                    if not ts or ts in seen_ts:
                        continue
                    seen_ts.add(ts)
                    if float(ts) > float(newest_seen or 0):
                        newest_seen = ts
                    self._count(summary, self._store_message(conversation, message, now))
                    if len(seen_ts) % 200 == 0:
                        self._tick(f"{label} — {len(seen_ts)} messages")
                    if self._is_thread_parent(message):
                        thread_meta[ts] = (
                            int(message.get("reply_count") or 0),
                            str(message.get("latest_reply") or ""),
                        )
        except SlackApiError as exc:
            report.notes.append(f"{conversation.name}: history unavailable this run ({exc.code}) — skipped")
            return summary

        # Deletion is inferred from absence, and absence is evidence only
        # where Slack demonstrably still serves history: at or above the
        # oldest message this response actually contained. Below that line —
        # and in a window Slack returned empty — a missing message is
        # indistinguishable from one hidden by a retention policy, and the
        # archive exists precisely to outlive retention, so nothing is
        # marked. The cost is deliberate and conservative: deleting the
        # oldest message of a window goes unnoticed until some older message
        # is served alongside it, which keeps serving a deleted message for
        # a while rather than ever burying a retained one.
        evidence_floor: float | None = None
        if pages >= MAX_PAGES:
            report.notes.append(
                f"{conversation.name}: the window was too large to page completely; deletions were not checked this run"
            )
        elif seen_ts:
            evidence_floor = max(oldest_epoch, min(float(ts) for ts in seen_ts))
            expected = self._archive.channel_level_ts_between(conversation.id, evidence_floor, now)
            summary.gone += self._archive.mark_messages_gone(conversation.id, expected - seen_ts, now)

        to_check = sorted(self._threads_to_check(conversation, thread_meta, oldest_epoch))
        for thread_index, thread_ts in enumerate(to_check, start=1):
            self._tick(f"{label} — thread {thread_index}/{len(to_check)}")
            self._sync_thread(conversation, thread_ts, now, summary, report, evidence_floor=evidence_floor)

        if self._full:
            # A full run re-verified everything reachable: that IS a lap.
            self._archive.set_sweep_state(conversation.id, None, now, lap_completed=True)
        elif self._sweep_pages > 0:
            self._sweep_conversation(conversation, label, now, summary, report)

        if seen_ts:
            self._archive.set_sync_state(conversation.id, newest_seen, now, full=self._full)
        else:
            self._archive.set_sync_state(conversation.id, last_ts, now, full=self._full)
        return summary

    def _tick(self, text: str) -> None:
        if self._progress is not None:
            self._progress(text)

    @staticmethod
    def _count(summary: ConversationSummary, status: str) -> None:
        if status == "new":
            summary.new += 1
        elif status == "edited":
            summary.edited += 1

    @staticmethod
    def _is_thread_parent(message: dict[str, Any]) -> bool:
        thread_ts = message.get("thread_ts")
        return bool(thread_ts) and str(thread_ts) == str(message.get("ts"))

    def _threads_to_check(
        self, conversation: Conversation, thread_meta: dict[str, tuple[int, str]], oldest_epoch: float
    ) -> set[str]:
        """Which threads deserve a ``conversations.replies`` call this run."""
        if self._full:
            return set(thread_meta) | self._archive.thread_ts_with_replies(conversation.id)

        to_check = self._moved_threads(conversation, thread_meta)
        # Threads already known to be recently alive are re-asked even when
        # their parent sits outside the window — the response is the only
        # place a new reply to an old parent shows up at all.
        recent = self._archive.active_thread_ts(conversation.id, oldest_epoch)
        to_check |= {ts for ts in recent if ts not in thread_meta}
        return to_check

    def _moved_threads(self, conversation: Conversation, thread_meta: dict[str, tuple[int, str]]) -> set[str]:
        """Served parents whose reply state moved past what the archive holds."""
        moved: set[str] = set()
        for thread_ts, (reply_count, latest_reply) in thread_meta.items():
            stored_count, stored_latest = self._archive.reply_stats(conversation.id, thread_ts)
            if reply_count != stored_count or (bool(latest_reply) and float(latest_reply) > float(stored_latest or 0)):
                moved.add(thread_ts)
        return moved

    def _sweep_conversation(
        self, conversation: Conversation, label: str, now: float, summary: ConversationSummary, report: SyncReport
    ) -> None:
        """One fixed-size slice of re-verification, below everything settled.

        The cursor walks from the recheck boundary toward the beginning of
        history and wraps; each slice re-serves a page, so old edits land,
        renames re-render, deletions are noticed (with slice-scoped evidence),
        and re-served parents flow through the same moved-thread diff the
        window uses. Deliberately absent: the ``active_thread_ts`` union —
        applied to a slice it would re-ask every historical thread the slice
        touches, and the fixed budget would explode. Every parent lives in
        some slice, so a lap still visits every revived thread — but reply
        *bodies* re-verify only when their thread's counters move: an old
        reply edited in place is the one drift a lap cannot see, and the
        README says so.
        """
        if report.throttled:
            # Under the 1 req/min cap the window work must stay affordable;
            # the lap pauses and resumes when requests are cheap again.
            self._note_sweep_paused(report)
            return

        sweep_before, _ = self._archive.sweep_state(conversation.id)
        lap_completed = False
        for _ in range(self._sweep_pages):
            top = sweep_before if sweep_before is not None else f"{now - self._recheck:.6f}"
            self._tick(f"{label} — sweeping history before {format_timestamp(top)}")
            messages, has_more, throttled = self._workspace.history_slice(
                conversation, latest=top, limit=self._sweep_page_size
            )
            if throttled:
                report.throttled = True
                self._note_sweep_paused(report)
                break
            report.sweep_pages += 1
            seen: set[str] = set()
            thread_meta: dict[str, tuple[int, str]] = {}
            for message in messages:
                ts = str(message.get("ts") or "")
                if not ts or ts in seen:
                    continue
                seen.add(ts)
                self._count(summary, self._store_message(conversation, message, now))
                if self._is_thread_parent(message):
                    thread_meta[ts] = (
                        int(message.get("reply_count") or 0),
                        str(message.get("latest_reply") or ""),
                    )
            if seen:
                oldest_served = min(seen, key=float)
                # Slice-scoped deletion evidence: the page served everything
                # in [oldest_served, top) — top itself belongs to the previous
                # slice (Slack's latest is exclusive), so the comparison must
                # be exclusive too, or top's message is marked gone every lap.
                expected = self._archive.channel_level_ts_between(
                    conversation.id, float(oldest_served), float(top), latest_inclusive=False
                )
                summary.gone += self._archive.mark_messages_gone(conversation.id, expected - seen, now)
                for thread_ts in sorted(self._moved_threads(conversation, thread_meta)):
                    self._sync_thread(
                        conversation, thread_ts, now, summary, report, evidence_floor=float(oldest_served)
                    )
                sweep_before = oldest_served
                report.sweep_oldest_verified = _older_ts(report.sweep_oldest_verified, oldest_served)
            if not has_more:
                lap_completed = True
                sweep_before = None
                break
            if not seen:
                # More history claimed but nothing served: the cursor cannot
                # advance without inventing a boundary. Retry next run.
                break

        self._archive.set_sweep_state(conversation.id, sweep_before, now, lap_completed=lap_completed)
        if not lap_completed:
            report.sweep_lap_completed = False

    def _note_sweep_paused(self, report: SyncReport) -> None:
        note = "the repair sweep was skipped this run (Slack is throttling); the lap resumes when requests are cheap"
        if note not in report.notes:
            report.notes.append(note)
        report.sweep_lap_completed = False

    def _refresh_stalest_name(self, now: float) -> None:
        """Re-check one user's name per run — the stalest one.

        The seed window trusts archived names for 30 days; without this rota a
        rename could hide behind that trust indefinitely on a workspace where
        the person never speaks. One ``users.info`` per run bounds rename lag
        by max(one rota cycle, one sweep lap) instead.
        """
        user_id = self._archive.stalest_user(now=now, min_age_seconds=USER_ROTA_MIN_AGE_SECONDS)
        if user_id is None:
            return
        previous = self._archive.user_names().get(user_id)
        fresh = self._workspace.refresh_user_name(user_id)
        if fresh == user_id and previous:
            # The lookup failed (deleted or invisible account). Keep the name
            # we had — but bump the clock, or one dead account pins the rota.
            fresh = previous
        self._archive.upsert_user(user_id, fresh, now)

    def _sync_thread(
        self,
        conversation: Conversation,
        thread_ts: str,
        now: float,
        summary: ConversationSummary,
        report: SyncReport,
        *,
        evidence_floor: float | None,
    ) -> None:
        try:
            fetched = list(
                self._client.paginate(
                    "conversations.replies", "messages", limit=1000, channel=conversation.id, ts=thread_ts
                )
            )
        except SlackApiError as exc:
            if exc.code == "thread_not_found":
                # "Not found" means deleted only where this run has evidence
                # Slack still serves history that old — a parent aged out of
                # retention answers exactly the same way, and must stay.
                if evidence_floor is not None and float(thread_ts) >= evidence_floor:
                    gone = {thread_ts} | self._archive.reply_ts(conversation.id, thread_ts)
                    summary.gone += self._archive.mark_messages_gone(conversation.id, gone, now)
                return
            report.notes.append(f"{conversation.name}: thread {thread_ts} unavailable this run ({exc.code}) — skipped")
            return

        seen: set[str] = set()
        for message in fetched:
            ts = str(message.get("ts") or "")
            if not ts or ts in seen:
                continue
            seen.add(ts)
            self._count(summary, self._store_message(conversation, message, now))
        vanished = self._archive.reply_ts(conversation.id, thread_ts) - seen
        summary.gone += self._archive.mark_messages_gone(conversation.id, vanished, now)

    # -- one message ----------------------------------------------------------------

    def _store_message(self, conversation: Conversation, message: dict[str, Any], now: float) -> str:
        entry = Entry(message=message, conversation=conversation)
        body = message_body(entry, resolve_user=self._workspace.user_name, resolve_channel=self._workspace.channel_name)
        status = self._archive.upsert_message(
            channel_id=conversation.id,
            ts=str(message.get("ts") or ""),
            thread_ts=str(message["thread_ts"]) if message.get("thread_ts") else None,
            subtype=str(message["subtype"]) if message.get("subtype") else None,
            user_id=str(message["user"]) if message.get("user") else None,
            sender_name=speaker(message, self._workspace.user_name),
            # "(no text)" is the *renderer's* stand-in; the stored text is
            # blank so indexers can drop the row rather than index the prop.
            text="" if body == "(no text)" else body,
            raw=message,
            edited_ts=str((message.get("edited") or {}).get("ts") or "") or None,
            now=now,
        )
        for raw_file in message.get("files") or []:
            if isinstance(raw_file, dict) and raw_file.get("id"):
                self._store_file(conversation.id, str(message.get("ts") or ""), raw_file, now)
        return status

    def _store_file(self, channel_id: str, ts: str, raw_file: dict[str, Any], now: float) -> None:
        size = raw_file.get("size")
        self._archive.upsert_file(
            file_id=str(raw_file["id"]),
            name=str(raw_file.get("name") or raw_file.get("title") or "") or None,
            mimetype=str(raw_file.get("mimetype") or "") or None,
            filetype=str(raw_file.get("filetype") or "") or None,
            size=int(size) if isinstance(size, int) else None,
            mode=str(raw_file.get("mode") or "") or None,
            permalink=str(raw_file.get("permalink") or "") or None,
            url_private=str(raw_file.get("url_private") or "") or None,
            now=now,
        )
        self._archive.link_file(channel_id, ts, str(raw_file["id"]))

    # -- media --------------------------------------------------------------------------

    def _download_media(self, now: float, report: SyncReport) -> None:
        queue = self._archive.download_queue(tiers=self._tiers, max_bytes=self._max_bytes)
        for count, row in enumerate(queue, start=1):
            file_id = str(row["id"])
            name = sanitized_filename(row["name"], fallback=file_id)
            self._tick(f"downloading {name} ({count}/{len(queue)})")
            dest = self._archive.media_dir / file_id / name
            label = f"{name} ({file_id})"
            try:
                written = download_to(
                    str(row["url_private"]),
                    dest,
                    token=self._token,
                    label=label,
                    expected_size=int(row["size"]) if row["size"] is not None else None,
                    timeout=self._timeout,
                    transport=self._transport,
                )
            except (DownloadError, ScrollbackError) as exc:
                report.download_failures.append(str(exc))
                continue
            self._archive.set_local_path(file_id, str(dest), now)
            report.files_downloaded += 1
            report.bytes_downloaded += written


def _older_ts(current: str | None, candidate: str) -> str:
    return candidate if current is None or float(candidate) < float(current) else current


def _human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{int(size)} B"  # pragma: no cover - unreachable


def render_sync_report(report: SyncReport, *, as_json: bool = False) -> list[str]:
    """The run's outcome, in the tool's two output shapes.

    Only changed conversations get their own line — a quiet workspace should
    produce a quiet report — but the summary always states the totals, so
    "nothing changed" is said rather than implied.
    """
    changed = [c for c in report.conversations if c.changed]
    if as_json:
        lines = [
            json.dumps(
                {"type": "conversation", "name": c.name, "new": c.new, "edited": c.edited, "gone": c.gone},
                ensure_ascii=False,
                sort_keys=True,
            )
            for c in changed
        ]
        lines += [
            json.dumps({"type": "download_failure", "text": failure}, ensure_ascii=False, sort_keys=True)
            for failure in report.download_failures
        ]
        lines.append(
            json.dumps(
                {
                    "type": "summary",
                    "conversations": len(report.conversations),
                    "changed": len(changed),
                    "new": sum(c.new for c in report.conversations),
                    "edited": sum(c.edited for c in report.conversations),
                    "gone": sum(c.gone for c in report.conversations),
                    "files_downloaded": report.files_downloaded,
                    "bytes_downloaded": report.bytes_downloaded,
                    "download_failures": len(report.download_failures),
                    "duration_seconds": round(report.duration_seconds, 3),
                    "archive": report.archive_path,
                    "synced_at": format_timestamp(f"{report.synced_at:.6f}"),
                    "sweep": {
                        "pages": report.sweep_pages,
                        "oldest_verified": report.sweep_oldest_verified,
                        "lap_completed": report.sweep_lap_completed,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        lines += [
            json.dumps({"type": "notice", "text": note}, ensure_ascii=False, sort_keys=True) for note in report.notes
        ]
        return lines

    lines = [f"{c.name}: {c.new} new, {c.edited} edited, {c.gone} gone" for c in changed]
    if report.files_downloaded:
        plural = "file" if report.files_downloaded == 1 else "files"
        lines.append(f"downloaded {report.files_downloaded} {plural} ({_human_bytes(report.bytes_downloaded)})")
    lines += [f"download failed: {failure}" for failure in report.download_failures]
    lines.append(
        f"synced {len(report.conversations)} conversations "
        f"({len(changed)} changed) in {report.duration_seconds:.1f}s — archive: {report.archive_path}"
    )
    lines += [f"[note: {note}]" for note in report.notes]
    return lines
