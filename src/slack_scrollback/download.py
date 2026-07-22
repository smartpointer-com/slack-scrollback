"""Downloading file bytes, under a three-host contract.

The API client pins everything to ``slack.com`` and refuses redirects
outright. File bytes cannot live under that rule — Slack serves them from two
other hosts — so this module implements the wider contract, measured live
rather than assumed:

* ``files.slack.com`` — where ``url_private`` points. Sent the token. Images
  come back ``200`` with the bytes; documents come back ``302`` to the
  safe-download CDN.
* ``slack-files.com`` — the CDN. The redirect target is a short-lived
  pre-signed URL under ``/files-pri-safe/``, and the signature alone
  authorises it, so the request is sent **without** the token. The token
  never travels to this host.

Everything else a redirect could name — above all the workspace login page,
which is what ``files.slack.com`` answers when access is denied — is refused
as *no access*, loudly, with no second request.

Every download is verified before it counts: final status 200, a
``Content-Type`` that is not HTML, and a body exactly as long as the size in
the file's metadata. That is the guard against archiving a login page as a
JPEG. Bytes land under a temporary name and are renamed into place, so a
half-written file never wears a real one's name.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .api import _USER_AGENT, HttpResponse, Transport, _opener_get
from .errors import DownloadError

FILES_HOST = "files.slack.com"
SAFE_HOST = "slack-files.com"
SAFE_PREFIX = f"https://{SAFE_HOST}/files-pri-safe/"


class _CaptureRedirects(urllib.request.HTTPRedirectHandler):
    """Hand 30x responses back as values instead of following them.

    urllib's default handler would follow the redirect itself, carrying the
    ``Authorization`` header to whatever host the ``Location`` names. Returning
    None makes urllib raise the original ``HTTPError``, which the transport
    below converts into an ordinary response — leaving the follow decision to
    code that can check the target first.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


_OPENER = urllib.request.build_opener(_CaptureRedirects)


def download_transport(url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
    """One GET that returns redirects and HTTP errors as values."""
    return _opener_get(_OPENER, url, headers, timeout)


def _require_files_host(url: str, label: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != FILES_HOST:
        raise DownloadError(
            f"refusing to download {label}: its URL points at "
            f"{parts.hostname or 'nothing'!r} rather than {FILES_HOST} — only Slack-hosted files are fetched"
        )


def _reject_html(response: HttpResponse, label: str) -> None:
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type == "text/html":
        raise DownloadError(
            f"download of {label} returned an HTML page instead of the file — "
            f"the token was not accepted for it, so it was not saved"
        )


def fetch_file_bytes(
    url_private: str,
    *,
    token: str,
    label: str,
    expected_size: int | None,
    timeout: float = 30.0,
    transport: Transport | None = None,
) -> bytes:
    """The verified bytes of one Slack-hosted file, or a ``DownloadError``."""
    _require_files_host(url_private, label)
    send: Transport = transport or download_transport

    authed_headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": _USER_AGENT,
    }
    response = send(url_private, authed_headers, timeout)

    if 300 <= response.status < 400:
        location = response.headers.get("location", "")
        if not location.startswith(SAFE_PREFIX):
            # files.slack.com redirects to the workspace login page when the
            # token has no access; anywhere else is not the protocol at all.
            # Either way there is nothing to fetch and nothing to retry.
            target = urllib.parse.urlsplit(location).hostname or "an empty location"
            raise DownloadError(
                f"no access to {label}: Slack redirected the download to {target} instead of its "
                f"safe-download CDN ({SAFE_PREFIX}...) — check that the app has the files:read scope"
            )
        # The signed URL authorises itself; the token must not ride along.
        response = send(location, {"User-Agent": _USER_AGENT}, timeout)
        if 300 <= response.status < 400:
            raise DownloadError(f"download of {label} redirected more than once — refusing to chase it")

    if response.status != 200:
        raise DownloadError(f"download of {label} failed with HTTP {response.status}")
    _reject_html(response, label)
    if expected_size is not None and len(response.body) != expected_size:
        raise DownloadError(
            f"download of {label} returned {len(response.body)} bytes where Slack's metadata says "
            f"{expected_size} — discarded rather than archived incomplete"
        )
    return response.body


def download_to(
    url_private: str,
    dest: Path,
    *,
    token: str,
    label: str,
    expected_size: int | None,
    timeout: float = 30.0,
    transport: Transport | None = None,
) -> int:
    """Fetch one file into ``dest``; returns the byte count written.

    The bytes are verified before anything touches ``dest``, then written to a
    temporary name in the same directory and renamed into place — a failure at
    any point leaves either the previous file or nothing, never a torso.
    """
    body = fetch_file_bytes(
        url_private, token=token, label=label, expected_size=expected_size, timeout=timeout, transport=transport
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=".download-", dir=dest.parent)
    try:
        with os.fdopen(handle, "wb") as tmp:
            tmp.write(body)
        os.replace(tmp_name, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return len(body)


def sanitized_filename(name: str | None, fallback: str) -> str:
    """A stored file's on-disk name: the basename only, never a path.

    Slack file names are workspace input; one containing separators or ``..``
    must not be allowed to steer where bytes land.
    """
    candidate = Path(str(name or "")).name.replace("\x00", "")
    if candidate in ("", ".", ".."):
        return fallback
    return candidate
