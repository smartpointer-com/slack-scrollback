"""Test helpers.

Nothing in the suite touches the network: every test drives ``SlackClient``
through a fake transport that records what it was asked for. That also makes the
read-only allowlist testable as the security property it is — a refused method
must leave no trace of a request anywhere.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from slack_scrollback.api import HttpResponse, SlackClient
from slack_scrollback.workspace import Conversation, Workspace

TOKEN = "xoxb-0000-test-token-not-real"


@pytest.fixture(autouse=True)
def _nothing_real_is_readable(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """No test may see this machine's real config, token, or archive.

    The archive made this load-bearing: `search` silently prefers an archive
    when one exists, and `resolve_archive_dir` falls through env and config to
    a real home-directory default — so an unscrubbed test would change
    behaviour on any machine that actually uses the tool. Tests that need one
    of these set their own value on top; explicit `environ=` arguments in
    unit tests are unaffected.
    """
    private = tmp_path_factory.mktemp("isolation")
    monkeypatch.setenv("SLACK_SCROLLBACK_CONFIG", str(private / "no-such-config.cfg"))
    monkeypatch.setenv("SLACK_SCROLLBACK_ARCHIVE_DIR", str(private / "no-such-archive"))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN_JSON_PATH", raising=False)


@dataclass
class Call:
    """One request the client tried to make."""

    method: str
    params: dict[str, str]
    headers: dict[str, str]
    url: str


Handler = Any  # a dict body, an HttpResponse, or a callable taking params


@dataclass
class FakeTransport:
    """Answers Slack calls from a canned table and records every request."""

    handlers: dict[str, Handler] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        parsed = urlsplit(url)
        method = parsed.path.rsplit("/", 1)[-1]
        params = dict(parse_qsl(parsed.query))
        self.calls.append(Call(method=method, params=params, headers=dict(headers), url=url))

        if method not in self.handlers:
            raise AssertionError(f"test did not stub Slack method {method!r}")
        result = self.handlers[method]
        if callable(result):
            result = result(params)
        if isinstance(result, HttpResponse):
            return result
        return HttpResponse(status=200, headers={}, body=json.dumps(result).encode())

    @property
    def methods(self) -> list[str]:
        return [call.method for call in self.calls]


class ExplodingTransport:
    """Fails the test if it is called at all."""

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        raise AssertionError(f"a request was made when none should have been: {url}")


@dataclass
class RecordingSleep:
    """Stands in for time.sleep so retry tests stay instant."""

    waits: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def err(code: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": code, **extra}


def make_client(handlers: dict[str, Handler] | None = None, **kwargs: Any) -> tuple[SlackClient, FakeTransport]:
    transport = FakeTransport(handlers=handlers or {})
    client = SlackClient(TOKEN, transport=transport, sleep=RecordingSleep(), **kwargs)
    return client, transport


def message(
    ts: str,
    text: str = "hello",
    user: str = "U0EXAMPLE1",
    **extra: Any,
) -> dict[str, Any]:
    """A channel message shaped like Slack's."""
    return {"type": "message", "ts": ts, "user": user, "text": text, **extra}


def channel(
    channel_id: str = "C0EXAMPLE1",
    name: str = "general",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": channel_id,
        "name": name,
        "is_channel": True,
        "is_member": True,
        "is_private": False,
        "num_members": 3,
        **extra,
    }


def conversation(channel_id: str = "C0EXAMPLE1", name: str = "#general", kind: str = "public") -> Conversation:
    return Conversation(id=channel_id, kind=kind, raw=channel(channel_id, name.lstrip("#")), name=name)


@pytest.fixture
def local_zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Pin the process's local timezone for a test.

    Rendering and bare-date parsing are both local-time behaviours, so they can
    only be tested by fixing the zone — otherwise the assertions would encode
    whatever zone the machine running them happens to be in.
    """

    def use(zone: str) -> None:
        monkeypatch.setenv("TZ", zone)
        time.tzset()

    yield use
    # monkeypatch restores TZ itself, but the C library caches it until told.
    time.tzset()


@pytest.fixture
def workspace_factory() -> Callable[[dict[str, Handler]], tuple[Workspace, FakeTransport]]:
    def build(handlers: dict[str, Handler]) -> tuple[Workspace, FakeTransport]:
        client, transport = make_client(handlers)
        return Workspace(client), transport

    return build


# -- sync fixtures -----------------------------------------------------------
#
# Sync tests drive a mutable in-memory "workspace": set up channels, messages
# and threads; run a sync; mutate; run again. The clock is injected, so tests
# choose their own "now" and windows are deterministic.

#: A fixed, readable "now" for sync tests: far enough from every message that
#: tests place their own timestamps relative to it.
NOW = 1_700_000_000.0


def ts_at(offset: float) -> str:
    """A Slack ts ``offset`` seconds after the fixture epoch (NOW - 30 days)."""
    return f"{NOW - 30 * 86400 + offset:.6f}"


def thread_parent(ts: str, *, reply_count: int, latest_reply: str, **extra: Any) -> dict[str, Any]:
    return message(ts, thread_ts=ts, reply_count=reply_count, latest_reply=latest_reply, **extra)


def thread_reply(parent_ts: str, ts: str, text: str = "a reply", **extra: Any) -> dict[str, Any]:
    return message(ts, text=text, thread_ts=parent_ts, **extra)


def slack_file(
    file_id: str = "F0EXAMPLE1",
    name: str = "plan.pdf",
    mimetype: str = "application/pdf",
    size: int = 6,
    mode: str = "hosted",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": file_id,
        "name": name,
        "mimetype": mimetype,
        "filetype": name.rsplit(".", 1)[-1],
        "size": size,
        "mode": mode,
        "permalink": f"https://acme.slack.com/files/U0EXAMPLE1/{file_id}/{name}",
        "url_private": f"https://files.slack.com/files-pri/T0EXAMPLE1-{file_id}/{name}",
        **extra,
    }


@dataclass
class FakeSlack:
    """A mutable in-memory workspace answering the read-only Slack methods.

    ``messages`` holds each conversation's channel-level messages (thread
    parents included, replies not — exactly what ``conversations.history``
    serves); ``threads`` holds replies keyed by ``(channel_id, thread_ts)``.
    Deleting from either simulates a Slack-side deletion.
    """

    channels: list[dict[str, Any]] = field(default_factory=lambda: [channel()])
    messages: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    threads: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=lambda: {"U0EXAMPLE1": "alice", "U0EXAMPLE2": "bob"})
    team_url: str = "https://acme.slack.com"

    def handlers(self) -> dict[str, Handler]:
        return {
            "auth.test": lambda params: ok(url=f"{self.team_url}/", team_id="T0EXAMPLE1"),
            "conversations.list": lambda params: ok(channels=self.channels),
            "conversations.history": self._history,
            "conversations.replies": self._replies,
            "users.info": self._user_info,
            "conversations.info": lambda params: err("channel_not_found"),
        }

    def _history(self, params: dict[str, str]) -> dict[str, Any]:
        """Model the parts of conversations.history the tool depends on.

        ``latest`` is exclusive unless ``inclusive=true`` rides along — the
        repair sweep's slice tiling rests on exactly that (verified live) —
        and ``limit``/``has_more`` behave like Slack's, so a big conversation
        genuinely takes several slices to lap.
        """
        oldest = float(params.get("oldest", "0"))
        latest = float(params.get("latest", "9999999999"))
        inclusive = params.get("inclusive") == "true"

        def in_window(m: dict[str, Any]) -> bool:
            epoch = float(str(m["ts"]))
            return oldest <= epoch and (epoch <= latest if inclusive else epoch < latest)

        found = sorted(
            (m for m in self.messages.get(params.get("channel", ""), []) if in_window(m)),
            key=lambda m: float(str(m["ts"])),
            reverse=True,
        )
        limit = int(params.get("limit", "100"))
        return ok(messages=found[:limit], has_more=len(found) > limit)

    def _replies(self, params: dict[str, str]) -> dict[str, Any]:
        channel_id, thread_ts = params.get("channel", ""), params.get("ts", "")
        parent = next((m for m in self.messages.get(channel_id, []) if str(m["ts"]) == thread_ts), None)
        replies = self.threads.get((channel_id, thread_ts), [])
        if parent is None:
            return err("thread_not_found")
        ordered = sorted(replies, key=lambda m: float(str(m["ts"])))
        return ok(messages=[parent, *ordered], has_more=False)

    def _user_info(self, params: dict[str, str]) -> dict[str, Any]:
        user_id = params.get("user", "")
        if user_id not in self.users:
            return err("user_not_found")
        return ok(user={"id": user_id, "profile": {"display_name": self.users[user_id]}})


@dataclass
class FakeFileHost:
    """Answers media downloads from a canned URL table, recording each request."""

    responses: dict[str, Handler] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
        self.calls.append(Call(method="GET", params={}, headers=dict(headers), url=url))
        if url not in self.responses:
            raise AssertionError(f"test did not stub download URL {url!r}")
        result = self.responses[url]
        if callable(result):
            result = result({})
        if isinstance(result, HttpResponse):
            return result
        raise AssertionError(f"download stub for {url!r} must be an HttpResponse")


def file_body(payload: bytes, content_type: str = "application/pdf") -> HttpResponse:
    return HttpResponse(status=200, headers={"content-type": content_type}, body=payload)


def run_sync(
    fake: FakeSlack,
    archive_dir: Any,
    *,
    full: bool = False,
    now: float = NOW,
    recheck_seconds: float = 7 * 86400.0,
    media_tiers: frozenset[str] = frozenset(),
    media_max_bytes: int | None = None,
    downloads: FakeFileHost | None = None,
    sweep_pages: int = 0,
    sweep_page_size: int = 200,
) -> tuple[Any, Any, FakeTransport]:
    """One sync run; returns ``(report, archive, transport)``.

    The archive connection is left open for assertions. Call again with the
    same directory (after mutating ``fake``) for an incremental follow-up run.
    The repair sweep is OFF by default here, unlike production: most tests
    assert exact windows and call counts, and a sweep slice underneath them
    would couple every assertion to the sweep schedule. Sweep tests opt in.
    """
    from pathlib import Path

    from slack_scrollback.archive import Archive
    from slack_scrollback.syncer import Syncer

    client, transport = make_client(fake.handlers())
    workspace = Workspace(client)
    archive = Archive.open_rw(Path(archive_dir))
    syncer = Syncer(
        workspace,
        client,
        archive,
        token=TOKEN,
        full=full,
        recheck_seconds=recheck_seconds,
        media_tiers=media_tiers,
        media_max_bytes=media_max_bytes,
        sweep_pages=sweep_pages,
        sweep_page_size=sweep_page_size,
        now_fn=lambda: now,
        download_transport=downloads,
    )
    report = syncer.run()
    return report, archive, transport
