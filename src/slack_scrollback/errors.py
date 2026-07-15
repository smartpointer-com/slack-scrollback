"""Errors carrying a next step.

Every message is a single line and, wherever the fix is knowable, names the
exact command or value that resolves it. The consumer is a small language model
that will paste the message straight into its next attempt, so an error that
merely describes the failure costs a whole retry cycle; one that prescribes the
remedy converges immediately.
"""

from __future__ import annotations


class ScrollbackError(Exception):
    """A fatal condition explained in terms of what to do about it."""


class ReadOnlyViolation(ScrollbackError):
    """A Slack method outside the read-only allowlist was attempted.

    Raised before any request leaves the process. This is the toolkit's core
    safety property: reaching this exception means a bug tried to call a
    mutating method, not that a user typed something wrong.
    """


class ConfigError(ScrollbackError):
    """The token or config file is missing, malformed, or the wrong kind."""


class UsageError(ScrollbackError):
    """Arguments were valid syntax but cannot be acted on (e.g. unknown channel)."""


class SlackApiError(ScrollbackError):
    """Slack returned ``ok: false``.

    ``code`` keeps the raw Slack error string so callers can branch on it and
    so the human-facing text can always quote it verbatim.
    """

    def __init__(self, message: str, *, code: str, method: str) -> None:
        super().__init__(message)
        self.code = code
        self.method = method
