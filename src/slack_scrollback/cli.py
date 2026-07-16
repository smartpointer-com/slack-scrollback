"""Command-line surface.

Six subcommands, each of which answers a whole question in one invocation:
``channels`` to discover, ``history`` to read, ``thread`` to follow a
conversation, ``search`` to find, ``sync`` to maintain the local archive, and
``file`` to turn a file reference into local bytes. Small models fall off a
cliff when a task requires chaining calls, so no subcommand exists only as a
step towards another.

Reads have two backends — Slack itself, and the archive ``sync`` maintains —
with one rule per command rather than a choice the caller must make:
``history`` and ``thread`` default to live because freshness is their point;
``search`` prefers the archive when one exists because whole-history search is
what an archive is for; ``channels`` lists live but takes its activity column
from the archive to avoid a request per conversation. ``--live`` and
``--archive`` override in either direction, and archive-backed output always
ends with a provenance trailer, so nobody mistakes local disk for Slack.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .api import SlackClient
from .archive import Archive, archive_exists, sync_lock
from .config import resolve_archive_dir, resolve_media_settings, resolve_token
from .download import download_to, sanitized_filename
from .errors import ScrollbackError, UsageError
from .format import LocalPathLookup, link_key, render_channels, render_messages
from .localread import ArchiveReader
from .syncer import DEFAULT_RECHECK, Syncer, render_sync_report
from .timeparse import parse_duration, parse_time, to_slack_ts
from .workspace import Conversation, Entry, Workspace

DEFAULT_LIMIT = 200
DEFAULT_SEARCH_WINDOW = "30d"

# https://acme.slack.com/archives/C0EXAMPLE1/p1700000000123456
_PERMALINK_RE = re.compile(r"/archives/(?P<channel>[A-Za-z0-9]+)/p(?P<ts>\d{10,})")
_TS_RE = re.compile(r"\d{10,}\.\d{1,6}")
_FILE_ID_RE = re.compile(r"F[A-Z0-9]{8,}")


def _add_common(parser: argparse.ArgumentParser, *, links: bool = True) -> None:
    parser.add_argument(
        "--token",
        metavar="XOXB",
        help="Slack bot token (else $SLACK_BOT_TOKEN, else the config file)",
    )
    parser.add_argument("--config", metavar="PATH", help="config file (default: ~/.config/slack-scrollback.cfg)")
    parser.add_argument(
        "--archive-dir",
        metavar="PATH",
        help="the local archive's directory (default: ~/.local/share/slack-scrollback)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSONL instead of text")
    if links:
        parser.add_argument("--links", action="store_true", help="append each message's Slack permalink")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SEC",
        help="per-request timeout (default: 30)",
    )


def _add_backend_flags(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--live", action="store_true", help="read Slack directly, ignoring the local archive")
    group.add_argument("--archive", action="store_true", help="read only the local archive, touching no network")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slack-scrollback",
        description="Read Slack history and search it, read-only, with a bot token.",
    )
    parser.add_argument("--version", action="version", version=f"slack-scrollback {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    channels = subparsers.add_parser(
        "channels",
        help="list conversations the bot can read",
        description="List every conversation the bot can read, most recently active first.",
    )
    _add_common(channels)
    _add_backend_flags(channels)
    channels.add_argument(
        "--no-activity",
        action="store_true",
        help="skip the last-activity column entirely",
    )
    channels.set_defaults(handler=cmd_channels)

    history = subparsers.add_parser(
        "history",
        help="read a conversation's messages",
        description="Fetch messages from one conversation, oldest first, with thread replies inline.",
    )
    _add_common(history)
    _add_backend_flags(history)
    history.add_argument("channel", help="channel name (#general or general), DM (@alice), or ID (C0EXAMPLE1)")
    history.add_argument("--since", metavar="WHEN", help="only messages after this (7d, 24h, today, 2026-01-31)")
    history.add_argument(
        "--until",
        metavar="WHEN",
        help="only messages before this (a bare date includes that whole day)",
    )
    history.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=f"max messages (default: {DEFAULT_LIMIT})",
    )
    history.add_argument("--no-threads", action="store_true", help="do not fetch thread replies")
    history.set_defaults(handler=cmd_history)

    thread = subparsers.add_parser(
        "thread",
        help="read one thread in full",
        description="Fetch a whole thread from a permalink, or from a conversation plus a timestamp.",
    )
    _add_common(thread)
    _add_backend_flags(thread)
    thread.add_argument(
        "target",
        nargs="+",
        metavar="TARGET",
        help="a Slack permalink, or a channel followed by the thread timestamp",
    )
    thread.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=f"max messages (default: {DEFAULT_LIMIT})",
    )
    thread.set_defaults(handler=cmd_thread)

    search = subparsers.add_parser(
        "search",
        help="find messages containing text",
        description=(
            "Find messages containing text. Uses the local archive when one exists "
            "(whole history, instant); otherwise reads history and matches locally."
        ),
    )
    _add_common(search)
    _add_backend_flags(search)
    search.add_argument("query", help="text to look for (case-insensitive)")
    search.add_argument("--in", dest="in_channel", metavar="CHANNEL", help="search one conversation instead of all")
    search.add_argument("--from", dest="from_user", metavar="USER", help="only messages by this person (@alice)")
    search.add_argument(
        "--since",
        metavar="WHEN",
        default=DEFAULT_SEARCH_WINDOW,
        help=f"how far back to read (default: {DEFAULT_SEARCH_WINDOW})",
    )
    search.add_argument("--until", metavar="WHEN", help="stop at this point")
    search.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=f"max matches (default: {DEFAULT_LIMIT})",
    )
    search.set_defaults(handler=cmd_search)

    sync = subparsers.add_parser(
        "sync",
        help="update the local archive",
        description=(
            "Mirror every readable conversation into the local archive: messages, threads, "
            "edits, deletions, and file bytes. An incremental run (the default) reads each "
            "conversation from wherever the previous run left off, or from --recheck ago if "
            "that is further back — re-reading that recent stretch is how edits and deletions "
            "are noticed. The only command that writes anything."
        ),
    )
    _add_common(sync, links=False)
    sync.add_argument(
        "--full",
        action="store_true",
        help="re-read all history Slack still serves, instead of the incremental window described above",
    )
    sync.add_argument(
        "--recheck",
        metavar="WHEN",
        default=DEFAULT_RECHECK,
        help=f"how far back an incremental run re-reads settled history for edits and deletions "
        f"(default: {DEFAULT_RECHECK})",
    )
    sync.add_argument(
        "--media",
        metavar="LIST",
        help="file tiers to download: comma-separated from documents,images,audio,video — or 'none' "
        "(default: documents,images)",
    )
    sync.add_argument(
        "--media-max-bytes",
        type=int,
        metavar="N",
        help="skip files larger than this many bytes (default: no limit)",
    )
    sync.set_defaults(handler=cmd_sync)

    file_cmd = subparsers.add_parser(
        "file",
        help="get a shared file's bytes as a local path",
        description=(
            "Turn a file ID or file permalink into a local path: from the archive when it has "
            "the bytes, downloaded from Slack otherwise."
        ),
    )
    _add_common(file_cmd, links=False)
    file_cmd.add_argument("target", metavar="TARGET", help="a file ID (F0EXAMPLE1) or any Slack URL containing one")
    file_cmd.add_argument("--out", metavar="PATH", help="where a live download lands (default: current directory)")
    file_cmd.add_argument(
        "--live", action="store_true", help="download fresh from Slack even if the archive has the bytes"
    )
    file_cmd.set_defaults(handler=cmd_file)

    return parser


# -- shared plumbing -------------------------------------------------------


def _config_path(args: argparse.Namespace) -> Path | None:
    return Path(args.config) if args.config else None


def _workspace(args: argparse.Namespace) -> Workspace:
    token = resolve_token(flag=args.token, config_path=_config_path(args))
    return Workspace(SlackClient(token, timeout=args.timeout))


def _archive_dir(args: argparse.Namespace) -> Path:
    return resolve_archive_dir(flag=args.archive_dir, config_path=_config_path(args))


def _enrichment(args: argparse.Namespace) -> LocalPathLookup | None:
    """The archive's bytes-on-disk lookup, when an archive exists.

    Live reads use it too: ``local_path`` in JSON output is about where bytes
    are, which is a fact about this machine, not about the backend asked.
    Enrichment is auxiliary by contract — a miss is null, never an error — so
    an archive that cannot be opened degrades a live answer to unenriched,
    with a stderr note, rather than failing it.
    """
    directory = _archive_dir(args)
    if not archive_exists(directory):
        return None
    try:
        return Archive.open_ro(directory).local_path_of
    except (ScrollbackError, sqlite3.Error):
        print(
            f"note: the archive at {directory} could not be read; file paths are omitted from this answer",
            file=sys.stderr,
        )
        return None


def _window(args: argparse.Namespace) -> tuple[str | None, str | None]:
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    oldest = to_slack_ts(parse_time(since, flag="--since")) if since else None
    latest = to_slack_ts(parse_time(until, flag="--until", upper_bound=True)) if until else None
    if oldest and latest and float(oldest) > float(latest):
        raise UsageError("--since is later than --until — swap them, or drop one of the two")
    return oldest, latest


def _lookups(workspace: Workspace) -> tuple[Any, Any]:
    """Name resolvers for the formatter, backed by the per-run caches."""
    return workspace.user_name, workspace.channel_name


def _permalinks(workspace: Workspace, entries: list[Entry], *, wanted: bool) -> dict[tuple[str, str], str]:
    if not wanted:
        return {}
    return {link_key(e): workspace.permalink(e.conversation, e.message) for e in entries}


def _reader_permalinks(reader: ArchiveReader, entries: list[Entry], *, wanted: bool) -> dict[tuple[str, str], str]:
    if not wanted:
        return {}
    out: dict[tuple[str, str], str] = {}
    for entry in entries:
        url = reader.permalink(entry.conversation, entry.message)
        if url:
            out[link_key(entry)] = url
    return out


def _emit(lines: list[str]) -> int:
    for line in lines:
        print(line)
    return 0


def _limit_of(args: argparse.Namespace) -> int:
    limit = int(getattr(args, "limit", DEFAULT_LIMIT))
    if limit < 1:
        raise UsageError(f"--limit must be at least 1, not {limit}")
    return limit


# -- commands --------------------------------------------------------------


def cmd_channels(args: argparse.Namespace) -> int:
    directory = _archive_dir(args)
    rows: list[tuple[Conversation, str | None]] = []
    provenance: str | None = None

    if args.archive:
        reader = ArchiveReader(Archive.open_ro(directory))
        activity = {} if args.no_activity else reader.last_activity_map()
        rows = [(c, activity.get(c.id)) for c in reader.readable_conversations()]
        provenance = reader.provenance()
    else:
        workspace = _workspace(args)
        conversations = workspace.readable_conversations()
        use_archive_activity = not args.live and not args.no_activity and archive_exists(directory)
        if use_archive_activity:
            # The whole reason the archive feeds this column: live "last
            # activity" costs one request per conversation, every time.
            reader = ArchiveReader(Archive.open_ro(directory))
            activity = reader.last_activity_map()
            rows = [(c, activity.get(c.id)) for c in conversations]
            provenance = (
                f"last activity from local archive, synced {reader.synced_when()} — pass --live to read Slack directly"
            )
        else:
            for conversation in conversations:
                last = None if args.no_activity else workspace.last_activity(conversation)
                rows.append((conversation, last))

    if not args.no_activity:
        rows.sort(key=lambda row: (row[1] is None, _most_recent_first(row[1])))
    return _emit(render_channels(rows, as_json=args.json, provenance=provenance))


def _most_recent_first(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return -float(str(ts).partition(".")[0])
    except ValueError:
        return 0.0


def cmd_history(args: argparse.Namespace) -> int:
    limit = _limit_of(args)
    oldest, latest = _window(args)

    if args.archive:
        archive = Archive.open_ro(_archive_dir(args))
        reader = ArchiveReader(archive)
        conversation = reader.resolve(args.channel)
        result = reader.fetch_history(
            conversation, oldest=oldest, latest=latest, limit=limit, expand_threads=not args.no_threads
        )
        return _emit(
            render_messages(
                result,
                resolve_user=reader.user_name,
                resolve_channel=reader.channel_name,
                limit=limit,
                as_json=args.json,
                permalinks=_reader_permalinks(reader, result.entries, wanted=args.links),
                local_path_of=archive.local_path_of,
                provenance=reader.provenance(),
            )
        )

    workspace = _workspace(args)
    conversation = workspace.resolve(args.channel)
    result = workspace.fetch_history(
        conversation,
        oldest=oldest,
        latest=latest,
        limit=limit,
        expand_threads=not args.no_threads,
    )
    resolve_user, resolve_channel = _lookups(workspace)
    return _emit(
        render_messages(
            result,
            resolve_user=resolve_user,
            resolve_channel=resolve_channel,
            limit=limit,
            as_json=args.json,
            permalinks=_permalinks(workspace, result.entries, wanted=args.links),
            local_path_of=_enrichment(args),
        )
    )


def parse_thread_target(target: list[str]) -> tuple[str, str]:
    """Accept a permalink, or a channel plus a timestamp."""
    if len(target) == 1:
        return _parse_permalink(target[0])
    if len(target) == 2:
        channel, ts = target
        if not _TS_RE.fullmatch(ts):
            raise UsageError(
                f"{ts!r} is not a Slack thread timestamp — one looks like 1700000000.123456. "
                f"Use Slack's 'Copy link' on the message and pass that permalink instead"
            )
        return channel, ts
    raise UsageError(
        "pass either a permalink "
        "(slack-scrollback thread https://acme.slack.com/archives/C0EXAMPLE1/p1700000000123456) "
        "or a channel and a timestamp (slack-scrollback thread '#general' 1700000000.123456)"
    )


def _parse_permalink(value: str) -> tuple[str, str]:
    match = _PERMALINK_RE.search(value)
    if not match:
        raise UsageError(
            f"cannot read {value!r} as a Slack permalink — one looks like "
            f"https://acme.slack.com/archives/C0EXAMPLE1/p1700000000123456 (use Slack's 'Copy link' on a message)"
        )
    digits = match.group("ts")
    ts = f"{digits[:-6]}.{digits[-6:]}"
    # A permalink to a reply carries its parent's timestamp, and the parent is
    # the thread worth showing.
    parent = re.search(r"[?&]thread_ts=([0-9.]+)", value)
    return match.group("channel"), parent.group(1) if parent else ts


def cmd_thread(args: argparse.Namespace) -> int:
    limit = _limit_of(args)
    channel_spec, thread_ts = parse_thread_target(args.target)

    if args.archive:
        archive = Archive.open_ro(_archive_dir(args))
        reader = ArchiveReader(archive)
        conversation = reader.resolve(channel_spec)
        result = reader.fetch_thread(conversation, thread_ts, limit=limit)
        return _emit(
            render_messages(
                result,
                resolve_user=reader.user_name,
                resolve_channel=reader.channel_name,
                limit=limit,
                as_json=args.json,
                permalinks=_reader_permalinks(reader, result.entries, wanted=args.links),
                local_path_of=archive.local_path_of,
                provenance=reader.provenance(),
            )
        )

    workspace = _workspace(args)
    conversation = workspace.resolve(channel_spec)
    result = workspace.fetch_thread(conversation, thread_ts, limit=limit)
    resolve_user, resolve_channel = _lookups(workspace)
    return _emit(
        render_messages(
            result,
            resolve_user=resolve_user,
            resolve_channel=resolve_channel,
            limit=limit,
            as_json=args.json,
            permalinks=_permalinks(workspace, result.entries, wanted=args.links),
            local_path_of=_enrichment(args),
        )
    )


def cmd_search(args: argparse.Namespace) -> int:
    limit = _limit_of(args)
    oldest, latest = _window(args)
    directory = _archive_dir(args)

    # The one auto-fallback in the tool, and it only falls in one direction:
    # no archive means live (exactly the stateless behaviour of a host the
    # zipapp was merely copied to). An existing archive is preferred because
    # whole-history substring search is the thing live search cannot be.
    use_archive = args.archive or (not args.live and archive_exists(directory))

    if use_archive:
        archive = Archive.open_ro(directory)
        reader = ArchiveReader(archive)
        conversations = [reader.resolve(args.in_channel)] if args.in_channel else reader.readable_conversations()
        result = reader.search(
            args.query,
            conversations=conversations,
            oldest=oldest,
            latest=latest,
            from_user=args.from_user,
            limit=limit,
        )
        return _emit(
            render_messages(
                result,
                resolve_user=reader.user_name,
                resolve_channel=reader.channel_name,
                limit=limit,
                show_channel=True,
                as_json=args.json,
                permalinks=_reader_permalinks(reader, result.entries, wanted=args.links),
                local_path_of=archive.local_path_of,
                provenance=reader.provenance(),
            )
        )

    workspace = _workspace(args)
    conversations = [workspace.resolve(args.in_channel)] if args.in_channel else workspace.readable_conversations()
    result = workspace.search(
        args.query,
        conversations=conversations,
        oldest=oldest,
        latest=latest,
        from_user=args.from_user,
        limit=limit,
    )
    resolve_user, resolve_channel = _lookups(workspace)
    return _emit(
        render_messages(
            result,
            resolve_user=resolve_user,
            resolve_channel=resolve_channel,
            limit=limit,
            show_channel=True,
            as_json=args.json,
            permalinks=_permalinks(workspace, result.entries, wanted=args.links),
            local_path_of=_enrichment(args),
        )
    )


def cmd_sync(args: argparse.Namespace) -> int:
    directory = _archive_dir(args)
    tiers, max_bytes = resolve_media_settings(
        tiers_flag=args.media, max_bytes_flag=args.media_max_bytes, config_path=_config_path(args)
    )
    recheck = parse_duration(args.recheck, flag="--recheck")

    with sync_lock(directory) as acquired:
        if not acquired:
            # The contract that makes overlapping scheduled runs harmless:
            # the other sync is doing this one's job.
            print(f"another sync already holds {directory / 'archive.lock'} — nothing to do")
            return 0
        token = resolve_token(flag=args.token, config_path=_config_path(args))
        client = SlackClient(token, timeout=args.timeout)
        workspace = Workspace(client)
        archive = Archive.open_rw(directory)
        syncer = Syncer(
            workspace,
            client,
            archive,
            token=token,
            full=args.full,
            recheck_seconds=recheck,
            media_tiers=tiers,
            media_max_bytes=max_bytes,
            timeout=args.timeout,
        )
        report = syncer.run()
    return _emit(render_sync_report(report, as_json=args.json))


def parse_file_target(value: str) -> str:
    """A file ID out of whatever reference the caller holds.

    Accepts the ID itself or any Slack URL carrying one — file permalinks look
    like ``https://acme.slack.com/files/U0EXAMPLE1/F0EXAMPLE1/plan.pdf``, where
    the ID is a whole path segment and always precedes the file's own name.
    """
    candidate = value.strip()
    if _FILE_ID_RE.fullmatch(candidate):
        return candidate
    segments = [piece for piece in urlsplit(candidate).path.split("/") if piece]
    for segment in segments:
        if _FILE_ID_RE.fullmatch(segment):
            return segment
    raise UsageError(
        f"cannot find a file ID in {value!r} — pass an ID like F0EXAMPLE1, or a file permalink like "
        f"https://acme.slack.com/files/U0EXAMPLE1/F0EXAMPLE1/plan.pdf"
    )


def _external_reference(row: Any) -> str:
    """Where an external file actually lives, for the refusal message.

    For ``mode: external`` files, ``url_private`` is not a signed Slack URL —
    it is the file's real home (a docs.google.com link and the like), which is
    precisely what the caller needs to hear. The never-print rule protects
    Slack's authenticated download URLs, so anything Slack-hosted still falls
    back to the permalink.
    """
    url = str(row["url_private"] or "")
    host = urlsplit(url).hostname or ""
    if url and host and not host.endswith((".slack.com", "slack.com", "slack-files.com")):
        return url
    return str(row["permalink"] or "its Slack page (no external URL recorded)")


def cmd_file(args: argparse.Namespace) -> int:
    file_id = parse_file_target(args.target)
    archive = Archive.open_ro(_archive_dir(args))
    row = archive.file_row(file_id)
    if row is None:
        raise UsageError(
            f"file {file_id} is not in the archive — run 'slack-scrollback sync' to pick it up, then retry"
        )
    name = str(row["name"] or file_id)
    if row["mode"] == "external":
        raise UsageError(
            f"{name} lives outside Slack (mode: external), so there are no Slack-hosted bytes to fetch — "
            f"its home is {_external_reference(row)}"
        )

    notes: list[str] = []
    if row["gone_at"] is not None:
        notes.append("this file was deleted on Slack; the archive's copy is served — that is what an archive is for")

    source = "archive"
    path = None if args.live else archive.local_path_of(file_id)
    if path is None:
        url_private = row["url_private"]
        if not url_private:
            raise UsageError(
                f"Slack holds no downloadable bytes for {name} (mode: {row['mode'] or 'unknown'}) "
                f"and the archive has none either"
            )
        token = resolve_token(flag=args.token, config_path=_config_path(args))
        filename = sanitized_filename(row["name"], fallback=file_id)
        out = Path(args.out).expanduser() if args.out else Path.cwd()
        dest = out / filename if out.is_dir() else out
        if dest.exists():
            raise UsageError(f"{dest} already exists — pass --out to choose another destination, or remove it first")
        download_to(
            str(url_private),
            dest,
            token=token,
            label=f"{filename} ({file_id})",
            expected_size=int(row["size"]) if row["size"] is not None else None,
            timeout=args.timeout,
        )
        path = dest
        source = "live"

    return _emit(_render_file(row, path=path, source=source, notes=notes, as_json=args.json))


def _render_file(row: Any, *, path: Path, source: str, notes: list[str], as_json: bool) -> list[str]:
    if as_json:
        record = {
            "id": str(row["id"]),
            "name": row["name"],
            "path": str(path),
            "mimetype": row["mimetype"],
            "size": row["size"],
            "source": source,
            "permalink": row["permalink"],
        }
        lines = [json.dumps(record, ensure_ascii=False, sort_keys=True)]
        lines += [json.dumps({"type": "notice", "text": note}, ensure_ascii=False, sort_keys=True) for note in notes]
        return lines

    lines = [str(path)]
    for key in ("name", "mimetype", "size", "permalink"):
        if row[key] is not None:
            lines.append(f"{key}: {row[key]}")
    lines.append(f"source: {source}")
    lines += [f"[note: {note}]" for note in notes]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    try:
        return int(args.handler(args))
    except ScrollbackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        # Corruption can surface from any query, not just at open time; a
        # damaged archive still deserves a next step instead of a traceback.
        print(
            f"error: the local archive appears corrupt or unreadable ({exc}) — "
            f"move archive.db aside and re-run 'slack-scrollback sync', or pass --live to bypass it",
            file=sys.stderr,
        )
        return 1
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
