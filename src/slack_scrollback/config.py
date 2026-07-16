"""Token resolution.

Precedence is ``--token``, then ``$SLACK_BOT_TOKEN``, then ``SLACK_BOT_TOKEN``
in the config file, and finally a JSON file named by ``SLACK_BOT_TOKEN_JSON_PATH``.
Flags carry the value itself rather than the name of an environment variable to
read: one indirection is confusing, and the shell already offers ``$VAR``.

The JSON step exists so a token that already lives in some other tool's secret
store does not have to be copied here. A second copy is not merely untidy: it
goes stale the moment the token is rotated, and does so silently. Naming the file
rather than knowing about any particular one keeps that generic — a password
manager's export, an agent framework's secret store, a provisioning artefact all
work the same way, and this tool learns nothing about any of them.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from .archive import DEFAULT_MEDIA_MAX_BYTES, DEFAULT_MEDIA_TIERS, MEDIA_TIERS
from .errors import ConfigError

ENV_TOKEN = "SLACK_BOT_TOKEN"
ENV_TOKEN_JSON = "SLACK_BOT_TOKEN_JSON_PATH"
CONFIG_ENV = "SLACK_SCROLLBACK_CONFIG"
CONFIG_KEY = "SLACK_BOT_TOKEN"
CONFIG_KEY_JSON = "SLACK_BOT_TOKEN_JSON_PATH"

#: The field read from that JSON file. Fixed rather than configurable: one knob
#: is a fallback, two are a query language.
JSON_FIELD = "slack_bot_token"

BOT_TOKEN_PREFIX = "xoxb-"
USER_TOKEN_PREFIX = "xoxp-"

ENV_ARCHIVE_DIR = "SLACK_SCROLLBACK_ARCHIVE_DIR"
CONFIG_KEY_ARCHIVE_DIR = "ARCHIVE_DIR"
CONFIG_KEY_MEDIA_TIERS = "MEDIA_TIERS"
CONFIG_KEY_MEDIA_MAX_BYTES = "MEDIA_MAX_BYTES"


def config_candidates(environ: Mapping[str, str] | None = None) -> list[Path]:
    """The config files looked for, in order, when ``--config`` is not given.

    Two locations, because a token is not ordinary configuration. ``~/.config``
    is the conventional home and comes first; ``~/.secrets`` is the common habit
    of keeping credentials in one narrowly-permissioned directory, separate from
    settings that are safe to read, sync, or commit. Supporting both costs a
    stat() and saves everyone whose secrets already live apart from their config.
    """
    env = os.environ if environ is None else environ
    override = env.get(CONFIG_ENV)
    if override:
        return [Path(override)]
    home = Path(os.path.expanduser("~"))
    return [
        home / ".config" / "slack-scrollback.cfg",
        home / ".secrets" / "slack-scrollback.env",
    ]


def default_config_path(environ: Mapping[str, str] | None = None) -> Path:
    """The first config file that exists, or the conventional one to create."""
    candidates = config_candidates(environ)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


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


def token_from_json(path: Path, *, source: str) -> str:
    """Read the bot token out of a JSON file's ``slack_bot_token`` field.

    Every failure names the file and the field, because this path is reached
    precisely when someone has pointed the tool at a store it did not write and
    cannot guess the shape of.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"{source} points at {path}, which does not exist") from exc
    except PermissionError as exc:
        raise ConfigError(
            f"{source} points at {path}, which this user cannot read — check its ownership and mode"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"{source} points at {path}, which cannot be read: {exc.strerror}") from exc

    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"{source} points at {path}, which is not valid JSON") from exc

    if not isinstance(document, dict):
        raise ConfigError(f"{path} does not hold a JSON object, so it has no '{JSON_FIELD}' field")

    value = document.get(JSON_FIELD)
    if value is None:
        raise ConfigError(
            f"{path} has no '{JSON_FIELD}' field — add one, or point {ENV_TOKEN_JSON} at a file that has it"
        )
    if not isinstance(value, str):
        raise ConfigError(f"the '{JSON_FIELD}' field in {path} is a {type(value).__name__}, not a string")
    return _validate(value, f"the '{JSON_FIELD}' field in {path}")


def resolve_archive_dir(
    *,
    flag: str | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Where the archive lives, with the same precedence shape as the token.

    ``--archive-dir``, then ``$SLACK_SCROLLBACK_ARCHIVE_DIR``, then
    ``ARCHIVE_DIR`` in the config file, then the XDG-conventional default.
    Note the flag is a *path*; ``--archive`` (no ``-dir``) is the backend
    selector and a different thing entirely.
    """
    env = os.environ if environ is None else environ
    if flag:
        return Path(flag).expanduser()
    from_env = env.get(ENV_ARCHIVE_DIR)
    if from_env:
        return Path(from_env).expanduser()
    config = load_config(config_path or default_config_path(env))
    from_file = config.get(CONFIG_KEY_ARCHIVE_DIR)
    if from_file:
        return Path(from_file).expanduser()
    return Path(os.path.expanduser("~")) / ".local" / "share" / "slack-scrollback"


def resolve_media_settings(
    *,
    tiers_flag: str | None = None,
    max_bytes_flag: int | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[frozenset[str], int | None]:
    """The media tiers ``sync`` downloads, and the per-file size cap.

    Flags beat the config file's ``MEDIA_TIERS``/``MEDIA_MAX_BYTES``; the
    default is documents and images with no size cap — the tier list, not a
    byte count, is what bounds an archive. ``0`` also means uncapped, so a
    config file's cap can be lifted from the command line. ``none`` turns
    downloads off entirely — metadata is still recorded, because it costs
    nothing and makes a later change of mind a re-download instead of a
    blind spot.
    """
    env = os.environ if environ is None else environ
    config = load_config(config_path or default_config_path(env))

    raw_tiers = tiers_flag if tiers_flag is not None else config.get(CONFIG_KEY_MEDIA_TIERS)
    if raw_tiers is None:
        tiers = DEFAULT_MEDIA_TIERS
    else:
        wanted = {piece.strip().lower() for piece in raw_tiers.split(",") if piece.strip()}
        if wanted == {"none"}:
            tiers = frozenset()
        else:
            unknown = wanted - set(MEDIA_TIERS)
            if unknown or not wanted:
                raise ConfigError(
                    f"--media/{CONFIG_KEY_MEDIA_TIERS} must be 'none' or a comma-separated subset of "
                    f"{', '.join(MEDIA_TIERS)} — not {raw_tiers!r}"
                )
            tiers = frozenset(wanted)

    max_bytes: int | None
    if max_bytes_flag is not None:
        max_bytes = max_bytes_flag
    else:
        raw_max = config.get(CONFIG_KEY_MEDIA_MAX_BYTES)
        if raw_max is None:
            max_bytes = DEFAULT_MEDIA_MAX_BYTES
        else:
            try:
                max_bytes = int(raw_max)
            except ValueError:
                raise ConfigError(
                    f"{CONFIG_KEY_MEDIA_MAX_BYTES} must be a byte count like 52428800, not {raw_max!r}"
                ) from None
    if max_bytes is not None and max_bytes < 0:
        raise ConfigError(f"--media-max-bytes must not be negative (got {max_bytes}) — use 0 or omit it for no limit")
    if max_bytes == 0:
        max_bytes = None
    return tiers, max_bytes


def resolve_token(
    *,
    flag: str | None = None,
    config_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Find the bot token, or explain precisely how to supply one."""
    env = os.environ if environ is None else environ
    path = config_path or default_config_path(env)

    if flag:
        return _validate(flag, "--token")

    from_env = env.get(ENV_TOKEN)
    if from_env:
        return _validate(from_env, f"${ENV_TOKEN}")

    config = load_config(path)
    from_file = config.get(CONFIG_KEY)
    if from_file:
        return _validate(from_file, f"{CONFIG_KEY} in {path}")

    # Last: a token held in someone else's store, named rather than copied.
    json_env = env.get(ENV_TOKEN_JSON)
    if json_env:
        return token_from_json(Path(json_env), source=f"${ENV_TOKEN_JSON}")
    json_cfg = config.get(CONFIG_KEY_JSON)
    if json_cfg:
        return token_from_json(Path(json_cfg), source=f"{CONFIG_KEY_JSON} in {path}")

    raise ConfigError(
        f"no Slack bot token found — supply one with '--token xoxb-...', "
        f"or export {ENV_TOKEN}=xoxb-..., "
        f"or write '{CONFIG_KEY}=xoxb-...' into {path}, "
        f"or point it at a JSON file holding a '{JSON_FIELD}' field with "
        f"'{CONFIG_KEY_JSON}=/path/to/secrets.json'"
    )
