"""Slack Web API transport, read-only by construction.

Three independent layers keep this toolkit incapable of changing a workspace:

1. :data:`READ_ONLY_METHODS` — an explicit allowlist checked before a request is
   built. A method that is not listed never reaches the network, whatever scopes
   the token happens to carry. Bot tokens are routinely granted ``chat:write``
   and friends for unrelated reasons; the allowlist, not the token, is what makes
   this tool safe.
2. GET-only. Every allowlisted method reads, and every one of them accepts GET,
   so the verb is hard-coded. Slack's mutating methods require POST.
3. Host pinning. Requests go to ``slack.com`` and nowhere else — no telemetry,
   no update checks, no redirects off-host.

The token travels in the ``Authorization`` header, never in a URL, so it cannot
leak through a query string into a log, a proxy, or an error message.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ReadOnlyViolation, ScrollbackError, SlackApiError

SLACK_HOST = "slack.com"
SLACK_API_BASE = f"https://{SLACK_HOST}/api"

#: Every Slack method this toolkit may call. All are read-only, and this is the
#: complete set the code actually uses — an allowlist that grants more than that
#: is not doing its job.
#:
#: Three read-only methods are deliberately absent:
#:
#: * ``search.*`` rejects bot tokens outright (``not_allowed_token_type``), so
#:   the ``search`` subcommand matches freshly-fetched history locally and never
#:   needs it.
#: * ``chat.getPermalink`` would cost one request per message; permalinks are
#:   composed from ``auth.test`` instead.
#: * ``users.list`` would page through an entire workspace to name a handful of
#:   speakers; names are resolved one at a time through ``users.info``.
READ_ONLY_METHODS: frozenset[str] = frozenset(
    {
        "auth.test",
        "conversations.history",
        "conversations.info",
        "conversations.list",
        "conversations.replies",
        "users.info",
    }
)

_USER_AGENT = "slack-scrollback (+https://github.com/smartpointer/slack-scrollback)"

# Slack's documented ceiling for conversations.history/replies; conversations.list
# is happiest at 200.
MAX_PAGE_LIMIT = 1000
DEFAULT_PAGE_LIMIT = 200

# A backstop on cursor-following, far above any real workspace: at the maximum
# page size this is a million messages from one conversation.
MAX_PAGES = 1000

_MAX_RETRY_WAIT_SECONDS = 300


@dataclass(frozen=True)
class HttpResponse:
    """A minimal HTTP result. ``headers`` keys are lowercased."""

    status: int
    headers: Mapping[str, str]
    body: bytes


Transport = Callable[[str, Mapping[str, str], float], HttpResponse]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Stop urllib following a redirect anywhere.

    Host pinning is checked on the URL before the request goes out, which says
    nothing about where a 30x might send it afterwards. urllib follows redirects
    by default and its ``redirect_request`` strips only the content headers — the
    ``Authorization`` header rides along to whatever host the ``Location`` names,
    and a downgrade to cleartext ``http`` is permitted. That would hand the bot
    token to a third party and return their response as though Slack had sent it.

    Slack's API does not redirect, so refusing outright costs nothing and makes
    the pinning claim true rather than aspirational.
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
        raise ReadOnlyViolation(
            f"refusing to follow an HTTP {code} redirect away from {SLACK_HOST} to {newurl!r}: "
            f"slack-scrollback talks to {SLACK_HOST} and nowhere else"
        )


# Built once: the default opener would follow redirects and leak the token.
_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _opener_get(
    opener: urllib.request.OpenerDirector, url: str, headers: Mapping[str, str], timeout: float
) -> HttpResponse:
    """One GET through ``opener``.

    HTTP error statuses come back as values rather than exceptions so callers
    can treat 429, 5xx — and, under an opener that declines to follow them,
    redirects — uniformly. Only unreachability and timeouts raise.
    """
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    host = urllib.parse.urlsplit(url).hostname
    try:
        with opener.open(request, timeout=timeout) as response:
            return HttpResponse(
                status=response.status,
                headers={k.lower(): v for k, v in response.headers.items()},
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=exc.code,
            headers={k.lower(): v for k, v in (exc.headers or {}).items()},
            body=exc.read(),
        )
    except urllib.error.URLError as exc:
        raise ScrollbackError(
            f"cannot reach {host}: {exc.reason} — check network connectivity and any proxy settings"
        ) from exc
    except TimeoutError as exc:
        raise ScrollbackError(f"timed out talking to {host} after {timeout:g}s — retry, or raise --timeout") from exc


def urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
    """Perform one GET with the standard library, refusing redirects outright."""
    return _opener_get(_OPENER, url, headers, timeout)


# Slack error string -> the next step that resolves it. Anything absent
# falls back to quoting Slack's own string, which is still better than a
# traceback and keeps unknown errors visible rather than swallowed.
_ERROR_HELP: dict[str, str] = {
    "invalid_auth": "Slack rejected the bot token — check it is current, and that the app is still installed",
    "not_authed": "no token reached Slack — supply one with --token, $SLACK_BOT_TOKEN, or the config file",
    "account_inactive": "the token belongs to a deactivated app or workspace — reinstall the Slack app",
    "token_revoked": "the token has been revoked — reinstall the Slack app to mint a new one",
    "token_expired": "the token has expired — reinstall the Slack app to mint a new one",
    "not_in_channel": (
        "the bot is not a member of that conversation — invite it by typing '/invite @your-bot-name' "
        "in the channel, then retry"
    ),
    "channel_not_found": (
        "no such conversation, or the bot cannot see it — run 'slack-scrollback channels' "
        "to list every conversation this bot can read"
    ),
    "channel_is_limited_access": "that conversation has limited access and the bot is not permitted to read it",
    "thread_not_found": "no thread starts at that timestamp — check the permalink points at a thread's first message",
    "message_not_found": "no message exists at that timestamp in that conversation",
    "invalid_ts_oldest": "Slack rejected the --since timestamp — use a form like 7d, today, or 2026-01-31",
    "invalid_ts_latest": "Slack rejected the --until timestamp — use a form like 7d, today, or 2026-01-31",
    "invalid_cursor": "Slack rejected a pagination cursor — retry the command",
    "not_allowed_token_type": (
        "Slack refuses this method for bot tokens — this toolkit is bot-token-only by design; "
        "the affected feature is unavailable rather than approximated"
    ),
    "ratelimited": "Slack is rate-limiting this app — retry in a minute",
    "access_denied": "the workspace or its admins denied access to this data",
    "no_permission": "the token lacks permission for this data — check the app's scopes under OAuth & Permissions",
    "fatal_error": "Slack reported an internal error — retry",
    "internal_error": "Slack reported an internal error — retry",
    "service_unavailable": "Slack is temporarily unavailable — retry",
}


class SlackClient:
    """Read-only Slack Web API client.

    ``transport`` and ``sleep`` are injectable so the allowlist and retry
    behaviour can be exercised without touching the network.
    """

    def __init__(
        self,
        token: str,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
        max_retries: int = 4,
    ) -> None:
        self._token = token
        self._transport: Transport = transport or urllib_transport
        self._sleep = sleep
        self._timeout = timeout
        self._max_retries = max_retries

    # -- the safety gate ---------------------------------------------------

    @staticmethod
    def _check_allowed(method: str) -> None:
        if method not in READ_ONLY_METHODS:
            allowed = ", ".join(sorted(READ_ONLY_METHODS))
            raise ReadOnlyViolation(
                f"refusing to call Slack method {method!r}: slack-scrollback is read-only "
                f"and calls only these methods: {allowed}"
            )

    def _build_url(self, method: str, params: Mapping[str, Any]) -> str:
        query = {k: str(v) for k, v in params.items() if v is not None}
        url = f"{SLACK_API_BASE}/{method}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        host = urllib.parse.urlsplit(url).hostname
        if host != SLACK_HOST:
            raise ReadOnlyViolation(f"refusing to contact host {host!r}: slack-scrollback only talks to {SLACK_HOST}")
        return url

    # -- requests ----------------------------------------------------------

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        """Call one allowlisted read-only method and return its parsed body."""
        self._check_allowed(method)
        url = self._build_url(method, params)
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }

        for attempt in range(self._max_retries + 1):
            response = self._transport(url, headers, self._timeout)
            last = attempt >= self._max_retries

            if response.status == 429:
                wait = self._retry_after(response.headers)
                if last:
                    raise ScrollbackError(
                        f"Slack is rate-limiting {method} and retries are exhausted — "
                        f"wait {wait:g}s and retry, or narrow the request with --since / --limit"
                    )
                self._sleep(wait)
                continue

            if response.status >= 500:
                if last:
                    raise ScrollbackError(f"Slack returned HTTP {response.status} for {method} — retry shortly")
                self._sleep(self._backoff(attempt))
                continue

            body = self._decode(method, response)

            if body.get("ok"):
                return body

            code = str(body.get("error") or "unknown_error")
            if code == "ratelimited" and not last:
                self._sleep(self._retry_after(response.headers))
                continue
            raise self._api_error(method, code, body)

        # The loop always returns or raises; this keeps type checkers honest.
        raise ScrollbackError(f"exhausted retries calling {method}")

    @staticmethod
    def _decode(method: str, response: HttpResponse) -> dict[str, Any]:
        try:
            decoded = json.loads(response.body)
        except ValueError as exc:
            raise ScrollbackError(
                f"Slack returned a non-JSON response (HTTP {response.status}) for {method} — retry shortly"
            ) from exc
        if not isinstance(decoded, dict):
            raise ScrollbackError(f"Slack returned an unexpected response shape for {method}")
        return decoded

    @staticmethod
    def _api_error(method: str, code: str, body: Mapping[str, Any]) -> SlackApiError:
        if code == "missing_scope":
            needed = body.get("needed")
            if needed:
                message = (
                    f"the Slack app is missing the '{needed}' scope needed for {method} — "
                    f"add it under OAuth & Permissions, then reinstall the app"
                )
            else:
                message = f"the Slack app is missing a scope needed for {method} — check OAuth & Permissions"
        else:
            help_text = _ERROR_HELP.get(code)
            message = help_text if help_text else f"Slack rejected {method} with error '{code}'"
        return SlackApiError(f"{message} (Slack error: {code})", code=code, method=method)

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> float:
        """Seconds to wait per Slack's ``Retry-After`` header, clamped."""
        raw = headers.get("retry-after")
        try:
            wait = float(raw) if raw is not None else 1.0
        except ValueError:
            wait = 1.0
        return max(1.0, min(wait, _MAX_RETRY_WAIT_SECONDS))

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0**attempt, 30.0)

    # -- pagination --------------------------------------------------------

    def iter_pages(self, method: str, *, limit: int = DEFAULT_PAGE_LIMIT, **params: Any) -> Iterator[dict[str, Any]]:
        """Yield successive response bodies, following ``next_cursor``.

        Page bodies rather than items, because callers need ``has_more`` and the
        page size itself to notice when Slack is quietly capping them.
        """
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(MAX_PAGES):
            page_params = dict(params)
            page_params["limit"] = min(limit, MAX_PAGE_LIMIT)
            if cursor:
                page_params["cursor"] = cursor
            body = self.call(method, **page_params)
            yield body
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return
            # A cursor that repeats means Slack is pointing back at a page already
            # served; following it would loop forever on somebody's terminal.
            if cursor in seen_cursors:
                return
            seen_cursors.add(cursor)

    def paginate(
        self,
        method: str,
        container: str,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        max_items: int | None = None,
        **params: Any,
    ) -> Iterator[dict[str, Any]]:
        """Yield items from ``container`` across pages, stopping at ``max_items``."""
        seen = 0
        for body in self.iter_pages(method, limit=limit, **params):
            items = body.get(container) or []
            for item in items:
                yield item
                seen += 1
                if max_items is not None and seen >= max_items:
                    return
