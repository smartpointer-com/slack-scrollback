"""Reading a workspace: conversations, names, history, threads, search.

Everything here is fetched fresh per invocation and cached only in memory for
the life of the process. Nothing is written to disk.

Names are resolved lazily through ``users.info`` rather than by walking
``users.list``: the cost then scales with the number of distinct speakers
actually rendered instead of with the size of the workspace, which keeps a
10,000-member org from paying fifty pages of pagination to print one channel.
"""

from __future__ import annotations

import difflib
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from .api import DEFAULT_PAGE_LIMIT, SlackClient
from .errors import SlackApiError, UsageError

# Conversation kinds, in the order `channels` lists them.
KIND_PUBLIC = "public"
KIND_PRIVATE = "private"
KIND_GROUP_DM = "group-dm"
KIND_DM = "dm"

_KIND_ORDER = {KIND_PUBLIC: 0, KIND_PRIVATE: 1, KIND_GROUP_DM: 2, KIND_DM: 3}

_ALL_TYPES = "public_channel,private_channel,mpim,im"

#: Slackbot's user ID, identical in every workspace. Its DM is special-cased
#: by Slack itself: conversations.history answers channel_not_found to bot
#: tokens, always — there is no history there for a bot to read.
SLACKBOT_USER_ID = "USLACKBOT"

# Slack silently caps `limit` to 15 on conversations.history/replies for apps
# distributed outside the Marketplace. Asking for more and receiving exactly this
# many, with more still pending, is the signature of that cap.
_THROTTLE_CAP = 15

#: Subtypes `search` never matches: joins and leaves are room traffic, not speech.
SEARCH_EXCLUDED_SUBTYPES: frozenset[str] = frozenset({"channel_join", "channel_leave", "group_join", "group_leave"})


@dataclass(frozen=True)
class Conversation:
    """One readable conversation, normalised across Slack's four shapes."""

    id: str
    kind: str
    raw: dict[str, Any]
    name: str = ""
    member_count: int | None = None

    @property
    def is_archived(self) -> bool:
        return bool(self.raw.get("is_archived"))

    @property
    def counterpart_id(self) -> str | None:
        """For a DM, the other person's user ID."""
        user = self.raw.get("user")
        return str(user) if user else None

    def sort_key(self) -> tuple[int, str]:
        return (_KIND_ORDER.get(self.kind, 9), self.name.lower())


@dataclass
class Entry:
    """A message positioned for rendering.

    ``depth`` is 0 for a channel-level message and 1 for a thread reply, which is
    the only nesting Slack has.
    """

    message: dict[str, Any]
    conversation: Conversation
    depth: int = 0


@dataclass
class FetchResult:
    """Messages plus the honest caveats about what is missing from them."""

    entries: list[Entry] = field(default_factory=list)
    truncated: bool = False
    throttled: bool = False
    notes: list[str] = field(default_factory=list)


def _kind_of(raw: dict[str, Any]) -> str:
    if raw.get("is_im"):
        return KIND_DM
    if raw.get("is_mpim"):
        return KIND_GROUP_DM
    if raw.get("is_private"):
        return KIND_PRIVATE
    return KIND_PUBLIC


def is_readable(raw: dict[str, Any]) -> bool:
    """Whether the bot can actually fetch this conversation's history.

    Membership is the access boundary. Slack sets ``is_member`` on channels but
    not on DMs or group DMs, where the bot's presence is implied by the
    conversation existing at all — so testing ``is_member`` alone would silently
    discard every DM.
    """
    if raw.get("is_im") or raw.get("is_mpim"):
        return True
    return bool(raw.get("is_member"))


def resolve_conversation(spec: str, conversations: list[Conversation]) -> Conversation:
    """Turn ``#name``, ``name``, or an ID into one of ``conversations``.

    A miss names the closest alternatives, because the caller is usually a
    language model that will copy a suggestion straight into its retry. Both
    backends resolve through here, so a channel spec means the same thing
    against Slack and against the archive.
    """
    wanted = spec.strip()
    if not wanted:
        raise UsageError("no channel given — pass a name like '#general' or an ID like C0EXAMPLE1")

    for conversation in conversations:
        if conversation.id == wanted:
            return _require_readable(conversation)

    bare = wanted.lstrip("#@")
    lowered = bare.lower()
    for conversation in conversations:
        if str(conversation.raw.get("name") or "").lower() == lowered:
            return _require_readable(conversation)

    # DMs are addressed by the other person's name, which lives on the user
    # record rather than the conversation.
    dms = [c for c in conversations if c.kind == KIND_DM]
    for conversation in dms:
        if conversation.name.lstrip("@").lower() == lowered:
            return _require_readable(conversation)

    # "@alice" should reach "Alice Jones": people are named by whatever part
    # of the name the requester holds. Only an unambiguous hit counts —
    # guessing between two people would silently answer about the wrong one.
    partial = [c for c in dms if lowered and lowered in c.name.lstrip("@").lower()]
    if len(partial) == 1:
        return _require_readable(partial[0])
    if len(partial) > 1:
        names = ", ".join(sorted(c.name for c in partial))
        raise UsageError(f"{wanted!r} matches several DMs ({names}) — name the one you mean exactly")

    raise UsageError(_unknown_channel_message(wanted, conversations))


def _unknown_channel_message(wanted: str, conversations: list[Conversation]) -> str:
    readable = [c for c in conversations if is_readable(c.raw) and not c.is_archived]
    names = [c.name for c in sorted(readable, key=Conversation.sort_key)]
    close = difflib.get_close_matches(wanted.lstrip("#@"), [n.lstrip("#@") for n in names], n=3, cutoff=0.5)
    if close:
        suggestion = " or ".join(f"'{name}'" for name in close)
        return (
            f"no conversation matches {wanted!r} — did you mean {suggestion}? "
            f"Run 'slack-scrollback channels' to list every conversation this bot can read"
        )
    preview = ", ".join(names[:8]) if names else "(none — the bot has not been invited anywhere)"
    return (
        f"no conversation matches {wanted!r}. Readable conversations include: {preview}. "
        f"Run 'slack-scrollback channels' for the full list"
    )


def _require_readable(conversation: Conversation) -> Conversation:
    if is_readable(conversation.raw):
        return conversation
    raise UsageError(
        f"the bot can see {conversation.name} but is not a member, so Slack serves it no history — "
        f"invite the bot by typing '/invite @your-bot-name' in {conversation.name}, then retry"
    )


def no_such_speaker_note(wanted: str, names: list[str]) -> str:
    """Distinguish "said nothing" from "not spelt like that".

    An empty result for ``--from`` is ambiguous, and the ambiguity is
    expensive: the requester cannot tell whether the person was silent or
    merely misnamed, and has nothing to try differently. Naming who did speak
    resolves it and supplies the correction.
    """
    if not names:
        return f"nobody spoke in this window, so there is nothing from '{wanted}' to find"
    if any(wanted in name.lower() for name in names):
        return f"'{wanted}' spoke in this window, but said nothing matching the query"
    return (
        f"nobody matching '{wanted}' spoke in this window — people who did: {', '.join(names)}. "
        f"Re-run --from with one of those names, or drop --from"
    )


class Workspace:
    """Read-only view of everything the bot token can see."""

    def __init__(self, client: SlackClient) -> None:
        self._client = client
        self._users: dict[str, str] = {}
        self._channel_names: dict[str, str] = {}
        self._conversations: list[Conversation] | None = None
        self._team_url: str | None = None

    # -- names -------------------------------------------------------------

    def user_name(self, user_id: str | None) -> str:
        """Display name for a user ID, falling back through Slack's own chain.

        Slack documents no precedence rule, and notes that absent profile data
        "may be null or may contain the empty string" — so this tests truthiness
        rather than key presence, and ends at the raw ID so an unknown or deleted
        account still renders as something traceable.
        """
        if not user_id:
            return "unknown"
        cached = self._users.get(user_id)
        if cached is not None:
            return cached

        name = user_id
        try:
            body = self._client.call("users.info", user=user_id)
        except SlackApiError:
            # A deleted or invisible user must not abort a whole transcript.
            self._users[user_id] = name
            return name

        user = body.get("user") or {}
        profile = user.get("profile") or {}
        for candidate in (
            profile.get("display_name"),
            profile.get("real_name"),
            user.get("real_name"),
            user.get("name"),
        ):
            if candidate:
                name = str(candidate)
                break
        self._users[user_id] = name
        return name

    def prime_users(self, user_ids: Iterable[str]) -> None:
        """Resolve several user IDs, skipping any already cached."""
        for user_id in dict.fromkeys(user_ids):
            if user_id and user_id not in self._users:
                self.user_name(user_id)

    def seed_user_names(self, names: Mapping[str, str]) -> None:
        """Pre-fill the name cache, e.g. from an archive's users table.

        A seeded ID costs no request; anything absent still resolves through
        ``users.info`` on first use.
        """
        for user_id, name in names.items():
            self._users.setdefault(user_id, name)

    def known_user_names(self) -> dict[str, str]:
        """Every name resolved or seeded so far this run."""
        return dict(self._users)

    def channel_name(self, channel_id: str) -> str:
        """Name for a channel ID, for rendering ``<#C…>`` mentions.

        Messages routinely mention channels the bot was never invited to, so this
        falls back to ``conversations.info`` rather than leaving a raw ID in the
        text, and remembers misses so a mention repeated fifty times costs one
        request.
        """
        for conversation in self.all_conversations():
            if conversation.id == channel_id:
                return conversation.name.lstrip("#")
        cached = self._channel_names.get(channel_id)
        if cached is not None:
            return cached

        name = channel_id
        try:
            body = self._client.call("conversations.info", channel=channel_id)
        except SlackApiError:
            self._channel_names[channel_id] = name
            return name
        channel = body.get("channel") or {}
        if channel.get("name"):
            name = str(channel["name"])
        self._channel_names[channel_id] = name
        return name

    # -- conversations -----------------------------------------------------

    def all_conversations(self) -> list[Conversation]:
        """Every conversation Slack will admit to, readable or not (cached).

        Non-readable ones are kept so that naming one can produce a precise
        error rather than "not found".
        """
        if self._conversations is not None:
            return self._conversations

        conversations: list[Conversation] = []
        for raw in self._client.paginate(
            "conversations.list",
            "channels",
            limit=DEFAULT_PAGE_LIMIT,
            types=_ALL_TYPES,
            exclude_archived="false",
        ):
            conversations.append(self._to_conversation(raw))
        self._conversations = conversations
        return conversations

    def _to_conversation(self, raw: dict[str, Any]) -> Conversation:
        kind = _kind_of(raw)
        members: Any = raw.get("num_members")
        if kind == KIND_DM:
            # A DM has no name of its own; it is known by the other person. Slack
            # reports no member count for one either, and it is always two.
            name = f"@{self.user_name(raw.get('user'))}"
            members = 2
        elif kind == KIND_GROUP_DM:
            # Slack names group DMs "mpdm-alice--bob--carol-1"; the readable part
            # is in the middle.
            raw_name = str(raw.get("name") or "")
            inner = raw_name.removeprefix("mpdm-").removesuffix("-1")
            people = ", ".join(part for part in inner.split("--") if part)
            name = f"group DM: {people}" if people else str(raw.get("id"))
        else:
            name = f"#{raw.get('name')}"
        return Conversation(
            id=str(raw.get("id")),
            kind=kind,
            raw=raw,
            name=name,
            member_count=members if isinstance(members, int) else None,
        )

    def readable_conversations(self, *, include_archived: bool = False) -> list[Conversation]:
        """Conversations whose history the bot can actually fetch."""
        out = [c for c in self.all_conversations() if is_readable(c.raw)]
        if not include_archived:
            out = [c for c in out if not c.is_archived]
        return sorted(out, key=Conversation.sort_key)

    def resolve(self, spec: str) -> Conversation:
        """Turn ``#name``, ``name``, or an ID into a conversation."""
        return resolve_conversation(spec, self.all_conversations())

    def last_activity(self, conversation: Conversation) -> str | None:
        """Timestamp of the most recent message, or None if unavailable.

        Slack's conversation object carries an ``updated`` field, but it tracks
        channel metadata changes and drifts from real activity by months in both
        directions, so it is not usable as a proxy. This costs one request per
        conversation; conversations the bot cannot read simply report nothing.
        """
        try:
            body = self._client.call("conversations.history", channel=conversation.id, limit=1)
        except SlackApiError:
            return None
        messages = body.get("messages") or []
        if not messages:
            return None
        ts = messages[0].get("ts")
        return str(ts) if ts else None

    # -- messages ----------------------------------------------------------

    def history_pages(
        self,
        conversation: Conversation,
        *,
        oldest: str | None,
        latest: str | None,
        page_limit: int,
    ) -> Iterator[tuple[dict[str, Any], bool]]:
        """Yield ``(page, throttled)`` for a conversation's history, newest page first.

        ``latest`` is always sent, defaulting to now. Slack's documentation
        implies it defaults to the present, but an ``oldest`` without a ``latest``
        observably anchors paging at the *old* end of the window: the first page
        comes back from just after ``oldest`` and the newest messages are missing
        outright, not merely reordered. Supplying both bounds restores
        newest-first paging, so a window query cannot silently lose the messages
        most likely to be wanted.
        """
        first = True
        for body in self._client.iter_pages(
            "conversations.history",
            limit=page_limit,
            channel=conversation.id,
            oldest=oldest,
            latest=latest if latest is not None else f"{time.time():.6f}",
            inclusive="true",
        ):
            throttled = False
            if first:
                messages = body.get("messages") or []
                throttled = page_limit > _THROTTLE_CAP and len(messages) == _THROTTLE_CAP and bool(body.get("has_more"))
                first = False
            yield body, throttled

    def fetch_history(
        self,
        conversation: Conversation,
        *,
        oldest: str | None = None,
        latest: str | None = None,
        limit: int = 200,
        expand_threads: bool = True,
    ) -> FetchResult:
        """Newest ``limit`` messages in the window, oldest first, threads inline.

        ``conversations.history`` returns channel-level messages only — thread
        replies are reachable solely through ``conversations.replies`` — so each
        thread costs one extra request. The cap counts replies too, which bounds
        that fan-out.
        """
        result = FetchResult()
        parents: list[dict[str, Any]] = []
        replies_by_parent: dict[str, list[dict[str, Any]]] = {}
        seen_ts: set[str] = set()
        total = 0

        for page, throttled in self.history_pages(
            conversation, oldest=oldest, latest=latest, page_limit=min(max(limit, 1), 1000)
        ):
            result.throttled = result.throttled or throttled
            for message in page.get("messages") or []:
                ts = str(message.get("ts") or "")
                if not ts or ts in seen_ts:
                    continue
                if total >= limit:
                    result.truncated = True
                    break
                seen_ts.add(ts)
                parents.append(message)
                total += 1

                if expand_threads and self._has_thread(message):
                    budget = limit - total
                    # Filter before measuring: a thread_broadcast already rendered
                    # at channel level reappears in its parent's reply list, and
                    # counting it against the budget would report a truncation
                    # that never happened.
                    fresh = [
                        r
                        for r in self._fetch_replies(conversation, ts, budget=budget)
                        if str(r.get("ts")) not in seen_ts
                    ]
                    if budget <= 0 or len(fresh) > budget:
                        result.truncated = True
                    taken = fresh[:budget] if budget > 0 else []
                    for reply in taken:
                        seen_ts.add(str(reply.get("ts")))
                    replies_by_parent[ts] = taken
                    total += len(taken)
            if result.truncated or total >= limit:
                # Anything still pending beyond this point is being dropped.
                result.truncated = result.truncated or bool(page.get("has_more"))
                break

        parents.sort(key=lambda m: _ts_sort_key(m.get("ts")))
        for parent in parents:
            result.entries.append(Entry(message=parent, conversation=conversation, depth=0))
            for reply in sorted(
                replies_by_parent.get(str(parent.get("ts")), []),
                key=lambda m: _ts_sort_key(m.get("ts")),
            ):
                result.entries.append(Entry(message=reply, conversation=conversation, depth=1))

        self._prime_from_entries(result.entries)
        return result

    @staticmethod
    def _has_thread(message: dict[str, Any]) -> bool:
        """Whether this channel-level message starts a thread with replies.

        A parent is identified by ``thread_ts == ts``; ``reply_count`` is a
        decoration that can read 0 on a parent whose replies were deleted.
        """
        thread_ts = message.get("thread_ts")
        if not thread_ts or str(thread_ts) != str(message.get("ts")):
            return False
        return int(message.get("reply_count") or 0) > 0

    def _fetch_replies(self, conversation: Conversation, thread_ts: str, *, budget: int) -> list[dict[str, Any]]:
        """Replies to one thread, excluding the parent.

        Fetches one more than ``budget`` so the caller can tell whether the cap
        cut anything off, and leaves the capping to the caller — only the caller
        knows which of these replies it has already rendered.
        """
        if budget <= 0:
            return []
        try:
            # Two spare slots: one for the parent, which repeats as the first
            # element of its own reply list, and one to reveal that more replies
            # exist than the budget allows.
            fetched = list(
                self._client.paginate(
                    "conversations.replies",
                    "messages",
                    limit=min(budget + 2, 1000),
                    max_items=budget + 2,
                    channel=conversation.id,
                    ts=thread_ts,
                )
            )
        except SlackApiError:
            return []
        return [r for r in fetched if str(r.get("ts")) != str(thread_ts)]

    def fetch_thread(self, conversation: Conversation, thread_ts: str, *, limit: int = 200) -> FetchResult:
        """A whole thread, parent first."""
        result = FetchResult()
        messages = list(
            self._client.paginate(
                "conversations.replies",
                "messages",
                limit=min(limit + 1, 1000),
                max_items=limit + 1,
                channel=conversation.id,
                ts=thread_ts,
            )
        )
        if not messages:
            raise UsageError(
                f"no thread found at timestamp {thread_ts} in {conversation.name} — "
                f"check the permalink points at a message that has replies"
            )
        if len(messages) > limit:
            messages = messages[:limit]
            result.truncated = True

        messages.sort(key=lambda m: _ts_sort_key(m.get("ts")))
        for index, message in enumerate(messages):
            result.entries.append(Entry(message=message, conversation=conversation, depth=0 if index == 0 else 1))
        self._prime_from_entries(result.entries)
        return result

    def search(
        self,
        query: str,
        *,
        conversations: list[Conversation],
        oldest: str | None = None,
        latest: str | None = None,
        from_user: str | None = None,
        limit: int = 200,
        scan_per_conversation: int = 1000,
    ) -> FetchResult:
        """Find messages by scanning freshly-fetched history.

        Slack's ``search.*`` methods reject bot tokens outright, so matching
        happens here instead of server-side. Nothing is indexed or written: each
        call re-reads history through the same allowlisted method as ``history``.
        """
        needle = query.strip().lower()
        if not needle:
            raise UsageError('no search query given — pass the text to look for, e.g. slack-scrollback search "budget"')

        result = FetchResult()
        matches: list[Entry] = []
        wanted_user = from_user.lstrip("@").lower() if from_user else None
        authors: set[str] = set()

        # Every conversation is scanned before anything is discarded. Stopping
        # once `limit` matches exist would return whichever channels happen to be
        # scanned first rather than the most recent matches, while the trailer
        # claimed otherwise — the window (`--since`) is what bounds the work here,
        # not the output cap.
        for conversation in conversations:
            try:
                pages = self.history_pages(
                    conversation,
                    oldest=oldest,
                    latest=latest,
                    page_limit=min(scan_per_conversation, 1000),
                )
                scanned = 0
                for page, throttled in pages:
                    result.throttled = result.throttled or throttled
                    for message in page.get("messages") or []:
                        scanned += 1
                        if wanted_user and message.get("user"):
                            authors.add(str(message["user"]))
                        if self._matches(message, needle, wanted_user):
                            matches.append(Entry(message=message, conversation=conversation, depth=0))
                    if scanned >= scan_per_conversation and page.get("has_more"):
                        # Only worth saying when something was actually left
                        # unread; a conversation scanned to exhaustion has no
                        # older messages to warn about.
                        result.notes.append(
                            f"{conversation.name}: stopped after scanning {scanned} messages; "
                            f"anything older in this window was not searched"
                        )
                        break
                    if scanned >= scan_per_conversation:
                        break
            except SlackApiError:
                # A conversation that refuses history (the Slackbot DM does)
                # should not abort a workspace-wide search.
                continue

        if wanted_user and not matches:
            result.notes.append(self._no_such_speaker_note(wanted_user, authors))

        matches.sort(key=lambda e: _ts_sort_key(e.message.get("ts")))
        if len(matches) > limit:
            matches = matches[-limit:]
            result.truncated = True
        result.entries = matches
        self._prime_from_entries(result.entries)
        return result

    def _matches(self, message: dict[str, Any], needle: str, wanted_user: str | None) -> bool:
        if str(message.get("subtype") or "") in SEARCH_EXCLUDED_SUBTYPES:
            return False
        text = str(message.get("text") or "")
        if needle not in text.lower():
            return False
        if wanted_user:
            return self._is_speaker(message, wanted_user)
        return True

    def _no_such_speaker_note(self, wanted: str, authors: set[str]) -> str:
        # The scan already resolved (and cached) any author it tested, so this
        # costs nothing for names already seen; the slice bounds the rest.
        names = sorted({self.user_name(user_id) for user_id in sorted(authors)[:12]})
        return no_such_speaker_note(wanted, names)

    def _is_speaker(self, message: dict[str, Any], wanted: str) -> bool:
        """Whether ``wanted`` names this message's author.

        People are asked for by whatever fragment of a name the requester has —
        "alice" for "Alice Jones", a first name for a full one. Demanding an
        exact display name would answer "nothing found", which reads as "she said
        nothing" rather than "that is not how she is spelt here", and there is
        nothing in that answer to correct on the next attempt.
        """
        user_id = message.get("user")
        if user_id:
            if str(user_id).lower() == wanted:
                return True
            name = self.user_name(str(user_id)).lower()
            return name == wanted or wanted in name
        for candidate in (message.get("username"), (message.get("bot_profile") or {}).get("name")):
            if candidate and wanted in str(candidate).lower():
                return True
        return False

    def _prime_from_entries(self, entries: list[Entry]) -> None:
        self.prime_users(str(e.message.get("user")) for e in entries if e.message.get("user"))

    # -- permalinks --------------------------------------------------------

    def team_url(self) -> str:
        """The workspace's base URL, e.g. ``https://acme.slack.com``."""
        if self._team_url is None:
            body = self._client.call("auth.test")
            self._team_url = str(body.get("url") or "").rstrip("/")
        return self._team_url

    def permalink(self, conversation: Conversation, message: dict[str, Any]) -> str:
        """Build a message's permalink.

        Slack's ``chat.getPermalink`` would answer this authoritatively but costs
        one request per message, which makes ``--links`` unusable over a few
        hundred messages. The URL is a pure function of workspace URL, channel and
        timestamp, so it is composed here from a single ``auth.test`` instead —
        verified to match ``chat.getPermalink`` byte-for-byte for plain messages,
        thread parents and replies.

        Anything in a thread — parent included, not just replies — carries the
        ``thread_ts``/``cid`` query that opens the thread pane.
        """
        ts = str(message.get("ts") or "")
        url = f"{self.team_url()}/archives/{conversation.id}/p{ts.replace('.', '')}"
        thread_ts = message.get("thread_ts")
        if thread_ts:
            url = f"{url}?thread_ts={thread_ts}&cid={conversation.id}"
        return url


def _ts_sort_key(ts: Any) -> tuple[int, int]:
    """Order Slack timestamps without going through a float.

    ``ts`` is ``<seconds>.<6-digit sequence>``; parsing it as a float loses the
    low digits and with them message identity.
    """
    text = str(ts or "0")
    seconds, _, fraction = text.partition(".")
    try:
        return (int(seconds), int(fraction or 0))
    except ValueError:
        return (0, 0)
