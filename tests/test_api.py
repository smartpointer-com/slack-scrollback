"""The read-only guarantee, and the transport that enforces it.

The allowlist is this toolkit's core safety property, so it is tested as one:
not merely "a write method raises" but "a write method never reaches the wire",
which is the claim the README makes to anyone handing over a token that can post.
"""

from __future__ import annotations

import json
import pathlib
import urllib.request
from typing import ClassVar

import pytest

from slack_scrollback.api import (
    _OPENER,
    READ_ONLY_METHODS,
    SLACK_HOST,
    HttpResponse,
    SlackClient,
    _RefuseRedirects,
    urllib_transport,
)
from slack_scrollback.errors import ReadOnlyViolation, ScrollbackError, SlackApiError
from tests.conftest import TOKEN, ExplodingTransport, RecordingSleep, err, make_client, ok

# Methods that change a workspace. None may ever be callable.
WRITE_METHODS = [
    "chat.postMessage",
    "chat.update",
    "chat.delete",
    "chat.postEphemeral",
    "chat.scheduleMessage",
    "chat.meMessage",
    "conversations.invite",
    "conversations.kick",
    "conversations.join",
    "conversations.leave",
    "conversations.create",
    "conversations.rename",
    "conversations.archive",
    "conversations.setPurpose",
    "conversations.setTopic",
    "reactions.add",
    "reactions.remove",
    "pins.add",
    "pins.remove",
    "files.upload",
    "files.delete",
    "files.getUploadURLExternal",
    "users.profile.set",
    "usergroups.create",
    "usergroups.update",
    "admin.users.remove",
    "admin.conversations.delete",
    "views.open",
    "dialog.open",
    "bookmarks.add",
]


def test_allowlist_is_exactly_the_documented_set() -> None:
    assert sorted(READ_ONLY_METHODS) == [
        "auth.test",
        "conversations.history",
        "conversations.info",
        "conversations.list",
        "conversations.replies",
        "users.info",
    ]


def test_allowlist_grants_nothing_the_code_does_not_use() -> None:
    """An allowlist wider than the code's actual needs is not doing its job."""
    source = "\n".join(
        (pathlib.Path(__file__).parent.parent / "src" / "slack_scrollback" / name).read_text()
        for name in ("workspace.py", "cli.py", "config.py", "format.py")
    )
    for method in READ_ONLY_METHODS:
        assert f'"{method}"' in source, f"{method} is allowlisted but never called"


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_write_methods_are_refused(method: str) -> None:
    client = SlackClient(TOKEN, transport=ExplodingTransport())
    with pytest.raises(ReadOnlyViolation) as caught:
        client.call(method)
    assert method in str(caught.value)


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_refusal_happens_before_any_request(method: str) -> None:
    """No socket, no URL, no header — the refusal precedes the request entirely."""
    client, transport = make_client({})
    with pytest.raises(ReadOnlyViolation):
        client.call(method)
    assert transport.calls == []


def test_refusal_names_what_is_allowed() -> None:
    """A refusal has to be actionable, so it lists the permitted methods."""
    client = SlackClient(TOKEN, transport=ExplodingTransport())
    with pytest.raises(ReadOnlyViolation) as caught:
        client.call("chat.postMessage")
    text = str(caught.value)
    for allowed in READ_ONLY_METHODS:
        assert allowed in text


def test_allowlist_is_immutable() -> None:
    assert isinstance(READ_ONLY_METHODS, frozenset)
    with pytest.raises(AttributeError):
        READ_ONLY_METHODS.add("chat.postMessage")  # type: ignore[attr-defined]


def test_allowed_method_reaches_the_transport() -> None:
    client, transport = make_client({"auth.test": ok(url="https://acme.slack.com/")})
    assert client.call("auth.test")["url"] == "https://acme.slack.com/"
    assert transport.methods == ["auth.test"]


def test_unknown_method_name_cannot_smuggle_a_host() -> None:
    client = SlackClient(TOKEN, transport=ExplodingTransport())
    for hostile in ("https://evil.example/api/x", "../../evil", "conversations.history/../chat.postMessage"):
        with pytest.raises(ReadOnlyViolation):
            client.call(hostile)


# -- token handling --------------------------------------------------------


def test_token_travels_in_the_header_never_the_url() -> None:
    client, transport = make_client({"auth.test": ok()})
    client.call("auth.test")
    call = transport.calls[0]
    assert call.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in call.url


def test_requests_only_ever_go_to_slack() -> None:
    client, transport = make_client({"conversations.list": ok(channels=[])})
    client.call("conversations.list")
    assert transport.calls[0].url.startswith(f"https://{SLACK_HOST}/api/")


def test_api_errors_never_leak_the_token() -> None:
    client, _ = make_client({"conversations.history": err("channel_not_found")})
    with pytest.raises(SlackApiError) as caught:
        client.call("conversations.history", channel="C0EXAMPLE1")
    assert TOKEN not in str(caught.value)


def test_none_valued_params_are_dropped() -> None:
    client, transport = make_client({"conversations.history": ok(messages=[])})
    client.call("conversations.history", channel="C0EXAMPLE1", oldest=None)
    assert "oldest" not in transport.calls[0].params
    assert transport.calls[0].params["channel"] == "C0EXAMPLE1"


class _FakeResponse:
    status = 200
    headers: ClassVar[dict[str, str]] = {}

    def read(self) -> bytes:
        return b'{"ok": true}'

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_transport_issues_a_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only is also enforced by the verb: mutating Slack methods need POST."""
    seen: dict[str, object] = {}

    class FakeOpener:
        def open(self, request: object, timeout: float | None = None) -> _FakeResponse:
            seen["method"] = request.get_method()  # type: ignore[attr-defined]
            seen["data"] = request.data  # type: ignore[attr-defined]
            return _FakeResponse()

    monkeypatch.setattr("slack_scrollback.api._OPENER", FakeOpener())
    urllib_transport("https://slack.com/api/auth.test", {"Authorization": "Bearer x"}, 5.0)
    assert seen["method"] == "GET"
    assert seen["data"] is None


def test_redirects_are_refused_so_the_token_cannot_travel_off_host() -> None:
    """urllib forwards Authorization across a redirect and permits an http downgrade.

    Host pinning is checked before the request goes out and says nothing about
    where a 30x sends it, so the handler is the only thing standing between the
    bot token and an arbitrary host.
    """
    handler = _RefuseRedirects()
    with pytest.raises(ReadOnlyViolation) as caught:
        handler.redirect_request(
            urllib.request.Request("https://slack.com/api/auth.test"),
            None,
            302,
            "Found",
            {},
            "https://evil.example/steal",
        )
    assert "evil.example" in str(caught.value)
    assert SLACK_HOST in str(caught.value)


def test_the_opener_has_redirects_disabled() -> None:
    handlers = getattr(_OPENER, "handlers", [])
    assert any(isinstance(h, _RefuseRedirects) for h in handlers)


# -- rate limiting and retries --------------------------------------------


def test_429_waits_for_retry_after_then_succeeds() -> None:
    sleeper = RecordingSleep()
    attempts = {"n": 0}

    def handler(_: dict[str, str]) -> HttpResponse | dict[str, object]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return HttpResponse(status=429, headers={"retry-after": "7"}, body=b"")
        return ok(messages=[])

    client = SlackClient(TOKEN, transport=_stub({"conversations.history": handler}), sleep=sleeper)
    assert client.call("conversations.history", channel="C0EXAMPLE1")["ok"] is True
    assert sleeper.waits == [7.0]


def test_retry_after_is_clamped_and_defaulted() -> None:
    assert SlackClient._retry_after({"retry-after": "0"}) == 1.0
    assert SlackClient._retry_after({"retry-after": "99999"}) == 300
    assert SlackClient._retry_after({"retry-after": "garbage"}) == 1.0
    assert SlackClient._retry_after({}) == 1.0


def test_ok_false_ratelimited_is_also_retried() -> None:
    sleeper = RecordingSleep()
    attempts = {"n": 0}

    def handler(_: dict[str, str]) -> dict[str, object]:
        attempts["n"] += 1
        return err("ratelimited") if attempts["n"] == 1 else ok(messages=[])

    client = SlackClient(TOKEN, transport=_stub({"conversations.history": handler}), sleep=sleeper)
    client.call("conversations.history", channel="C0EXAMPLE1")
    assert attempts["n"] == 2


def test_exhausted_retries_explain_the_wait() -> None:
    client = SlackClient(
        TOKEN,
        transport=_stub({"conversations.list": HttpResponse(429, {"retry-after": "30"}, b"")}),
        sleep=RecordingSleep(),
        max_retries=1,
    )
    with pytest.raises(ScrollbackError) as caught:
        client.call("conversations.list")
    assert "rate-limiting" in str(caught.value)


def test_server_errors_are_retried_then_reported() -> None:
    client = SlackClient(
        TOKEN,
        transport=_stub({"conversations.list": HttpResponse(503, {}, b"nope")}),
        sleep=RecordingSleep(),
        max_retries=2,
    )
    with pytest.raises(ScrollbackError) as caught:
        client.call("conversations.list")
    assert "503" in str(caught.value)


def test_non_json_response_is_reported_clearly() -> None:
    client = SlackClient(TOKEN, transport=_stub({"conversations.list": HttpResponse(200, {}, b"<html>nope")}))
    with pytest.raises(ScrollbackError) as caught:
        client.call("conversations.list")
    assert "non-JSON" in str(caught.value)


# -- error translation -----------------------------------------------------


def test_missing_scope_names_the_needed_scope() -> None:
    client, _ = make_client(
        {"conversations.history": err("missing_scope", needed="channels:history", provided="chat:write")}
    )
    with pytest.raises(SlackApiError) as caught:
        client.call("conversations.history", channel="C0EXAMPLE1")
    message = str(caught.value)
    assert "channels:history" in message
    assert "reinstall" in message


def test_not_in_channel_tells_you_to_invite_the_bot() -> None:
    client, _ = make_client({"conversations.history": err("not_in_channel")})
    with pytest.raises(SlackApiError) as caught:
        client.call("conversations.history", channel="C0EXAMPLE1")
    assert "/invite" in str(caught.value)


def test_unknown_error_codes_are_quoted_verbatim() -> None:
    client, _ = make_client({"users.info": err("some_new_slack_error")})
    with pytest.raises(SlackApiError) as caught:
        client.call("users.info", user="U0EXAMPLE1")
    assert "some_new_slack_error" in str(caught.value)
    assert caught.value.code == "some_new_slack_error"


# -- pagination ------------------------------------------------------------


def test_pagination_follows_the_cursor_and_stops_when_empty() -> None:
    pages = {
        "": ok(channels=[{"id": "C1"}], response_metadata={"next_cursor": "page2"}),
        "page2": ok(channels=[{"id": "C2"}], response_metadata={"next_cursor": ""}),
    }
    client = SlackClient(TOKEN, transport=_stub({"conversations.list": lambda p: pages[p.get("cursor", "")]}))
    assert [c["id"] for c in client.paginate("conversations.list", "channels")] == ["C1", "C2"]


def test_pagination_respects_max_items() -> None:
    body = ok(channels=[{"id": f"C{i}"} for i in range(50)], response_metadata={"next_cursor": "more"})
    client = SlackClient(TOKEN, transport=_stub({"conversations.list": body}))
    assert len(list(client.paginate("conversations.list", "channels", max_items=3))) == 3


def test_pagination_tolerates_a_missing_container() -> None:
    client = SlackClient(TOKEN, transport=_stub({"conversations.list": ok()}))
    assert list(client.paginate("conversations.list", "channels")) == []


def test_page_limit_is_capped_at_slacks_maximum() -> None:
    transport = _stub({"conversations.history": ok(messages=[])})
    client = SlackClient(TOKEN, transport=transport)
    list(client.iter_pages("conversations.history", limit=99999, channel="C0EXAMPLE1"))
    assert transport.calls[0].params["limit"] == "1000"


def _stub(handlers: dict[str, object]):  # type: ignore[no-untyped-def]
    from tests.conftest import FakeTransport

    return FakeTransport(handlers=handlers)


def test_fake_transport_reports_unstubbed_methods() -> None:
    """Guards the suite itself: an unexpected call must fail loudly."""
    client, _ = make_client({})
    with pytest.raises(AssertionError):
        client.call("auth.test")


def test_json_body_is_parsed_not_evaluated() -> None:
    client = SlackClient(
        TOKEN, transport=_stub({"auth.test": HttpResponse(200, {}, json.dumps({"ok": True}).encode())})
    )
    assert client.call("auth.test") == {"ok": True}
