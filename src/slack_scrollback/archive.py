"""The local archive: one SQLite file plus a media directory, written only by ``sync``.

The archive exists to outlive Slack. Retention windows, edits, deletions, and
message volume all stop mattering once history is on local disk — so nothing
here is ever hard-deleted. When Slack stops returning something the archive
holds, the row is marked ``gone_at`` and filtered from reads; the data, and any
downloaded bytes, stay.

Two properties make it trustworthy:

* ``sync`` is the only writer, and a whole run commits as one transaction
  (:meth:`Archive.begin` / :meth:`Archive.commit`). A crashed run changes
  nothing; a re-run is idempotent, because every write is a keyed upsert.
* The journal mode is DELETE, deliberately not WAL. WAL requires every reader
  to create ``-shm``/``-wal`` files beside the database, but the archive is
  meant to be shared with other local users through read-only permissions and
  opened by external indexers with ``immutable=1``. The single end-of-run
  commit keeps the torn-read window to the milliseconds of that commit.

``messages_flat`` is a public read contract for external indexers, not an
internal convenience — its columns, semantics and soft-delete behaviour are
documented in the README and must stay stable.
"""

from __future__ import annotations

import contextlib
import functools
import json
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .errors import ScrollbackError, UsageError

SCHEMA_VERSION = 2

DB_NAME = "archive.db"
MEDIA_DIR_NAME = "media"
LOCK_NAME = "archive.lock"

#: Subtypes that are room housekeeping rather than speech. They stay in the
#: ``messages`` table — the archive keeps everything — but are filtered from
#: ``messages_flat`` so joins and renames do not pollute an external index.
HOUSEKEEPING_SUBTYPES: tuple[str, ...] = (
    "channel_join",
    "channel_leave",
    "channel_name",
    "channel_topic",
    "channel_purpose",
    "channel_archive",
    "channel_unarchive",
    "bot_add",
    "bot_remove",
)

#: Media tiers, keyed by mimetype prefix. Everything that is not an image,
#: audio or video — PDFs, office documents, text, archives, unlabelled bytes —
#: counts as a document: those are the files whose bytes carry indexable
#: content, so "document" is the safe default for the unknown.
MEDIA_TIERS: tuple[str, ...] = ("documents", "images", "audio", "video")
DEFAULT_MEDIA_TIERS = frozenset({"documents", "images"})

#: Per-file size cap for downloads; None caps nothing. The tier list is the
#: real safety valve, so an unset cap defaults to "archive what was shared".
DEFAULT_MEDIA_MAX_BYTES: int | None = None

_SCHEMA_V1 = f"""
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE conversations (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  kind          TEXT NOT NULL,
  is_member     INTEGER NOT NULL,
  first_seen_at REAL NOT NULL,
  gone_at       REAL
);

CREATE TABLE users (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  refreshed_at REAL NOT NULL
);

CREATE TABLE messages (
  channel_id    TEXT NOT NULL,
  ts            TEXT NOT NULL,
  ts_epoch      REAL NOT NULL,
  thread_ts     TEXT,
  subtype       TEXT,
  user_id       TEXT,
  sender_name   TEXT NOT NULL,
  text          TEXT NOT NULL,
  raw           TEXT NOT NULL,
  edited_ts     TEXT,
  first_seen_at REAL NOT NULL,
  gone_at       REAL,
  PRIMARY KEY (channel_id, ts)
);
CREATE INDEX idx_messages_chan_epoch ON messages (channel_id, ts_epoch);
CREATE INDEX idx_messages_thread ON messages (channel_id, thread_ts);

CREATE TABLE files (
  id            TEXT PRIMARY KEY,
  name          TEXT,
  mimetype      TEXT,
  filetype      TEXT,
  size          INTEGER,
  mode          TEXT,
  permalink     TEXT,
  url_private   TEXT,
  local_path    TEXT,
  downloaded_at REAL,
  gone_at       REAL
);

CREATE TABLE message_files (
  channel_id TEXT NOT NULL,
  ts         TEXT NOT NULL,
  file_id    TEXT NOT NULL,
  PRIMARY KEY (channel_id, ts, file_id)
);

CREATE TABLE sync_state (
  channel_id   TEXT PRIMARY KEY,
  last_ts      TEXT NOT NULL DEFAULT '0',
  last_run_at  REAL,
  last_full_at REAL
);

CREATE VIEW messages_flat AS
  SELECT m.channel_id || ':' || m.ts                    AS msg_id,
         m.channel_id                                   AS chat_jid,
         c.name                                         AS chat_name,
         m.ts_epoch                                     AS ts,
         m.sender_name                                  AS sender_name,
         m.text                                         AS text,
         NULL AS media_type, NULL AS filename,
         NULL AS mime_type,  NULL AS local_path
    FROM messages m JOIN conversations c ON c.id = m.channel_id
   WHERE m.gone_at IS NULL
     AND (m.subtype IS NULL OR m.subtype NOT IN ({",".join(f"'{s}'" for s in HOUSEKEEPING_SUBTYPES)}))
  UNION ALL
  SELECT m.channel_id || ':' || m.ts || ':' || f.id,
         m.channel_id, c.name, m.ts_epoch, m.sender_name,
         ''                                             AS text,
         CASE WHEN f.mimetype LIKE 'image/%' THEN 'image'
              WHEN f.mimetype LIKE 'audio/%'
                OR f.mimetype LIKE 'video/%' THEN 'other'
              ELSE 'document' END                       AS media_type,
         f.name, f.mimetype, f.local_path
    FROM message_files mf
    JOIN messages m       ON m.channel_id = mf.channel_id AND m.ts = mf.ts
    JOIN files f          ON f.id = mf.file_id
    JOIN conversations c  ON c.id = m.channel_id
   WHERE m.gone_at IS NULL AND f.gone_at IS NULL
     AND f.local_path IS NOT NULL;
"""

# External-content FTS over the rendered text, with the standard trigger
# triple so edits flow through. Trigram rather than the default tokenizer:
# the documented search semantics are *substring*, and a token-based index
# would silently change what "matches" means. A quoted trigram phrase is a
# case-folded substring test over the index.
#
# Table and triggers are separate scripts because their lifecycles differ: the
# virtual table needs the fts5 module even to be DROPped, so on an FTS5-less
# host it can only be left in place — but the triggers are plain schema
# objects that would make every message write compile (and crash on) the
# missing module, so *they* are what gets dropped and later recreated.
_FTS_TABLE = """
CREATE VIRTUAL TABLE messages_fts USING fts5(
  text,
  content='messages', content_rowid='rowid',
  tokenize='trigram'
);
"""

_FTS_TRIGGER_NAMES = ("messages_fts_ai", "messages_fts_au", "messages_fts_ad")

_FTS_TRIGGERS = """
CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER messages_fts_au AFTER UPDATE OF text ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
  INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
"""

_FTS_DROP_TRIGGERS = "".join(f"DROP TRIGGER IF EXISTS {name};\n" for name in _FTS_TRIGGER_NAMES)

#: Forward-only migration scripts; ``MIGRATIONS[n]`` upgrades a version-n
#: database to version n+1 and stamps both version markers itself.
MIGRATIONS: tuple[str, ...] = (
    # v0 -> v1: the original schema.
    _SCHEMA_V1
    + "\nINSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '1');\n"
    + "PRAGMA user_version = 1;",
    # v1 -> v2: cursor state for the continuous-repair sweep. sweep_before is
    # the ts the next slice reads strictly below (NULL = start a new lap);
    # sweep_lap_at records when the last completed lap finished.
    "ALTER TABLE sync_state ADD COLUMN sweep_before TEXT;\n"
    "ALTER TABLE sync_state ADD COLUMN sweep_lap_at REAL;\n"
    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', '2');\n"
    "PRAGMA user_version = 2;",
)


@functools.cache
def fts5_trigram_available() -> bool:
    """Whether this Python's SQLite can build the trigram full-text index.

    Probed with a throwaway in-memory table because both pieces are optional:
    FTS5 is a compile-time module, and the trigram tokenizer needs SQLite 3.34.
    The archive is complete without either — search just falls back to a scan.
    """
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("CREATE VIRTUAL TABLE probe USING fts5(t, tokenize='trigram')")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


def media_tier(mimetype: str | None) -> str:
    """Which ``--media`` tier a file belongs to, from its mimetype."""
    lowered = (mimetype or "").lower()
    if lowered.startswith("image/"):
        return "images"
    if lowered.startswith("audio/"):
        return "audio"
    if lowered.startswith("video/"):
        return "video"
    return "documents"


def resolve_media_path(stored: str, media_dir: Path) -> Path:
    """Rejoin a stored media path to this reader's media directory.

    Stored paths are absolute as written at download time, but the archive
    directory may since have moved — so only the part after the last
    ``/media/`` is trusted, and it is rejoined to the configured directory.
    Every reader of the archive resolves paths this same way, which is what
    makes the whole directory relocatable without rewriting rows.
    """
    _, sep, tail = stored.rpartition(f"/{MEDIA_DIR_NAME}/")
    return media_dir / tail if sep else media_dir / Path(stored).name


def archive_exists(directory: Path) -> bool:
    return (directory / DB_NAME).is_file()


@contextlib.contextmanager
def sync_lock(directory: Path) -> Iterator[bool]:
    """Hold the archive's one-writer lock; yields False if another sync has it.

    flock rather than a pid file: the kernel releases it however the process
    dies, so there is no stale-lock state to clean up. On platforms without
    ``fcntl`` the lock degrades to a no-op — single-writer discipline is then
    the operator's job, as the README says.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX platforms only
        yield True
        return
    # The lock file is the first write of a sync run, so it is also where a
    # read-only consumer of a shared archive finds out sync is not their
    # command — that deserves the tool's own words, not a traceback.
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Opened outside its `with` so this except guards exactly the open:
        # wrapped around the whole block it would also swallow OSErrors from
        # the caller's body re-raised through the yield, and mislabel them.
        handle = open(directory / LOCK_NAME, "w")  # noqa: SIM115
    except OSError as exc:
        raise ScrollbackError(_unwritable(directory, exc)) from exc
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _unopenable(directory: Path, exc: Exception) -> str:
    return (
        f"the archive at {directory} cannot be opened ({exc}) — if the file is corrupt or half-copied, "
        f"move {DB_NAME} aside and re-run 'slack-scrollback sync'; pass --live to read Slack without it"
    )


def _unwritable(directory: Path, exc: OSError) -> str:
    return (
        f"cannot write to the archive directory {directory} ({exc.strerror}) — sync is the archive's writer "
        f"and needs write access there. A shared archive is usually shared read-only: its owner runs sync, "
        f"and everyone else reads it through search, 'history --archive', and 'file'"
    )


def _ts_epoch(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


def _canonical(message: dict[str, Any]) -> str:
    """The stored form of a raw message: stable across dict ordering."""
    return json.dumps(message, ensure_ascii=False, sort_keys=True)


class Archive:
    """One open archive database.

    ``open_rw`` (sync only) creates or migrates the schema; ``open_ro`` (every
    reader) refuses to touch a database that does not exist rather than
    conjuring an empty one — an empty answer from an archive nobody built
    would read as "nothing was said".
    """

    def __init__(self, connection: sqlite3.Connection, directory: Path) -> None:
        self._con = connection
        self._con.row_factory = sqlite3.Row
        self.directory = directory
        #: Why search cannot use the full-text index, or None when it can.
        self.fts_unavailable_reason: str | None = None

    # -- lifecycle -----------------------------------------------------------

    @classmethod
    def open_rw(cls, directory: Path) -> Archive:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / MEDIA_DIR_NAME).mkdir(exist_ok=True)
        except OSError as exc:
            raise ScrollbackError(_unwritable(directory, exc)) from exc
        # isolation_level=None puts sqlite3 in autocommit so that BEGIN/COMMIT
        # are explicit — the one-transaction-per-run contract is then visible
        # in the code that honours it rather than implied by driver defaults.
        connection = sqlite3.connect(directory / DB_NAME, isolation_level=None)
        archive = cls(connection, directory)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            archive._migrate()
            archive._ensure_fts()
        except sqlite3.Error as exc:
            raise ScrollbackError(_unopenable(directory, exc)) from exc
        return archive

    @classmethod
    def open_ro(cls, directory: Path) -> Archive:
        db = directory / DB_NAME
        if not db.is_file():
            raise UsageError(
                f"no archive found at {directory} — run 'slack-scrollback sync' to build one, "
                f"or point --archive-dir at an existing archive"
            )
        try:
            connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
            archive = cls(connection, directory)
            version = archive._user_version()
        except sqlite3.Error as exc:
            raise ScrollbackError(_unopenable(directory, exc)) from exc
        if version > SCHEMA_VERSION:
            raise ScrollbackError(
                f"the archive at {directory} has schema version {version}, newer than this tool understands "
                f"({SCHEMA_VERSION}) — upgrade slack-scrollback"
            )
        if not fts5_trigram_available():
            archive.fts_unavailable_reason = "this Python's SQLite lacks FTS5"
        elif not archive._fts_exists():
            archive.fts_unavailable_reason = "the archive has no full-text index yet (the next sync will build one)"
        elif not archive._fts_triggers_present():
            # A sync on an FTS5-less host drops the triggers and writes past
            # the index; trusting it now would silently miss those rows.
            archive.fts_unavailable_reason = (
                "the archive's full-text index is stale after a sync without FTS5 (the next sync will rebuild it)"
            )
        return archive

    def close(self) -> None:
        self._con.close()

    def begin(self) -> None:
        self._con.execute("BEGIN")

    def commit(self) -> None:
        self._con.execute("COMMIT")

    def rollback(self) -> None:
        # Tolerant of there being no transaction: rollback runs on the error
        # path, and raising here would bury the exception that mattered.
        with contextlib.suppress(sqlite3.OperationalError):
            self._con.execute("ROLLBACK")

    @property
    def media_dir(self) -> Path:
        return self.directory / MEDIA_DIR_NAME

    def _user_version(self) -> int:
        return int(self._con.execute("PRAGMA user_version").fetchone()[0])

    def _migrate(self) -> None:
        """Create or upgrade the schema, forward-only, one version at a time.

        A fresh database walks the same chain as an old one — v0 to v1 to v2 —
        so every upgrade step runs on every developer machine every day, not
        only on the one archive in the field old enough to need it.
        """
        version = self._user_version()
        if version > SCHEMA_VERSION:
            raise ScrollbackError(
                f"the archive at {self.directory} has schema version {version}, newer than this tool understands "
                f"({SCHEMA_VERSION}) — upgrade slack-scrollback"
            )
        while version < SCHEMA_VERSION:
            # The transaction lives inside the script: executescript() commits
            # any transaction already open before it runs, so wrapping it in
            # begin()/commit() from out here would silently fall apart.
            self._con.executescript("BEGIN;\n" + MIGRATIONS[version] + "\nCOMMIT;")
            advanced = self._user_version()
            if advanced <= version:  # pragma: no cover - a migration authoring bug
                raise ScrollbackError(f"schema migration from version {version} did not advance — this is a bug")
            version = advanced

    def _fts_exists(self) -> bool:
        row = self._con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'").fetchone()
        return row is not None

    def _fts_triggers_present(self) -> bool:
        placeholders = ",".join("?" for _ in _FTS_TRIGGER_NAMES)
        row = self._con.execute(
            f"SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name IN ({placeholders})",
            _FTS_TRIGGER_NAMES,
        ).fetchone()
        return int(row[0]) == len(_FTS_TRIGGER_NAMES)

    def _ensure_fts(self) -> None:
        """Bring the full-text index to whatever state this SQLite can support.

        Capable host, no index (or a stale one left by a lesser host): create
        the table and triggers and rebuild from the content table, so every
        row — including any written while the index was dark — appears.

        FTS5-less host: record why search will scan, and drop the triggers if
        an earlier capable host installed them — otherwise the very first
        message write would compile them and die on the missing module, and
        the design's promise is that such a host *archives anyway*. The
        virtual table itself stays: dropping it needs the module too, and
        without triggers it is inert.
        """
        if not fts5_trigram_available():
            self.fts_unavailable_reason = "this SQLite lacks FTS5"
            self._con.executescript("BEGIN;\n" + _FTS_DROP_TRIGGERS + "COMMIT;")
            return
        if self._fts_exists() and self._fts_triggers_present():
            return
        table = "" if self._fts_exists() else _FTS_TABLE
        self._con.executescript(
            "BEGIN;\n"
            + table
            + _FTS_DROP_TRIGGERS
            + _FTS_TRIGGERS
            + "\nINSERT INTO messages_fts(messages_fts) VALUES ('rebuild');\nCOMMIT;"
        )

    @property
    def fts_usable(self) -> bool:
        return self.fts_unavailable_reason is None and self._fts_exists()

    # -- meta ----------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self._con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        row = self._con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    # -- conversations -------------------------------------------------------

    def upsert_conversation(self, *, conversation_id: str, name: str, kind: str, is_member: bool, now: float) -> None:
        self._con.execute(
            """
            INSERT INTO conversations (id, name, kind, is_member, first_seen_at, gone_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT (id) DO UPDATE SET name = excluded.name, kind = excluded.kind,
                                           is_member = excluded.is_member, gone_at = NULL
            """,
            (conversation_id, name, kind, int(is_member), now),
        )

    def mark_conversations_gone(self, listed_ids: Iterable[str], now: float) -> int:
        """Mark conversations Slack no longer lists.

        Every run calls this — the roster is fetched every run anyway — behind
        the caller's listing-completeness guard: a truncated listing cannot
        distinguish "gone" from "not listed this time", so it must not judge.
        """
        listed = set(listed_ids)
        stored = [str(row["id"]) for row in self._con.execute("SELECT id FROM conversations WHERE gone_at IS NULL")]
        vanished = [cid for cid in stored if cid not in listed]
        self._con.executemany("UPDATE conversations SET gone_at = ? WHERE id = ?", [(now, cid) for cid in vanished])
        return len(vanished)

    def conversation_rows(self) -> list[sqlite3.Row]:
        return list(self._con.execute("SELECT * FROM conversations WHERE gone_at IS NULL ORDER BY id"))

    # -- users ----------------------------------------------------------------

    def user_names(self) -> dict[str, str]:
        return {str(row["id"]): str(row["name"]) for row in self._con.execute("SELECT id, name FROM users")}

    def fresh_user_names(self, *, now: float, max_age_seconds: float) -> dict[str, str]:
        rows = self._con.execute("SELECT id, name FROM users WHERE refreshed_at > ?", (now - max_age_seconds,))
        return {str(row["id"]): str(row["name"]) for row in rows}

    def stalest_user(self, *, now: float, min_age_seconds: float) -> str | None:
        """The user whose name has gone longest unchecked, if old enough to matter."""
        row = self._con.execute(
            "SELECT id FROM users WHERE refreshed_at < ? ORDER BY refreshed_at ASC LIMIT 1",
            (now - min_age_seconds,),
        ).fetchone()
        return str(row["id"]) if row else None

    def upsert_user(self, user_id: str, name: str, now: float) -> None:
        self._con.execute(
            """
            INSERT INTO users (id, name, refreshed_at) VALUES (?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET name = excluded.name, refreshed_at = excluded.refreshed_at
            """,
            (user_id, name, now),
        )

    # -- sync state ------------------------------------------------------------

    def last_ts(self, channel_id: str) -> str:
        row = self._con.execute("SELECT last_ts FROM sync_state WHERE channel_id = ?", (channel_id,)).fetchone()
        return str(row[0]) if row else "0"

    def sweep_state(self, channel_id: str) -> tuple[str | None, float | None]:
        """(sweep_before, sweep_lap_at) for one conversation; (None, None) starts a lap."""
        row = self._con.execute(
            "SELECT sweep_before, sweep_lap_at FROM sync_state WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            return None, None
        before = str(row["sweep_before"]) if row["sweep_before"] is not None else None
        lap_at = float(row["sweep_lap_at"]) if row["sweep_lap_at"] is not None else None
        return before, lap_at

    def set_sweep_state(self, channel_id: str, sweep_before: str | None, now: float, *, lap_completed: bool) -> None:
        self._con.execute(
            """
            INSERT INTO sync_state (channel_id, sweep_before, sweep_lap_at)
            VALUES (?, ?, ?)
            ON CONFLICT (channel_id) DO UPDATE SET
              sweep_before = excluded.sweep_before,
              sweep_lap_at = COALESCE(excluded.sweep_lap_at, sync_state.sweep_lap_at)
            """,
            (channel_id, sweep_before, now if lap_completed else None),
        )

    def set_sync_state(self, channel_id: str, last_ts: str, now: float, *, full: bool) -> None:
        self._con.execute(
            """
            INSERT INTO sync_state (channel_id, last_ts, last_run_at, last_full_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (channel_id) DO UPDATE SET
              last_ts = excluded.last_ts,
              last_run_at = excluded.last_run_at,
              last_full_at = COALESCE(excluded.last_full_at, sync_state.last_full_at)
            """,
            (channel_id, last_ts, now, now if full else None),
        )

    # -- messages ---------------------------------------------------------------

    def upsert_message(
        self,
        *,
        channel_id: str,
        ts: str,
        thread_ts: str | None,
        subtype: str | None,
        user_id: str | None,
        sender_name: str,
        text: str,
        raw: dict[str, Any],
        edited_ts: str | None,
        now: float,
    ) -> str:
        """Write one message; returns ``new``, ``edited`` or ``unchanged``.

        "Edited" means the reader-visible content moved — the rendered text or
        the edit timestamp — not merely that Slack's raw object drifted (reply
        counts and reactions churn constantly). A row previously marked gone
        that Slack serves again is un-marked: it was never deleted.
        """
        canonical = _canonical(raw)
        row = self._con.execute(
            "SELECT text, raw, edited_ts, gone_at, sender_name FROM messages WHERE channel_id = ? AND ts = ?",
            (channel_id, ts),
        ).fetchone()
        if row is None:
            self._con.execute(
                """
                INSERT INTO messages (channel_id, ts, ts_epoch, thread_ts, subtype, user_id, sender_name,
                                      text, raw, edited_ts, first_seen_at, gone_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    channel_id,
                    ts,
                    _ts_epoch(ts),
                    thread_ts,
                    subtype,
                    user_id,
                    sender_name,
                    text,
                    canonical,
                    edited_ts,
                    now,
                ),
            )
            return "new"
        # Identical raw is not enough to skip the write: the *rendering* may
        # have moved under it — a --full run resolves speaker names afresh
        # precisely so a rename reaches old rows. Only a row whose stored
        # rendering also matches is truly unchanged.
        if (
            str(row["raw"]) == canonical
            and row["gone_at"] is None
            and str(row["text"]) == text
            and str(row["sender_name"]) == sender_name
        ):
            return "unchanged"
        self._con.execute(
            """
            UPDATE messages SET thread_ts = ?, subtype = ?, user_id = ?, sender_name = ?,
                                text = ?, raw = ?, edited_ts = ?, gone_at = NULL
            WHERE channel_id = ? AND ts = ?
            """,
            (thread_ts, subtype, user_id, sender_name, text, canonical, edited_ts, channel_id, ts),
        )
        content_moved = text != str(row["text"]) or (edited_ts or None) != (
            str(row["edited_ts"]) if row["edited_ts"] is not None else None
        )
        return "edited" if content_moved else "unchanged"

    def channel_level_ts_between(
        self, channel_id: str, oldest: float, latest: float, *, latest_inclusive: bool = True
    ) -> set[str]:
        """Timestamps the archive expects ``conversations.history`` to return.

        Thread replies are excluded — their absence from a history response is
        normal, not deletion — except broadcast replies, which do appear there.
        The sweep passes ``latest_inclusive=False``: its slices are bounded by
        an *exclusive* Slack ``latest``, and an inclusive comparison here would
        expect the previous slice's oldest message in a response that, by
        protocol, cannot contain it — falsely marking it gone.
        """
        comparator = "<=" if latest_inclusive else "<"
        rows = self._con.execute(
            f"""
            SELECT ts FROM messages
            WHERE channel_id = ? AND gone_at IS NULL AND ts_epoch >= ? AND ts_epoch {comparator} ?
              AND (thread_ts IS NULL OR thread_ts = ts OR subtype = 'thread_broadcast')
            """,
            (channel_id, oldest, latest),
        )
        return {str(row["ts"]) for row in rows}

    def mark_messages_gone(self, channel_id: str, ts_values: Iterable[str], now: float) -> int:
        """Soft-delete rows; returns how many actually transitioned.

        Rows already gone, or ts values the archive never held, are not news
        and must not inflate the report's "gone" count — a thread teardown
        routinely re-names a parent the window diff already marked.
        """
        values = list(ts_values)
        marked = 0
        for start in range(0, len(values), 400):
            chunk = values[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            cursor = self._con.execute(
                f"UPDATE messages SET gone_at = ? WHERE channel_id = ? AND gone_at IS NULL AND ts IN ({placeholders})",
                [now, channel_id, *chunk],
            )
            marked += cursor.rowcount
        return marked

    def reply_ts(self, channel_id: str, thread_ts: str) -> set[str]:
        rows = self._con.execute(
            "SELECT ts FROM messages WHERE channel_id = ? AND thread_ts = ? AND ts != ? AND gone_at IS NULL",
            (channel_id, thread_ts, thread_ts),
        )
        return {str(row["ts"]) for row in rows}

    def reply_stats(self, channel_id: str, thread_ts: str) -> tuple[int, str | None]:
        """(count, newest ts) of the replies the archive holds for one thread."""
        row = self._con.execute(
            """
            SELECT COUNT(*), MAX(ts_epoch), MAX(ts) FROM messages
            WHERE channel_id = ? AND thread_ts = ? AND ts != ? AND gone_at IS NULL
            """,
            (channel_id, thread_ts, thread_ts),
        ).fetchone()
        count = int(row[0])
        return count, (str(row[2]) if count else None)

    def active_thread_ts(self, channel_id: str, since_epoch: float) -> set[str]:
        """Threads with any stored activity — parent or reply — since the cutoff.

        This is the polling archiver's answer to a structural blind spot: a
        reply to an old thread changes nothing in a windowed history response,
        so it can only be found by re-asking the thread itself. Re-checking
        every thread that was recently alive bounds the misses to threads that
        fell silent for longer than the recheck window — which the repair
        sweep catches when a lap re-serves the parent.
        """
        rows = self._con.execute(
            """
            SELECT DISTINCT thread_ts FROM messages
            WHERE channel_id = ? AND thread_ts IS NOT NULL AND gone_at IS NULL AND ts_epoch >= ?
            """,
            (channel_id, since_epoch),
        )
        return {str(row["thread_ts"]) for row in rows}

    def thread_ts_with_replies(self, channel_id: str) -> set[str]:
        rows = self._con.execute(
            "SELECT DISTINCT thread_ts FROM messages "
            "WHERE channel_id = ? AND thread_ts IS NOT NULL AND ts != thread_ts AND gone_at IS NULL",
            (channel_id,),
        )
        return {str(row["thread_ts"]) for row in rows}

    # -- reads: messages ---------------------------------------------------------

    def channel_level_messages(self, channel_id: str, *, oldest: float, latest: float) -> Iterator[sqlite3.Row]:
        """Non-gone channel-level rows in the window, newest first."""
        return iter(
            self._con.execute(
                """
                SELECT * FROM messages
                WHERE channel_id = ? AND gone_at IS NULL AND ts_epoch >= ? AND ts_epoch <= ?
                  AND (thread_ts IS NULL OR thread_ts = ts OR subtype = 'thread_broadcast')
                ORDER BY ts_epoch DESC, ts DESC
                """,
                (channel_id, oldest, latest),
            )
        )

    def replies(self, channel_id: str, thread_ts: str) -> list[sqlite3.Row]:
        """Non-gone replies of one thread, oldest first, parent excluded."""
        return list(
            self._con.execute(
                """
                SELECT * FROM messages
                WHERE channel_id = ? AND thread_ts = ? AND ts != ? AND gone_at IS NULL
                ORDER BY ts_epoch ASC, ts ASC
                """,
                (channel_id, thread_ts, thread_ts),
            )
        )

    def message_row(self, channel_id: str, ts: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._con.execute(
            "SELECT * FROM messages WHERE channel_id = ? AND ts = ? AND gone_at IS NULL", (channel_id, ts)
        ).fetchone()
        return row

    def search_candidates(
        self, needle: str, *, channel_ids: list[str], oldest: float, latest: float
    ) -> tuple[list[sqlite3.Row], bool]:
        """Rows that might match, and whether the full-text index chose them.

        The index is never trusted blindly: candidates are re-verified in
        Python, so the only thing FTS changes is how much gets scanned.
        Needles under three characters cannot form a trigram, so they scan.
        """
        placeholders = ",".join("?" for _ in channel_ids)
        window = f"m.gone_at IS NULL AND m.ts_epoch >= ? AND m.ts_epoch <= ? AND m.channel_id IN ({placeholders})"
        params: list[Any] = [oldest, latest, *channel_ids]
        if self.fts_usable and len(needle) >= 3:
            phrase = '"' + needle.replace('"', '""') + '"'
            rows = self._con.execute(
                f"""
                SELECT m.* FROM messages_fts f JOIN messages m ON m.rowid = f.rowid
                WHERE messages_fts MATCH ? AND {window}
                ORDER BY m.ts_epoch ASC, m.ts ASC
                """,
                [phrase, *params],
            )
            return list(rows), True
        rows = self._con.execute(f"SELECT m.* FROM messages m WHERE {window} ORDER BY m.ts_epoch ASC, m.ts ASC", params)
        return list(rows), False

    def sender_names_between(self, *, channel_ids: list[str], oldest: float, latest: float) -> set[str]:
        placeholders = ",".join("?" for _ in channel_ids)
        rows = self._con.execute(
            f"""
            SELECT DISTINCT sender_name FROM messages
            WHERE gone_at IS NULL AND ts_epoch >= ? AND ts_epoch <= ? AND channel_id IN ({placeholders})
            """,
            [oldest, latest, *channel_ids],
        )
        return {str(row["sender_name"]) for row in rows}

    def last_activity_by_channel(self) -> dict[str, str]:
        """Newest non-gone message ts per conversation — the `channels` column."""
        rows = self._con.execute(
            """
            SELECT channel_id, ts FROM messages
            WHERE gone_at IS NULL
            GROUP BY channel_id HAVING ts_epoch = MAX(ts_epoch)
            """
        )
        return {str(row["channel_id"]): str(row["ts"]) for row in rows}

    # -- files ---------------------------------------------------------------------

    def upsert_file(
        self,
        *,
        file_id: str,
        name: str | None,
        mimetype: str | None,
        filetype: str | None,
        size: int | None,
        mode: str | None,
        permalink: str | None,
        url_private: str | None,
        now: float,
    ) -> None:
        """Record a file's metadata, preserving any bytes already downloaded.

        A tombstone is Slack's own deletion marker, so seeing one sets
        ``gone_at`` — but keeps the stub from clobbering real metadata an
        earlier sync recorded, because the archived copy is still servable.
        """
        existing = self._con.execute("SELECT mode FROM files WHERE id = ?", (file_id,)).fetchone()
        if mode == "tombstone":
            if existing is None:
                self._con.execute(
                    "INSERT INTO files (id, name, mimetype, filetype, size, mode, permalink, url_private, gone_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (file_id, name, mimetype, filetype, size, mode, permalink, url_private, now),
                )
            else:
                self._con.execute(
                    "UPDATE files SET mode = 'tombstone', gone_at = COALESCE(gone_at, ?) WHERE id = ?",
                    (now, file_id),
                )
            return
        self._con.execute(
            """
            INSERT INTO files (id, name, mimetype, filetype, size, mode, permalink, url_private, gone_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT (id) DO UPDATE SET
              name = excluded.name, mimetype = excluded.mimetype, filetype = excluded.filetype,
              size = excluded.size, mode = excluded.mode, permalink = excluded.permalink,
              url_private = excluded.url_private, gone_at = NULL
            """,
            (file_id, name, mimetype, filetype, size, mode, permalink, url_private),
        )

    def link_file(self, channel_id: str, ts: str, file_id: str) -> None:
        self._con.execute(
            "INSERT OR IGNORE INTO message_files (channel_id, ts, file_id) VALUES (?, ?, ?)",
            (channel_id, ts, file_id),
        )

    def file_row(self, file_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._con.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return row

    def local_path_of(self, file_id: str) -> Path | None:
        """Where this file's bytes are on disk, if the archive has them."""
        row = self.file_row(file_id)
        if row is None or row["local_path"] is None:
            return None
        resolved = resolve_media_path(str(row["local_path"]), self.media_dir)
        return resolved if resolved.is_file() else None

    def set_local_path(self, file_id: str, path: str, now: float) -> None:
        self._con.execute("UPDATE files SET local_path = ?, downloaded_at = ? WHERE id = ?", (path, now, file_id))

    def download_queue(self, *, tiers: frozenset[str], max_bytes: int | None) -> list[sqlite3.Row]:
        """Files whose bytes should be fetched this run.

        The queue is computed over the whole table, not this run's messages,
        so a failed or interrupted download heals on the next sync. A stored
        ``local_path`` counts only if the bytes are actually on disk at the
        expected size — a path is a claim, the file is the fact.
        """
        if not tiers:
            return []
        rows = self._con.execute(
            """
            SELECT * FROM files
            WHERE url_private IS NOT NULL AND gone_at IS NULL
              AND (mode IS NULL OR mode NOT IN ('external', 'tombstone'))
            ORDER BY id
            """
        )
        queue: list[sqlite3.Row] = []
        for row in rows:
            if media_tier(row["mimetype"]) not in tiers:
                continue
            size = row["size"]
            if max_bytes is not None and size is not None and int(size) > max_bytes:
                continue
            if row["local_path"] is not None:
                on_disk = resolve_media_path(str(row["local_path"]), self.media_dir)
                if on_disk.is_file() and (size is None or on_disk.stat().st_size == int(size)):
                    continue
            queue.append(row)
        return queue
