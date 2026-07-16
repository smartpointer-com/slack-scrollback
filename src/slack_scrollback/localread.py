"""Archive-backed reads: the same four questions, answered from local disk.

The point of a second backend is that nothing else changes. Messages come back
as the same :class:`Entry` objects, rendered by the same formatter, resolved by
the same channel-spec rules — so the output of an archive read is the output of
a live read plus one provenance trailer, and a recipe written against one
backend works against the other.

Names come from the archive's ``users`` and ``conversations`` tables instead of
``users.info``. Everything marked ``gone_at`` is filtered: the archive keeps
what Slack deleted, but *serving* deleted messages is the ``file`` command's
explicitly-labelled business, not a search result's.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .archive import Archive
from .errors import UsageError
from .format import format_timestamp
from .workspace import (
    SEARCH_EXCLUDED_SUBTYPES,
    Conversation,
    Entry,
    FetchResult,
    is_readable,
    no_such_speaker_note,
    resolve_conversation,
)

PROVENANCE_HINT = "pass --live to read Slack directly"


class ArchiveReader:
    """Read-only view of one archive, shaped like :class:`Workspace`."""

    def __init__(self, archive: Archive) -> None:
        self._archive = archive
        self._users = archive.user_names()
        self._conversations: list[Conversation] | None = None

    # -- names, mirroring Workspace ------------------------------------------

    def user_name(self, user_id: str | None) -> str:
        if not user_id:
            return "unknown"
        return self._users.get(user_id, user_id)

    def channel_name(self, channel_id: str) -> str:
        for conversation in self.all_conversations():
            if conversation.id == channel_id:
                return conversation.name.lstrip("#")
        return channel_id

    # -- conversations ---------------------------------------------------------

    def all_conversations(self) -> list[Conversation]:
        if self._conversations is not None:
            return self._conversations
        out: list[Conversation] = []
        for row in self._archive.conversation_rows():
            kind = str(row["kind"])
            name = str(row["name"])
            # Just enough raw for the shared resolution and readability rules
            # to mean what they mean against live data.
            raw = {
                "id": str(row["id"]),
                "name": name.lstrip("#@"),
                "is_member": bool(row["is_member"]),
                "is_im": kind == "dm",
                "is_mpim": kind == "group-dm",
                "is_private": kind == "private",
            }
            out.append(Conversation(id=str(row["id"]), kind=kind, raw=raw, name=name))
        self._conversations = out
        return out

    def readable_conversations(self) -> list[Conversation]:
        readable = [c for c in self.all_conversations() if is_readable(c.raw)]
        return sorted(readable, key=Conversation.sort_key)

    def resolve(self, spec: str) -> Conversation:
        return resolve_conversation(spec, self.all_conversations())

    def last_activity_map(self) -> dict[str, str]:
        return self._archive.last_activity_by_channel()

    # -- provenance --------------------------------------------------------------

    def provenance(self) -> str:
        """The trailer every archive-backed answer carries."""
        return f"from local archive, synced {self.synced_when()} — {PROVENANCE_HINT}"

    def synced_when(self) -> str:
        last = self._archive.get_meta("last_sync_at")
        return format_timestamp(last) if last else "never"

    # -- permalinks ----------------------------------------------------------------

    def permalink(self, conversation: Conversation, message: dict[str, Any]) -> str | None:
        """Composed exactly as the live backend composes it, from stored state."""
        team_url = self._archive.get_meta("team_url")
        if not team_url:
            return None
        ts = str(message.get("ts") or "")
        url = f"{team_url}/archives/{conversation.id}/p{ts.replace('.', '')}"
        thread_ts = message.get("thread_ts")
        if thread_ts:
            url = f"{url}?thread_ts={thread_ts}&cid={conversation.id}"
        return url

    # -- messages ---------------------------------------------------------------------

    def fetch_history(
        self,
        conversation: Conversation,
        *,
        oldest: str | None = None,
        latest: str | None = None,
        limit: int = 200,
        expand_threads: bool = True,
    ) -> FetchResult:
        """Mirror of ``Workspace.fetch_history`` over stored rows.

        Same shape by construction: newest ``limit`` messages in the window,
        oldest first, replies nested and counted against the cap, broadcast
        replies shown once. The row source is the only thing that differs.
        """
        result = FetchResult()
        low = float(oldest) if oldest else 0.0
        high = float(latest) if latest else time.time()

        parents: list[dict[str, Any]] = []
        replies_by_parent: dict[str, list[dict[str, Any]]] = {}
        seen_ts: set[str] = set()
        total = 0

        for row in self._archive.channel_level_messages(conversation.id, oldest=low, latest=high):
            ts = str(row["ts"])
            if ts in seen_ts:
                continue
            if total >= limit:
                result.truncated = True
                break
            seen_ts.add(ts)
            parents.append(json.loads(str(row["raw"])))
            total += 1

            if expand_threads and row["thread_ts"] == ts:
                reply_rows = self._archive.replies(conversation.id, ts)
                budget = limit - total
                fresh = [r for r in reply_rows if str(r["ts"]) not in seen_ts]
                if fresh and (budget <= 0 or len(fresh) > budget):
                    result.truncated = True
                taken = fresh[:budget] if budget > 0 else []
                for reply_row in taken:
                    seen_ts.add(str(reply_row["ts"]))
                replies_by_parent[ts] = [json.loads(str(r["raw"])) for r in taken]
                total += len(taken)

        parents.sort(key=lambda m: float(str(m.get("ts") or 0)))
        for parent in parents:
            result.entries.append(Entry(message=parent, conversation=conversation, depth=0))
            for reply in replies_by_parent.get(str(parent.get("ts")), []):
                result.entries.append(Entry(message=reply, conversation=conversation, depth=1))
        return result

    def fetch_thread(self, conversation: Conversation, thread_ts: str, *, limit: int = 200) -> FetchResult:
        result = FetchResult()
        parent_row = self._archive.message_row(conversation.id, thread_ts)
        reply_rows = self._archive.replies(conversation.id, thread_ts)
        if parent_row is None and not reply_rows:
            raise UsageError(
                f"no thread found at timestamp {thread_ts} in {conversation.name} in the archive — "
                f"it may not have been synced yet; {PROVENANCE_HINT}"
            )
        rows = ([parent_row] if parent_row is not None else []) + reply_rows
        if len(rows) > limit:
            rows = rows[:limit]
            result.truncated = True
        for index, row in enumerate(rows):
            result.entries.append(
                Entry(message=json.loads(str(row["raw"])), conversation=conversation, depth=0 if index == 0 else 1)
            )
        return result

    # -- search -------------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        conversations: list[Conversation],
        oldest: str | None = None,
        latest: str | None = None,
        from_user: str | None = None,
        limit: int = 200,
    ) -> FetchResult:
        """Substring search over the archive's rendered text.

        The full-text index only ever *narrows* what gets scanned: every
        candidate is re-verified in Python against the same predicate the
        scan path applies, so with and without FTS5 the answer is identical.
        """
        needle = query.strip().lower()
        if not needle:
            raise UsageError('no search query given — pass the text to look for, e.g. slack-scrollback search "budget"')

        result = FetchResult()
        low = float(oldest) if oldest else 0.0
        high = float(latest) if latest else time.time()
        wanted_user = from_user.lstrip("@").lower() if from_user else None
        by_id = {c.id: c for c in conversations}
        channel_ids = list(by_id)
        if not channel_ids:
            return result

        candidates, _ = self._archive.search_candidates(needle, channel_ids=channel_ids, oldest=low, latest=high)
        if self._archive.fts_unavailable_reason and len(needle) >= 3:
            result.notes.append(f"archive search fell back to a full scan — {self._archive.fts_unavailable_reason}")

        matches: list[Entry] = []
        for row in candidates:
            if not self._row_matches(row, needle, wanted_user):
                continue
            matches.append(Entry(message=json.loads(str(row["raw"])), conversation=by_id[str(row["channel_id"])]))

        if wanted_user and not matches:
            spoke = self._archive.sender_names_between(channel_ids=channel_ids, oldest=low, latest=high)
            result.notes.append(no_such_speaker_note(wanted_user, sorted(spoke)[:12]))

        if len(matches) > limit:
            matches = matches[-limit:]
            result.truncated = True
        result.entries = matches
        return result

    @staticmethod
    def _row_matches(row: Any, needle: str, wanted_user: str | None) -> bool:
        if row["subtype"] and str(row["subtype"]) in SEARCH_EXCLUDED_SUBTYPES:
            return False
        if needle not in str(row["text"]).lower():
            return False
        if wanted_user is None:
            return True
        user_id = str(row["user_id"] or "")
        if user_id and user_id.lower() == wanted_user:
            return True
        sender = str(row["sender_name"] or "").lower()
        return sender == wanted_user or wanted_user in sender
