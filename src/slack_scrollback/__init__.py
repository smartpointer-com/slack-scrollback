"""slack-scrollback: read-only Slack history for LLM agents."""

import sys

__version__ = "0.1.0"

# Checked here because this module is the first thing any entry point imports.
#
# The package happens to import cleanly on 3.9 today — nothing 3.11-only runs at
# import time, and `from __future__ import annotations` defers the rest — but
# that is an accident, not a promise, and exactly the kind that breaks silently
# later. It matters most for the single-file build, whose shebang resolves to
# whatever python3 the running user has first on PATH: on macOS that is still
# 3.9. Refusing outright beats a traceback from somewhere deep.
#
# ruff reads the project's 3.11 floor and calls this block unreachable; it is
# reachable precisely when that floor is violated, which is the case it exists
# for. Kept to syntax old interpreters can parse, or it would raise SyntaxError
# instead of explaining itself.
if sys.version_info < (3, 11):  # noqa: UP036
    _v = sys.version_info
    raise SystemExit(
        f"slack-scrollback needs Python 3.11 or newer, but is running on "
        f"{_v[0]}.{_v[1]}.{_v[2]} ({sys.executable}). Run it with a newer interpreter, e.g. "
        f"`/opt/homebrew/bin/python3 slack-scrollback ...`, or rebuild the single-file "
        f"artifact against one: `make build PYTHON_SHEBANG=/opt/homebrew/bin/python3`"
    )
