"""The three-host download contract, tested as the security property it is.

The claim under test is not "downloads work" but "the token travels only to
``files.slack.com``, never to ``slack-files.com`` or anywhere a redirect might
point". Every test drives the downloader through a fake transport that records
each request, so a refusal can be asserted as *no request at all* — the same
no-trace standard the API allowlist tests hold themselves to. Nothing here
touches the network.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path

import pytest

from slack_scrollback.api import HttpResponse
from slack_scrollback.download import (
    _OPENER,
    SAFE_PREFIX,
    _CaptureRedirects,
    download_to,
    fetch_file_bytes,
    sanitized_filename,
)
from slack_scrollback.errors import DownloadError
from tests.conftest import TOKEN, FakeFileHost, file_body

URL_PRIVATE = "https://files.slack.com/files-pri/T0EXAMPLE1-F0EXAMPLE1/plan.pdf"
SAFE_URL = "https://slack-files.com/files-pri-safe/T0EXAMPLE1-F0EXAMPLE1/plan.pdf?c=abc123"
LOGIN_URL = "https://acme.slack.com/?redir=%2Ffiles-pri%2FT0EXAMPLE1-F0EXAMPLE1%2Fplan.pdf"

BODY = b"%PDF-1.7 synthetic body"
LABEL = "plan.pdf"


def redirect(location: str | None) -> HttpResponse:
    """A 302 as Slack sends it — Location only, no body worth keeping."""
    headers = {} if location is None else {"location": location}
    return HttpResponse(status=302, headers=headers, body=b"")


def fetch(host: FakeFileHost, url: str = URL_PRIVATE, expected_size: int | None = len(BODY)) -> bytes:
    return fetch_file_bytes(url, token=TOKEN, label=LABEL, expected_size=expected_size, transport=host)


def auth_header(host: FakeFileHost, index: int) -> str | None:
    """The Authorization value of the ``index``-th request, matched case-insensitively."""
    lowered = {k.lower(): v for k, v in host.calls[index].headers.items()}
    return lowered.get("authorization")


def temp_leftovers(directory: Path) -> list[Path]:
    return list(directory.glob(".download-*"))


# -- happy paths -------------------------------------------------------------


def test_direct_200_is_one_authed_request_returning_the_body() -> None:
    """Images come straight back with the bytes; the token rides only that one request."""
    host = FakeFileHost(responses={URL_PRIVATE: file_body(BODY, "image/png")})
    assert fetch(host) == BODY
    assert len(host.calls) == 1
    assert auth_header(host, 0) == f"Bearer {TOKEN}"


def test_safe_cdn_hop_is_followed_without_the_token() -> None:
    """The one redirect the protocol has: the signed URL authorises itself, so
    forwarding the Authorization header would leak the token to a second host."""
    host = FakeFileHost(responses={URL_PRIVATE: redirect(SAFE_URL), SAFE_URL: file_body(BODY)})
    assert fetch(host) == BODY
    assert len(host.calls) == 2
    assert host.calls[0].url == URL_PRIVATE
    assert auth_header(host, 0) == f"Bearer {TOKEN}"
    assert host.calls[1].url == SAFE_URL
    assert auth_header(host, 1) is None, "the token must never travel to slack-files.com"


def test_download_to_writes_exact_bytes_creating_parents(tmp_path: Path) -> None:
    """The write path is temp-name-then-rename, so success leaves exactly the
    file — right bytes, right count returned, and no temp torso beside it."""
    host = FakeFileHost(responses={URL_PRIVATE: file_body(BODY)})
    dest = tmp_path / "media" / "F0EXAMPLE1" / "plan.pdf"
    written = download_to(URL_PRIVATE, dest, token=TOKEN, label=LABEL, expected_size=len(BODY), transport=host)
    assert written == len(BODY)
    assert dest.read_bytes() == BODY
    assert temp_leftovers(dest.parent) == []


# -- refused redirects -------------------------------------------------------


def test_login_page_redirect_means_no_access_and_no_second_request() -> None:
    """files.slack.com answers a token without access by redirecting to the
    workspace login page; following it would archive HTML as a document."""
    host = FakeFileHost(responses={URL_PRIVATE: redirect(LOGIN_URL)})
    with pytest.raises(DownloadError) as caught:
        fetch(host)
    assert len(host.calls) == 1
    message = str(caught.value)
    assert "access" in message
    assert "scope" in message
    assert LABEL in message


@pytest.mark.parametrize(
    "location",
    [
        pytest.param("https://slack-files.com/not-safe/T0EXAMPLE1-F0EXAMPLE1/plan.pdf", id="wrong-path"),
        pytest.param("http://slack-files.com/files-pri-safe/T0EXAMPLE1-F0EXAMPLE1/plan.pdf", id="cleartext"),
        pytest.param(None, id="no-location-header"),
    ],
)
def test_redirects_outside_the_signed_prefix_are_refused_after_one_request(location: str | None) -> None:
    """The follow rule is the exact prefix ``https://slack-files.com/files-pri-safe/``
    — right host on the wrong path, an http downgrade of the right path, and a
    30x with no Location at all are each something other than the protocol."""
    host = FakeFileHost(responses={URL_PRIVATE: redirect(location)})
    with pytest.raises(DownloadError):
        fetch(host)
    assert len(host.calls) == 1


def test_a_second_redirect_is_never_chased() -> None:
    """One hop is the whole protocol; a chain would let the CDN steer the client anywhere."""
    onward = f"{SAFE_PREFIX}T0EXAMPLE1-F0EXAMPLE1/again.pdf?c=def456"
    host = FakeFileHost(responses={URL_PRIVATE: redirect(SAFE_URL), SAFE_URL: redirect(onward)})
    with pytest.raises(DownloadError) as caught:
        fetch(host)
    assert len(host.calls) == 2
    assert "more than once" in str(caught.value)


# -- refused responses -------------------------------------------------------


@pytest.mark.parametrize("status", [403, 500])
def test_non_200_final_status_fails_naming_the_status(status: int) -> None:
    host = FakeFileHost(responses={URL_PRIVATE: HttpResponse(status=status, headers={}, body=b"")})
    with pytest.raises(DownloadError) as caught:
        fetch(host)
    assert str(status) in str(caught.value)
    assert len(host.calls) == 1


@pytest.mark.parametrize("content_type", ["text/html", "text/html; charset=utf-8"])
def test_html_body_is_refused_and_never_written(tmp_path: Path, content_type: str) -> None:
    """A 200 that is HTML is a login or error page wearing a success code — the
    guard against archiving one as a PDF."""
    host = FakeFileHost(responses={URL_PRIVATE: file_body(b"<html>sign in</html>", content_type)})
    dest = tmp_path / "plan.pdf"
    with pytest.raises(DownloadError):
        download_to(URL_PRIVATE, dest, token=TOKEN, label=LABEL, expected_size=None, transport=host)
    assert not dest.exists()
    assert temp_leftovers(tmp_path) == []


@pytest.mark.parametrize("content_type", ["application/pdf", "image/png"])
def test_non_html_content_types_are_accepted(content_type: str) -> None:
    host = FakeFileHost(responses={URL_PRIVATE: file_body(BODY, content_type)})
    assert fetch(host) == BODY


def test_size_mismatch_is_refused_and_nothing_lands(tmp_path: Path) -> None:
    """Slack's metadata size is exact and free, so a short body is a truncated
    or substituted download — discarded, not archived incomplete."""
    host = FakeFileHost(responses={URL_PRIVATE: file_body(BODY)})
    dest = tmp_path / "plan.pdf"
    with pytest.raises(DownloadError) as caught:
        download_to(URL_PRIVATE, dest, token=TOKEN, label=LABEL, expected_size=len(BODY) + 1, transport=host)
    assert str(len(BODY)) in str(caught.value)
    assert not dest.exists()
    assert temp_leftovers(tmp_path) == []


def test_expected_size_none_skips_the_size_check() -> None:
    host = FakeFileHost(responses={URL_PRIVATE: file_body(BODY)})
    assert fetch(host, expected_size=None) == BODY


# -- refused before any request ----------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://evil.example/x", id="foreign-host"),
        pytest.param("https://slack.com/x", id="api-host-is-not-the-files-host"),
        pytest.param("http://files.slack.com/x", id="cleartext-files-host"),
    ],
)
def test_urls_off_the_files_host_are_refused_with_zero_requests(url: str) -> None:
    """The host check precedes the request, so a poisoned ``url_private`` in
    message metadata cannot make the client greet an arbitrary server — with or
    without the token, no socket ever opens."""
    host = FakeFileHost()
    with pytest.raises(DownloadError):
        fetch(host, url=url)
    assert host.calls == []


# -- sanitized_filename -------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("plan.pdf", "plan.pdf", id="plain-name"),
        pytest.param("a/b/c.pdf", "c.pdf", id="path-reduced-to-basename"),
        pytest.param("../../etc/passwd", "passwd", id="traversal-reduced-to-basename"),
        pytest.param("..", "F0EXAMPLE1", id="dot-dot-falls-back"),
        pytest.param("", "F0EXAMPLE1", id="empty-falls-back"),
        pytest.param(None, "F0EXAMPLE1", id="missing-falls-back"),
        pytest.param("pla\x00n.pdf", "plan.pdf", id="null-byte-stripped"),
    ],
)
def test_sanitized_filename_yields_a_safe_basename(name: str | None, expected: str) -> None:
    """File names are workspace input; none may steer where bytes land."""
    result = sanitized_filename(name, "F0EXAMPLE1")
    assert result == expected
    assert "/" not in result


# -- the redirect handler itself ----------------------------------------------


def test_capture_redirects_hands_30x_back_instead_of_following() -> None:
    """urllib's default handler would follow a 302 itself, Authorization header
    and all; returning None is what surfaces the redirect as a value for the
    prefix check above. No network is involved — the handler is pure."""
    handler = _CaptureRedirects()
    outcome = handler.redirect_request(
        urllib.request.Request(URL_PRIVATE),
        None,
        302,
        "Found",
        {},
        "https://evil.example/steal",
    )
    assert outcome is None


def test_the_download_opener_uses_the_capturing_handler() -> None:
    handlers = getattr(_OPENER, "handlers", [])
    assert any(isinstance(h, _CaptureRedirects) for h in handlers)
