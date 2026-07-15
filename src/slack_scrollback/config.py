"""Token resolution.

Precedence is ``--token``, then ``$SLACK_BOT_TOKEN``, then the config file.
Flags carry the value itself rather than the name of an environment variable to
read: one indirection is confusing, and the shell already offers ``$VAR``.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import ConfigError

ENV_TOKEN = "SLACK_BOT_TOKEN"
CONFIG_ENV = "SLACK_SCROLLBACK_CONFIG"
CONFIG_KEY = "SLACK_BOT_TOKEN"

BOT_TOKEN_PREFIX = "xoxb-"
USER_TOKEN_PREFIX = "xoxp-"


def default_config_path() -> Path:
    """Where the config file lives when ``--config`` is not given."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".config" / "slack-scrollback.cfg"


def parse_config(text: str) -> dict[str, str]:
    """Parse the ``KEY=VALUE`` config format.

    The format is deliberately its own thing rather than a shell fragment: one
    ``KEY=VALUE`` per line, ``#`` starts a comment, surrounding single or double
    quotes are stripped from the value. There is no interpolation, no ``export``,
    no line continuation and no command substitution — a config file is data, and
    sourcing it through a shell would both import shell semantics nobody asked
    for and forfeit the pure-Python, no-host-dependency guarantee.
    """
    out: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            # The offending line is deliberately not quoted back. Writing the
            # bare token and forgetting the key is the obvious way to reach here,
            # so echoing the line would print the secret to stderr — the one
            # thing this tool promises never to do. A line number locates it.
            raise ConfigError(
                f"config line {lineno} is not KEY=VALUE — write it as "
                f"'{CONFIG_KEY}=xoxb-...', or start the line with '#' to comment it out "
                f"(the line's own text is withheld in case it is a token)"
            )
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


def load_config(path: Path) -> dict[str, str]:
    """Read and parse the config file; a missing file is not an error."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror}") from exc
    return parse_config(text)


def _validate(token: str, source: str) -> str:
    """Reject anything that is not a bot token, naming where it came from."""
    token = token.strip()
    if not token:
        raise ConfigError(f"the token from {source} is empty — set it to a bot token starting with {BOT_TOKEN_PREFIX}")
    if token.startswith(USER_TOKEN_PREFIX):
        raise ConfigError(
            f"the token from {source} is a user token ({USER_TOKEN_PREFIX}...), which this tool rejects by design: "
            f"it would read everything the person can see rather than only what the bot was invited to. "
            f"Supply a bot token ({BOT_TOKEN_PREFIX}...) from your Slack app's OAuth & Permissions page"
        )
    if not token.startswith(BOT_TOKEN_PREFIX):
        raise ConfigError(
            f"the token from {source} does not look like a Slack bot token: "
            f"it must start with {BOT_TOKEN_PREFIX} (get one from your Slack app's OAuth & Permissions page)"
        )
    return token


def resolve_token(
    *,
    flag: str | None = None,
    config_path: Path | None = None,
    environ: dict[str, str] | None = None,
) -> str:
    """Find the bot token, or explain precisely how to supply one."""
    env = os.environ if environ is None else environ
    path = config_path or default_config_path()

    if flag:
        return _validate(flag, "--token")

    from_env = env.get(ENV_TOKEN)
    if from_env:
        return _validate(from_env, f"${ENV_TOKEN}")

    config = load_config(path)
    from_file = config.get(CONFIG_KEY)
    if from_file:
        return _validate(from_file, f"{CONFIG_KEY} in {path}")

    raise ConfigError(
        f"no Slack bot token found — supply one with '--token xoxb-...', "
        f"or export {ENV_TOKEN}=xoxb-..., "
        f"or write '{CONFIG_KEY}=xoxb-...' into {path}"
    )
