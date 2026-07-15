"""Command-line surface.

Four subcommands, each of which answers a whole question in one invocation:
``channels`` to discover, ``history`` to read, ``thread`` to follow a
conversation, ``search`` to find. Small models fall off a cliff when a task
requires chaining calls, so no subcommand exists only as a step towards another.

Every subcommand works with no optional flags at all.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .api import SlackClient
from .config import resolve_token
from .errors import ScrollbackError, UsageError
from .format import link_key, render_channels, render_messages
from .timeparse import parse_time, to_slack_ts
from .workspace import Conversation, Entry, Workspace

DEFAULT_LIMIT = 200
DEFAULT_SEARCH_WINDOW = "30d"

# https://acme.slack.com/archives/C0EXAMPLE1/p1700000000123456
_PERMALINK_RE = re.compile(r"/archives/(?P<channel>[A-Za-z0-9]+)/p(?P<ts>\d{10,})")
_TS_RE = re.compile(r"\d{10,}\.\d{1,6}")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--token",
        metavar="XOXB",
        help="Slack bot token (else $SLACK_BOT_TOKEN, else the config file)",
    )
    parser.add_argument("--config", metavar="PATH", help="config file (default: ~/.config/slack-scrollback.cfg)")
    parser.add_argument("--json", action="store_true", help="emit JSONL instead of text")
    parser.add_argument("--links", action="store_true", help="append each message's Slack permalink")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SEC",
        help="per-request timeout (default: 30)",
    )


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
    channels.add_argument(
        "--no-activity",
        action="store_true",
        help="skip the last-activity lookup, saving one request per conversation (useful on large workspaces)",
    )
    channels.set_defaults(handler=cmd_channels)

    history = subparsers.add_parser(
        "history",
        help="read a conversation's messages",
        description="Fetch messages from one conversation, oldest first, with thread replies inline.",
    )
    _add_common(history)
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
        description="Find messages containing text by reading history and matching locally.",
    )
    _add_common(search)
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

    return parser


# -- shared plumbing -------------------------------------------------------


def _workspace(args: argparse.Namespace) -> Workspace:
    token = resolve_token(flag=args.token, config_path=Path(args.config) if args.config else None)
    return Workspace(SlackClient(token, timeout=args.timeout))


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
    workspace = _workspace(args)
    rows: list[tuple[Conversation, str | None]] = []
    for conversation in workspace.readable_conversations():
        last = None if args.no_activity else workspace.last_activity(conversation)
        rows.append((conversation, last))
    if not args.no_activity:
        rows.sort(key=lambda row: (row[1] is None, _most_recent_first(row[1])))
    return _emit(render_channels(rows, as_json=args.json))


def _most_recent_first(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return -float(str(ts).partition(".")[0])
    except ValueError:
        return 0.0


def cmd_history(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    limit = _limit_of(args)
    conversation = workspace.resolve(args.channel)
    oldest, latest = _window(args)
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
    workspace = _workspace(args)
    limit = _limit_of(args)
    channel_spec, thread_ts = parse_thread_target(args.target)
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
        )
    )


def cmd_search(args: argparse.Namespace) -> int:
    workspace = _workspace(args)
    limit = _limit_of(args)
    oldest, latest = _window(args)
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
        )
    )


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
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
