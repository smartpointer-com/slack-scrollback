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
