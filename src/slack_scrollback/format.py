"""Rendering messages for a language model to read.

The default form is one message per line — ``[YYYY-MM-DD HH:MM] name: text`` —
with thread replies indented by two spaces. It is deliberately plain: the reader
is a small model with a few thousand tokens to spend, so every line is a fact and
nothing is decoration.

Timestamps render in the local timezone, matching what the Slack client shows the
people who wrote the messages.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from .workspace import Conversation, Entry, FetchResult

#: Slack wraps every special reference in angle brackets.
_ENTITY_RE = re.compile(r"<(.*?)>")

_INDENT = "  "

NameLookup = Callable[[str], str]


def unescape(text: str) -> str:
    """Undo Slack's escaping — and only Slack's.

    Slack escapes exactly three characters. A general HTML unescaper would also
    eat sequences like ``&nbsp;`` or ``&#39;`` that are literal text somebody
    typed, corrupting the message. ``&amp;`` is resolved last so that ``&amp;lt;``
    (a user who wrote "&lt;") survives as ``&lt;`` rather than collapsing to ``<``.
    """
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _render_entity(body: str, resolve_user: NameLookup, resolve_channel: NameLookup) -> str:
    """Flatten one ``<...>`` reference.

    Slack escapes ``&``, ``<`` and ``>`` inside a reference's label and URL just
    as it does in ordinary text, so anything taken verbatim from the reference is
    unescaped on the way out. Names that came from a lookup are already plain and
    are left alone.
    """
    if body.startswith("@"):
        ref, _, label = body[1:].partition("|")
        return f"@{unescape(label)}" if label else f"@{resolve_user(ref)}"
    if body.startswith("#"):
        ref, _, label = body[1:].partition("|")
        return f"#{unescape(label)}" if label else f"#{resolve_channel(ref)}"
    if body.startswith("!"):
        inner = body[1:]
        if inner in ("here", "channel", "everyone"):
            return f"@{inner}"
        if inner.startswith("subteam^"):
            _, _, label = inner.partition("|")
            return unescape(label) if label else "@group"
        if inner.startswith("date^"):
            # <!date^1392734382^{date_short}|Feb 18, 2014> — the authored
            # fallback after the final pipe is the only human-readable part.
            _, sep, fallback = inner.rpartition("|")
            return unescape(fallback) if sep else unescape(inner)
        _, _, label = inner.partition("|")
        return unescape(label) if label else unescape(inner)
    url, sep, label = body.partition("|")
    return unescape(label) if sep and label else unescape(url)


def render_text(text: str, *, resolve_user: NameLookup, resolve_channel: NameLookup) -> str:
    """Flatten Slack's mrkdwn into plain text.

    ``<...>`` references are resolved first and the three escapes undone second.
    Reversing that order would turn text where somebody literally typed
    ``&lt;@U0EXAMPLE1&gt;`` into a live-looking mention.
    """
    if not text:
        return ""
    pieces = _ENTITY_RE.split(text)
    out: list[str] = []
    for index, piece in enumerate(pieces):
        if index % 2:
            out.append(_render_entity(piece, resolve_user, resolve_channel))
        else:
            out.append(unescape(piece))
    return "".join(out)


def format_timestamp(ts: str) -> str:
    """``ts`` -> ``YYYY-MM-DD HH:MM`` in local time."""
    seconds, _, _ = str(ts or "0").partition(".")
    try:
        moment = dt.datetime.fromtimestamp(int(seconds))
    except (ValueError, OverflowError, OSError):
        return "????-??-?? ??:??"
    return moment.strftime("%Y-%m-%d %H:%M")


def iso_timestamp(ts: str) -> str:
    seconds, _, _ = str(ts or "0").partition(".")
    try:
        moment = dt.datetime.fromtimestamp(int(seconds)).astimezone()
    except (ValueError, OverflowError, OSError):
        return ""
    return moment.isoformat()


def speaker(message: dict[str, Any], resolve_user: NameLookup) -> str:
    """Who said it.

    Bot posts carry no ``user``; ``username`` is Slack's documented per-message
    override, so it wins over the app's own name.
    """
    user_id = message.get("user")
    if user_id:
        return resolve_user(str(user_id))
    for candidate in (
        message.get("username"),
        (message.get("bot_profile") or {}).get("name"),
        message.get("bot_id"),
    ):
        if candidate:
            return str(candidate)
    return "unknown"


def file_summaries(message: dict[str, Any]) -> list[str]:
    """``[file: name]`` per attachment.

    Every field but ``id`` is optional: Slack degrades files that fall outside a
    workspace's retention to a stub, dropping ``name`` and the rest.
    """
    out: list[str] = []
    for entry in message.get("files") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("title") or "unavailable file"
        out.append(f"[file: {name}]")
    return out


def attachment_summaries(
    message: dict[str, Any], *, resolve_user: NameLookup, resolve_channel: NameLookup
) -> list[str]:
    """Legacy attachments, preferring the plain-text summary Slack ships for them."""
    out: list[str] = []
    for entry in message.get("attachments") or []:
        if not isinstance(entry, dict):
            continue
        fallback = entry.get("fallback")
        if fallback:
            out.append(f"[attachment: {unescape(str(fallback))}]")
            continue
        parts = [str(entry.get(key)) for key in ("author_name", "title", "text") if entry.get(key)]
        if parts:
            rendered = render_text(" — ".join(parts), resolve_user=resolve_user, resolve_channel=resolve_channel)
            out.append(f"[attachment: {rendered}]")
    return out


def message_body(entry: Entry, *, resolve_user: NameLookup, resolve_channel: NameLookup) -> str:
    """Everything the message says, on one line."""
    message = entry.message
    text = render_text(str(message.get("text") or ""), resolve_user=resolve_user, resolve_channel=resolve_channel)
    parts = [text] if text else []
    parts += file_summaries(message)
    parts += attachment_summaries(message, resolve_user=resolve_user, resolve_channel=resolve_channel)
    body = " ".join(part for part in parts if part)
    body = " ".join(body.split())
    return body or "(no text)"


def format_entry(
    entry: Entry,
    *,
    resolve_user: NameLookup,
    resolve_channel: NameLookup,
    show_channel: bool = False,
    permalink: str | None = None,
) -> str:
    """One message as a single line."""
    indent = _INDENT * entry.depth
    when = format_timestamp(str(entry.message.get("ts") or ""))
    who = speaker(entry.message, resolve_user)
    where = f"{entry.conversation.name} " if show_channel else ""
    body = message_body(entry, resolve_user=resolve_user, resolve_channel=resolve_channel)
    line = f"{indent}[{when}] {where}{who}: {body}"
    if permalink:
        line = f"{line} {permalink}"
    return line


def entry_to_dict(
    entry: Entry,
    *,
    resolve_user: NameLookup,
    resolve_channel: NameLookup,
    permalink: str | None = None,
) -> dict[str, Any]:
    """A message as stable JSON.

    ``ts`` stays the verbatim Slack string: it is the message's identity, and a
    float round-trip silently destroys its low digits.
    """
    message = entry.message
    user_id = message.get("user")
    return {
        "type": "message",
        "ts": str(message.get("ts") or ""),
        "time": iso_timestamp(str(message.get("ts") or "")),
        "channel": entry.conversation.name,
        "channel_id": entry.conversation.id,
        "user_id": str(user_id) if user_id else None,
        "user": speaker(message, resolve_user),
        "text": message_body(entry, resolve_user=resolve_user, resolve_channel=resolve_channel),
        "thread_ts": str(message.get("thread_ts")) if message.get("thread_ts") else None,
        "is_reply": entry.depth > 0,
        "subtype": message.get("subtype"),
        "files": [f.get("name") for f in message.get("files") or [] if isinstance(f, dict)],
        "permalink": permalink,
    }


def link_key(entry: Entry) -> tuple[str, str]:
    """Identify a message across conversations.

    A Slack ``ts`` is unique only within one channel, so search results spanning
    channels can collide on it. The conversation is part of the identity.
    """
    return (entry.conversation.id, str(entry.message.get("ts") or ""))


def truncation_notice(*, limit: int) -> str:
    """Tell the reader it did not get everything, and how to get more."""
    return (
        f"[truncated: showing the newest {limit} messages; older ones in this window were not fetched. "
        f"Raise the cap with --limit {limit * 5}, or narrow the window with --since 24h]"
    )


def throttle_notice() -> str:
    return (
        "[note: Slack capped this request to 15 messages, which means this app is rate-limited to "
        "1 request/minute — apps distributed outside the Slack Marketplace are throttled. "
        "Large fetches will be very slow; see the README section 'Rate limits']"
    )


def _notices(result: FetchResult, *, limit: int) -> list[str]:
    """Everything the caller must know beyond the messages themselves."""
    out = [f"note: {note}" for note in result.notes]
    if result.throttled:
        out.append(throttle_notice().strip("[]"))
    if result.truncated:
        out.append(truncation_notice(limit=limit).strip("[]"))
    return out


def render_messages(
    result: FetchResult,
    *,
    resolve_user: NameLookup,
    resolve_channel: NameLookup,
    limit: int,
    show_channel: bool = False,
    as_json: bool = False,
    permalinks: dict[tuple[str, str], str] | None = None,
) -> list[str]:
    """The whole output for a message-listing command, trailers included.

    The trailers are not decoration: they are how the reader learns the answer is
    incomplete. They therefore appear in both renderings — JSON emits them as
    ``{"type": "notice"}`` records, discriminated from messages by ``type``, so a
    programmatic consumer cannot mistake a truncated read for a complete one.
    """
    links = permalinks or {}
    lines: list[str] = []

    if as_json:
        for entry in result.entries:
            payload = entry_to_dict(
                entry,
                resolve_user=resolve_user,
                resolve_channel=resolve_channel,
                permalink=links.get(link_key(entry)),
            )
            lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        for notice in _notices(result, limit=limit):
            lines.append(
                json.dumps(
                    {"type": "notice", "truncated": result.truncated, "throttled": result.throttled, "text": notice},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return lines

    if not result.entries:
        lines.append("(no messages found)")
    for entry in result.entries:
        lines.append(
            format_entry(
                entry,
                resolve_user=resolve_user,
                resolve_channel=resolve_channel,
                show_channel=show_channel,
                permalink=links.get(link_key(entry)),
            )
        )
    lines.extend(f"[{notice}]" for notice in _notices(result, limit=limit))
    return lines


def render_channels(rows: Iterable[tuple[Conversation, str | None]], *, as_json: bool = False) -> list[str]:
    """The `channels` table: id, name, members, last activity."""
    materialised = list(rows)
    if as_json:
        return [
            json.dumps(
                {
                    "id": conversation.id,
                    "name": conversation.name,
                    "kind": conversation.kind,
                    "members": conversation.member_count,
                    "last_activity": iso_timestamp(last) if last else None,
                    "last_activity_ts": last,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            for conversation, last in materialised
        ]

    if not materialised:
        return [
            "(no readable conversations — invite the bot to a channel with '/invite @your-bot-name', "
            "or check the token's scopes)"
        ]

    name_width = max(len(c.name) for c, _ in materialised)
    name_width = min(max(name_width, 4), 40)
    lines = [f"{'CHANNEL'.ljust(name_width)}  {'KIND':<9}  {'MEMBERS':>7}  LAST ACTIVITY        ID"]
    for conversation, last in materialised:
        members = str(conversation.member_count) if conversation.member_count is not None else "-"
        when = format_timestamp(last) if last else "-"
        lines.append(
            f"{conversation.name[:name_width].ljust(name_width)}  {conversation.kind:<9}  "
            f"{members:>7}  {when:<19}  {conversation.id}"
        )
    return lines
