"""End-to-end CLI behaviour around the archive: backend rules, ``sync``, and ``file``.

Every test isolates configuration completely — the config file is pointed at a
nonexistent path, token variables are scrubbed from the environment, and the
archive directory lives under ``tmp_path`` — so no test can read or touch a
real deployment. Live paths run against ``SlackClient`` instances wired to
fake transports; archive paths run against archives built by driving the real
sync machinery over those same fakes.
"""

from __future__ import annotations

import fcntl
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from slack_scrollback import cli
from slack_scrollback.api import SlackClient
from slack_scrollback.errors import UsageError
from tests.conftest import (
    TOKEN,
    FakeFileHost,
    FakeSlack,
    FakeTransport,
    channel,
    file_body,
    make_client,
    message,
    ok,
    run_sync,
    slack_file,
    ts_at,
)

PERMALINK = "https://acme.slack.com/archives/C0EXAMPLE1/p1700000000123456"

FILE_ID = "F0EXAMPLE1"
FILE_NAME = "plan.pdf"
FILE_BYTES = b"abcdef"  # length 6 == the size slack_file() declares
FILE_URL_PRIVATE = f"https://files.slack.com/files-pri/T0EXAMPLE1-{FILE_ID}/{FILE_NAME}"
FILE_PERMALINK = f"https://acme.slack.com/files/U0EXAMPLE1/{FILE_ID}/{FILE_NAME}"
EXTERNAL_URL = "https://docs.google.com/document/d/e2Xample/edit"

#: A --since bound far below the conftest fixture epoch, so archive windows
#: cover the fixture messages no matter what the real clock says.
EPOCH_SINCE = "2000-01-01"

ARCHIVE_TRAILER = "[from local archive, synced "


# -- isolation and plumbing --------------------------------------------------


@pytest.fixture(autouse=True)
def arch_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Scrub every config source and point the archive at tmp_path.

    Autouse so no test in this module can accidentally read the developer's
    real config file, token, or archive.
    """
    monkeypatch.setenv("SLACK_SCROLLBACK_CONFIG", str(tmp_path / "nonexistent.cfg"))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN_JSON_PATH", raising=False)
    directory = tmp_path / "arch"
    monkeypatch.setenv("SLACK_SCROLLBACK_ARCHIVE_DIR", str(directory))
    return directory


def base_fake(*, with_file: bool = True) -> FakeSlack:
    """One channel, one plain message, and optionally one hosted-file message."""
    fake = FakeSlack()
    messages = [message(ts_at(0), "hello there")]
    if with_file:
        messages.append(message(ts_at(60), "the plan", files=[slack_file()]))
    fake.messages = {"C0EXAMPLE1": messages}
    return fake


def sync_archive(fake: FakeSlack, directory: Path, *, with_bytes: bool = False) -> None:
    """Build (or update) an archive from the fake, end to end through the syncer."""
    downloads = FakeFileHost(responses={FILE_URL_PRIVATE: file_body(FILE_BYTES)}) if with_bytes else None
    tiers = frozenset({"documents"}) if with_bytes else frozenset()
    report, archive, _ = run_sync(fake, directory, media_tiers=tiers, downloads=downloads)
    archive.close()
    assert not report.download_failures


def install_live_client(monkeypatch: pytest.MonkeyPatch, handlers: dict[str, Any]) -> tuple[FakeTransport, list[str]]:
    """Route the CLI's SlackClient construction onto a fake transport.

    Returns the transport (whose ``methods`` records every request) and the
    list of constructions, so tests can assert whether a client was built.
    A bot token is placed in the (already scrubbed) environment because every
    live path resolves one before talking.
    """
    transport = FakeTransport(handlers=handlers)
    constructed: list[str] = []

    def factory(token: str, **kwargs: Any) -> SlackClient:
        constructed.append(token)
        return SlackClient(token, transport=transport, sleep=lambda _: None)

    monkeypatch.setattr("slack_scrollback.cli.SlackClient", factory)
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    return transport, constructed


def forbid_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the CLI constructs a SlackClient at all."""

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SlackClient was constructed where an archive read should have sufficed")

    monkeypatch.setattr("slack_scrollback.cli.SlackClient", explode)


def static_search_handlers() -> dict[str, Any]:
    """Handlers that answer a live search regardless of the requested window."""
    return {
        "conversations.list": ok(channels=[channel()]),
        "conversations.history": ok(messages=[message("1700000000.000100", "budget hello")]),
        "users.info": ok(user={"id": "U0EXAMPLE1", "profile": {"display_name": "alice"}}),
    }


# -- backend flags and defaults ----------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["history", "#general", "--archive"],
        ["thread", PERMALINK, "--archive"],
        ["search", "budget", "--archive"],
        ["channels", "--archive"],
    ],
    ids=lambda argv: str(argv[0]),
)
def test_archive_flag_without_an_archive_errors_naming_sync(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forbid_client(monkeypatch)
    code = cli.main(argv)
    captured = capsys.readouterr()
    assert code == 1
    assert "slack-scrollback sync" in captured.err


@pytest.mark.parametrize(
    "argv",
    [["channels"], ["history", "#general"], ["thread", PERMALINK], ["search", "budget"]],
    ids=lambda argv: str(argv[0]),
)
def test_live_and_archive_flags_are_mutually_exclusive(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main([*argv, "--live", "--archive"])
    assert caught.value.code == 2


def test_search_default_uses_the_archive_and_needs_no_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    """A pure-archive read works with no token configured anywhere."""
    sync_archive(base_fake(with_file=False), arch_dir)
    forbid_client(monkeypatch)
    code = cli.main(["search", "hello", "--since", EPOCH_SINCE])
    out = capsys.readouterr().out
    assert code == 0
    assert "hello there" in out
    trailer = out.strip().splitlines()[-1]
    assert trailer.startswith(ARCHIVE_TRAILER)
    assert "pass --live" in trailer


def test_search_falls_back_to_live_when_no_archive_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, constructed = install_live_client(monkeypatch, static_search_handlers())
    code = cli.main(["search", "budget"])
    out = capsys.readouterr().out
    assert code == 0
    assert constructed, "the no-archive fallback must construct a live client"
    assert "budget hello" in out
    assert ARCHIVE_TRAILER not in out


def test_search_live_flag_bypasses_an_existing_archive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    sync_archive(base_fake(with_file=False), arch_dir)
    _, constructed = install_live_client(monkeypatch, static_search_handlers())
    code = cli.main(["search", "budget", "--live"])
    out = capsys.readouterr().out
    assert code == 0
    assert constructed
    assert "budget hello" in out  # only the fake live workspace holds this text
    assert ARCHIVE_TRAILER not in out


def test_history_defaults_to_live_even_when_an_archive_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    fake = base_fake(with_file=False)
    sync_archive(fake, arch_dir)
    transport, constructed = install_live_client(monkeypatch, fake.handlers())
    code = cli.main(["history", "#general"])
    out = capsys.readouterr().out
    assert code == 0
    assert constructed, "history defaults to live: freshness is its point"
    assert "conversations.history" in transport.methods
    assert "hello there" in out
    assert ARCHIVE_TRAILER not in out


def test_history_archive_flag_reads_without_a_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    sync_archive(base_fake(with_file=False), arch_dir)
    forbid_client(monkeypatch)
    code = cli.main(["history", "#general", "--archive"])
    out = capsys.readouterr().out
    assert code == 0
    assert "hello there" in out
    assert out.strip().splitlines()[-1].startswith(ARCHIVE_TRAILER)


def test_channels_default_takes_activity_from_the_archive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    fake = base_fake(with_file=False)
    sync_archive(fake, arch_dir)
    transport, _ = install_live_client(monkeypatch, fake.handlers())
    code = cli.main(["channels"])
    out = capsys.readouterr().out
    assert code == 0
    assert "conversations.list" in transport.methods
    assert "conversations.history" not in transport.methods, "the archive must replace per-conversation lookups"
    assert "last activity from local archive" in out


def test_channels_live_flag_forces_per_conversation_lookups(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    fake = base_fake(with_file=False)
    sync_archive(fake, arch_dir)
    transport, _ = install_live_client(monkeypatch, fake.handlers())
    code = cli.main(["channels", "--live"])
    out = capsys.readouterr().out
    assert code == 0
    assert "conversations.history" in transport.methods
    assert "local archive" not in out


def test_channels_archive_flag_constructs_no_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    sync_archive(base_fake(with_file=False), arch_dir)
    forbid_client(monkeypatch)
    code = cli.main(["channels", "--archive"])
    out = capsys.readouterr().out
    assert code == 0
    assert "#general" in out
    assert out.strip().splitlines()[-1].startswith(ARCHIVE_TRAILER)


def test_channels_no_activity_skips_lookups_and_provenance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    fake = base_fake(with_file=False)
    sync_archive(fake, arch_dir)
    transport, _ = install_live_client(monkeypatch, fake.handlers())
    code = cli.main(["channels", "--no-activity"])
    out = capsys.readouterr().out
    assert code == 0
    assert "#general" in out
    assert "conversations.history" not in transport.methods
    assert "local archive" not in out


def test_history_archive_output_is_live_output_plus_provenance(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    """The two backends render byte-identically, modulo the trailer."""
    fake = base_fake()
    sync_archive(fake, arch_dir)
    forbid_client(monkeypatch)
    assert cli.main(["history", "#general", "--archive"]) == 0
    archive_lines = capsys.readouterr().out.splitlines()
    install_live_client(monkeypatch, fake.handlers())
    assert cli.main(["history", "#general"]) == 0
    live_lines = capsys.readouterr().out.splitlines()
    assert archive_lines[-1].startswith(ARCHIVE_TRAILER)
    assert archive_lines[:-1] == live_lines


# -- sync ---------------------------------------------------------------------


def test_sync_builds_the_archive_and_prints_a_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    fake = base_fake()
    client, _ = make_client(fake.handlers())
    monkeypatch.setattr("slack_scrollback.cli.SlackClient", lambda token, **kwargs: client)
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    code = cli.main(["sync", "--archive-dir", str(arch_dir), "--media", "none"])
    out = capsys.readouterr().out
    assert code == 0
    assert "synced 1 conversations" in out
    assert str(arch_dir) in out
    assert (arch_dir / "archive.db").is_file()


def test_sync_exits_cleanly_when_the_lock_is_held(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    forbid_client(monkeypatch)  # a locked-out run must never get as far as Slack
    arch_dir.mkdir(parents=True)
    with open(arch_dir / "archive.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            code = cli.main(["sync", "--archive-dir", str(arch_dir)])
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
    out = capsys.readouterr().out
    assert code == 0
    assert "nothing to do" in out


def test_sync_rejects_a_malformed_recheck_duration(capsys: pytest.CaptureFixture[str], arch_dir: Path) -> None:
    code = cli.main(["sync", "--archive-dir", str(arch_dir), "--recheck", "bogus"])
    err = capsys.readouterr().err
    assert code == 1
    assert "--recheck" in err
    assert "duration" in err


def test_sync_rejects_unknown_media_tiers_naming_the_valid_ones(
    capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    code = cli.main(["sync", "--archive-dir", str(arch_dir), "--media", "bogus"])
    err = capsys.readouterr().err
    assert code == 1
    assert "documents" in err
    assert "video" in err


# -- file: target parsing -------------------------------------------------------


def test_parse_file_target_accepts_a_bare_id() -> None:
    assert cli.parse_file_target("F0EXAMPLE123") == "F0EXAMPLE123"


def test_parse_file_target_takes_the_file_segment_of_a_permalink() -> None:
    url = "https://acme.slack.com/files/U0EXAMPLE1/F0EXAMPLE123/plan.pdf"
    assert cli.parse_file_target(url) == "F0EXAMPLE123"


def test_parse_file_target_rejects_an_id_like_filename_containing_a_dot() -> None:
    with pytest.raises(UsageError):
        cli.parse_file_target("https://acme.slack.com/downloads/F0EXAMPLE99.pdf")


def test_parse_file_target_junk_names_the_expected_shape() -> None:
    with pytest.raises(UsageError) as caught:
        cli.parse_file_target("not-a-file-reference")
    assert "F0EXAMPLE1" in str(caught.value)


# -- file: serving bytes ---------------------------------------------------------


def test_file_archive_hit_serves_bytes_without_token_or_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    sync_archive(base_fake(), arch_dir, with_bytes=True)
    forbid_client(monkeypatch)  # and the isolation fixture guarantees no token anywhere
    code = cli.main(["file", FILE_ID, "--archive-dir", str(arch_dir)])
    out = capsys.readouterr().out
    assert code == 0
    served = Path(out.splitlines()[0])
    assert served.is_absolute()
    assert served.is_file()
    assert served.read_bytes() == FILE_BYTES
    assert "source: archive" in out


def test_file_json_emits_one_stable_object(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    sync_archive(base_fake(), arch_dir, with_bytes=True)
    forbid_client(monkeypatch)
    code = cli.main(["file", FILE_ID, "--archive-dir", str(arch_dir), "--json"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert code == 0
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert set(record) == {"id", "name", "path", "mimetype", "size", "source", "permalink"}
    assert record["id"] == FILE_ID
    assert record["name"] == FILE_NAME
    assert record["size"] == len(FILE_BYTES)
    assert record["source"] == "archive"
    assert record["permalink"] == FILE_PERMALINK
    assert Path(record["path"]).is_file()


def test_file_resolves_paths_inside_a_relocated_archive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path, tmp_path: Path
) -> None:
    """Stored paths are rejoined at the last /media/, so a moved archive still serves."""
    sync_archive(base_fake(), arch_dir, with_bytes=True)
    relocated = tmp_path / "relocated"
    shutil.copytree(arch_dir, relocated)
    forbid_client(monkeypatch)
    code = cli.main(["file", FILE_ID, "--archive-dir", str(relocated)])
    first = capsys.readouterr().out.splitlines()[0]
    assert code == 0
    assert first.startswith(str(relocated / "media"))
    assert Path(first).read_bytes() == FILE_BYTES


def test_file_unknown_id_names_sync(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    sync_archive(base_fake(), arch_dir)
    forbid_client(monkeypatch)
    code = cli.main(["file", "F0MISSING99", "--archive-dir", str(arch_dir)])
    err = capsys.readouterr().err
    assert code == 1
    assert "slack-scrollback sync" in err


def test_file_external_refuses_and_names_the_external_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    fake = base_fake(with_file=False)
    external = slack_file(
        file_id="F0EXTERNAL1",
        name="notes.gdoc",
        mimetype="application/vnd.google-apps.document",
        mode="external",
        url_private=EXTERNAL_URL,
    )
    fake.messages["C0EXAMPLE1"].append(message(ts_at(120), "shared a doc", files=[external]))
    sync_archive(fake, arch_dir)
    forbid_client(monkeypatch)
    code = cli.main(["file", "F0EXTERNAL1", "--archive-dir", str(arch_dir)])
    err = capsys.readouterr().err
    assert code == 1
    assert "external" in err
    assert EXTERNAL_URL in err


def test_file_gone_on_slack_still_serves_bytes_with_a_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    fake = base_fake()
    sync_archive(fake, arch_dir, with_bytes=True)
    # Slack replaces a deleted file with a tombstone stub; the next sync records it.
    fake.messages["C0EXAMPLE1"][1]["files"] = [{"id": FILE_ID, "mode": "tombstone"}]
    sync_archive(fake, arch_dir)
    forbid_client(monkeypatch)
    code = cli.main(["file", FILE_ID, "--archive-dir", str(arch_dir)])
    out = capsys.readouterr().out
    assert code == 0
    assert Path(out.splitlines()[0]).read_bytes() == FILE_BYTES
    assert "deleted" in out


def test_file_live_fallback_downloads_to_out_and_never_the_archive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path, tmp_path: Path
) -> None:
    sync_archive(base_fake(), arch_dir)  # metadata only: the archive holds no bytes
    forbid_client(monkeypatch)  # a live download needs no API client either
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    fetched: list[str] = []

    def fake_download_to(
        url: str, dest: Path, *, token: str, label: str, expected_size: int | None, timeout: float
    ) -> int:
        fetched.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        size = int(expected_size or 0)
        dest.write_bytes(b"x" * size)
        return size

    monkeypatch.setattr("slack_scrollback.cli.download_to", fake_download_to)
    out_dir = tmp_path / "downloads"
    out_dir.mkdir()

    code = cli.main(["file", FILE_ID, "--archive-dir", str(arch_dir), "--out", str(out_dir)])
    out = capsys.readouterr().out
    dest = out_dir / FILE_NAME
    assert code == 0
    assert out.splitlines()[0] == str(dest)
    assert "source: live" in out
    assert dest.read_bytes() == b"x" * len(FILE_BYTES)
    assert fetched == [FILE_URL_PRIVATE]
    assert not any((arch_dir / "media").rglob("*")), "sync is the only archive writer"

    # The destination now exists; a rerun must refuse rather than overwrite.
    code = cli.main(["file", FILE_ID, "--archive-dir", str(arch_dir), "--out", str(out_dir)])
    err = capsys.readouterr().err
    assert code == 1
    assert "already exists" in err


# -- url_private must never surface -----------------------------------------------


def test_url_private_never_appears_in_any_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    fake = base_fake()
    sync_archive(fake, arch_dir, with_bytes=True)
    install_live_client(monkeypatch, fake.handlers())
    probes = [
        ["history", "#general", "--json"],  # live backend
        ["channels", "--json"],  # live list, archive activity
        ["history", "#general", "--archive", "--json"],
        ["search", "plan", "--since", EPOCH_SINCE, "--json"],  # archive backend
        ["file", FILE_ID],
        ["file", FILE_ID, "--json"],
    ]
    for argv in probes:
        assert cli.main(argv) == 0, argv
        captured = capsys.readouterr()
        assert "files-pri" not in captured.out, argv
        assert "files-pri" not in captured.err, argv


# -- structured file references in output -------------------------------------------


def _message_file_refs(stdout: str) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in stdout.splitlines()]
    with_files = [r for r in records if r.get("type") == "message" and r.get("files")]
    assert len(with_files) == 1
    refs = with_files[0]["files"]
    assert isinstance(refs, list)
    return [ref for ref in refs if isinstance(ref, dict)]


def test_history_json_live_enriches_files_from_the_archive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    fake = base_fake()
    sync_archive(fake, arch_dir, with_bytes=True)
    _, constructed = install_live_client(monkeypatch, fake.handlers())
    assert cli.main(["history", "#general", "--json"]) == 0
    (ref,) = _message_file_refs(capsys.readouterr().out)
    assert constructed, "the default history backend is live"
    assert set(ref) == {"id", "name", "mimetype", "size", "permalink", "local_path"}
    assert ref["id"] == FILE_ID
    assert ref["name"] == FILE_NAME
    assert ref["mimetype"] == "application/pdf"
    assert ref["size"] == len(FILE_BYTES)
    assert ref["permalink"] == FILE_PERMALINK
    assert ref["local_path"] is not None
    assert ref["local_path"].startswith(str(arch_dir / "media"))
    assert Path(ref["local_path"]).is_file()


def test_history_json_file_shape_is_intact_without_an_archive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = base_fake()
    install_live_client(monkeypatch, fake.handlers())
    assert cli.main(["history", "#general", "--json"]) == 0
    (ref,) = _message_file_refs(capsys.readouterr().out)
    assert set(ref) == {"id", "name", "mimetype", "size", "permalink", "local_path"}
    assert ref["local_path"] is None


def test_links_appends_the_file_permalink_to_the_message_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = base_fake()
    install_live_client(monkeypatch, fake.handlers())
    assert cli.main(["history", "#general", "--links"]) == 0
    out = capsys.readouterr().out
    line = next(text for text in out.splitlines() if "the plan" in text)
    assert line.endswith(FILE_PERMALINK)


# -- a damaged archive degrades, never a traceback ------------------------------


def corrupt_archive(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "archive.db").write_bytes(b"this is not a sqlite database at all")


def test_a_corrupt_archive_fails_archive_reads_with_a_next_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    """Torn copies and disk damage happen; the answer is the tool's own error
    idiom with a remedy, never a raw sqlite3 traceback."""
    corrupt_archive(arch_dir)
    forbid_client(monkeypatch)

    code = cli.main(["history", "general", "--archive"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("error: ")
    assert "slack-scrollback sync" in captured.err


def test_a_corrupt_archive_does_not_break_a_live_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    """Enrichment is auxiliary: a live history must still answer when the
    archive beside it is damaged, minus local_path, plus a stderr note."""
    corrupt_archive(arch_dir)
    fake = base_fake()
    install_live_client(monkeypatch, fake.handlers())

    code = cli.main(["history", "general", "--since", EPOCH_SINCE, "--json"])
    captured = capsys.readouterr()
    assert code == 0
    records = [json.loads(line) for line in captured.out.splitlines()]
    assert any(record.get("type") == "message" for record in records)
    assert "could not be read" in captured.err


def test_a_corrupt_archive_fails_the_search_default_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    """The archive-if-present default must not silently fall back to live —
    a broken archive is news the operator needs, and --live is the bypass."""
    corrupt_archive(arch_dir)
    forbid_client(monkeypatch)

    code = cli.main(["search", "hello"])
    captured = capsys.readouterr()
    assert code == 1
    assert "--live" in captured.err


# -- the progress ticker ----------------------------------------------------------


class FakeTty:
    """A terminal-shaped stream: records writes, claims to be a tty."""

    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, text: str) -> None:
        self.written.append(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


def test_the_ticker_rewrites_one_line_and_wipes_itself() -> None:
    """Progress is watched, not kept: every update overwrites the last, and
    nothing of the ticker survives into scrollback above the report."""
    stream = FakeTty()
    ticker = cli.Ticker(stream)  # type: ignore[arg-type]
    ticker("syncing #general (1/13)")
    ticker("syncing #random (2/13)")
    ticker.finish()

    assert all(chunk.startswith("\r\x1b[K") for chunk in stream.written)
    assert "syncing #random (2/13)" in stream.written[1]
    assert stream.written[-1] == "\r\x1b[K"
    ticker.finish()
    assert len(stream.written) == 3  # a clean ticker has nothing more to wipe


def test_the_ticker_truncates_to_the_terminal_width(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setattr("slack_scrollback.cli.shutil.get_terminal_size", lambda: os.terminal_size((20, 24)))
    stream = FakeTty()
    ticker = cli.Ticker(stream)  # type: ignore[arg-type]
    ticker("a line much longer than twenty columns")
    payload = stream.written[0].removeprefix("\r\x1b[K")
    assert len(payload) <= 19
    assert payload.endswith("…")


def test_sync_draws_no_ticker_when_stderr_is_not_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    """A scheduled run's log must hold the report and nothing else — no
    carriage returns, no escape codes."""
    fake = base_fake(with_file=False)
    install_live_client(monkeypatch, fake.handlers())

    code = cli.main(["sync", "--media", "none"])
    captured = capsys.readouterr()
    assert code == 0
    assert "\r" not in captured.err and "\x1b" not in captured.err


def test_sync_quiet_suppresses_the_ticker_even_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    fake = base_fake(with_file=False)
    install_live_client(monkeypatch, fake.handlers())
    monkeypatch.setattr("slack_scrollback.cli.sys.stderr.isatty", lambda: True, raising=False)

    code = cli.main(["sync", "--media", "none", "--quiet"])
    captured = capsys.readouterr()
    assert code == 0
    assert "\r" not in captured.err and "\x1b" not in captured.err
    assert "synced" in captured.out


def test_sync_against_a_read_only_shared_archive_errors_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arch_dir: Path
) -> None:
    """The shared-archive arrangement is owner-writes, everyone-else-reads; a
    reader who runs sync anyway gets one prescriptive line, not a traceback."""
    fake = base_fake(with_file=False)
    sync_archive(fake, arch_dir)
    arch_dir.chmod(0o500)
    forbid_client(monkeypatch)
    try:
        code = cli.main(["sync"])
    finally:
        arch_dir.chmod(0o700)
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("error: cannot write to the archive directory")
    assert "owner runs sync" in captured.err
    assert "Traceback" not in captured.err


def test_run_exits_with_mains_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The zipapp enters through run(): if it merely returned main()'s code,
    the built artifact would exit 0 on every handled error and a scheduler
    would read failure as success."""
    monkeypatch.setattr("sys.argv", ["slack-scrollback", "history", "general", "--archive"])
    with pytest.raises(SystemExit) as caught:
        cli.run()
    assert caught.value.code == 1
