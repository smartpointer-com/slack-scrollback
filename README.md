# slack-scrollback

Read-only Slack history and search for LLM agents, over a bot token, as
deterministic CLI calls.

An agent with shell access can already talk to Slack; what it usually cannot do
is *read what was said*. `slack-scrollback` fills that gap and nothing else: six
subcommands that fetch channel history, follow a thread, search messages, list
what is visible, maintain a local archive, and turn a shared file into local
bytes. It cannot post, edit, react, or delete — that is enforced in code, not by
convention.

```
$ slack-scrollback history '#general' --since today
[2026-07-15 09:02] alice: morning — deploy is green
[2026-07-15 09:14] bob: nice. shipping the billing fix today?
  [2026-07-15 09:20] alice: yes, after standup
[2026-07-15 11:40] carol: [file: q3-plan.pdf]
```

- **Read-only by construction.** An explicit allowlist of Slack methods is
  checked before any request is built, so a token that happens to carry
  `chat:write` still cannot write. See [Read-only](#read-only).
- **Bot tokens only.** A bot sees what it was invited to. A user token would see
  whatever the person can see, which is a different and much larger boundary, so
  user tokens are rejected outright.
- **Stateless reads, one well-marked exception.** The read commands fetch Slack
  fresh by default. `sync` owns the only state — a local archive it alone
  writes — and any answer served from it says so. See [The archive](#the-archive).
- **No dependencies.** Pure Python 3.11+ standard library (SQLite included).
  The venv exists only to hold the test and lint tooling.

## Install

```sh
make build          # produces ./slack-scrollback — one self-contained file
./slack-scrollback channels
```

That artifact is the whole tool: a stdlib [zipapp](https://docs.python.org/3/library/zipapp.html),
about 80 KB, with no dependencies to resolve. **It installs by being copied.**

```sh
cp slack-scrollback ~/.local/bin/          # or anywhere on PATH
scp slack-scrollback other-host:/usr/local/bin/
```

No clone, no venv, no `pip`, no container on the target — only a `python3` of
3.11 or newer.

### The shebang, if the target's python3 is old

The artifact ships `#!/usr/bin/env python3`, which resolves to whatever `python3`
comes first on the *running user's* `PATH`. On macOS that is `/usr/bin/python3`,
still 3.9, and the tool refuses to run on it — loudly, naming the fix, rather
than misbehaving. Two ways out:

```sh
# Bake a known-good interpreter into the copy you distribute:
make build PYTHON_SHEBANG=/opt/homebrew/bin/python3

# …or just call it with one:
/opt/homebrew/bin/python3 slack-scrollback channels
```

This matters when the tool is installed for a *different* user — a service
account or daemon — whose `PATH` is not yours.

### Working on the tool

```sh
make install     # creates .venv and installs the package plus pinned dev tools
make all         # install + lint + test + build
```

`make` is the only entry point: `install`, `build`, `lint`, `test`, `fmt`,
`clean`, `all`. During development `.venv/bin/slack-scrollback` runs the code in
place, without rebuilding.

## Set up the Slack app

### 1. Create an app and add scopes

At [api.slack.com/apps](https://api.slack.com/apps) create an app for your
workspace, then under **OAuth & Permissions → Bot Token Scopes** add:

| Scope | Why |
|---|---|
| `channels:history` | read public channel messages |
| `channels:read` | list public channels |
| `groups:history` | read private channel messages |
| `groups:read` | list private channels |
| `im:history` | read DMs |
| `im:read` | list DMs |
| `mpim:history` | read group DMs |
| `mpim:read` | list group DMs |
| `users:read` | turn user IDs into names |
| `files:read` | optional — only for media download in `sync` |
| `channels:join` | optional — only for the join-everything step below |

Install the app to the workspace and copy the **Bot User OAuth Token**
(`xoxb-…`).

Do **not** bother with `search:read` or the `search:read.*` scopes. They do not
help — see [Why search is local](#why-search-is-local).

### 2. Supply the token

Any one of, in order of precedence:

```sh
slack-scrollback channels --token xoxb-your-token   # flag
export SLACK_BOT_TOKEN=xoxb-your-token              # environment
echo 'SLACK_BOT_TOKEN=xoxb-your-token' > ~/.config/slack-scrollback.cfg
```

With no `--config`, two locations are tried in order, and the first that exists
wins:

| Path | For |
|---|---|
| `~/.config/slack-scrollback.cfg` | the conventional home for configuration |
| `~/.secrets/slack-scrollback.env` | the habit of keeping credentials in one narrowly-permissioned directory |

`$SLACK_SCROLLBACK_CONFIG` overrides both, as does `--config PATH`.

The file is one `KEY=VALUE` per line. A line whose first non-blank character is
`#` is a comment; `#` elsewhere is part of the value. It is data, not a shell
fragment: no interpolation, no `export`, no substitution, so a value like `$HOME`
stays those five characters.

#### A token that already lives somewhere else

If the token is already held by another tool — a secret store, a password
manager's export, a provisioning artefact — name that file instead of copying out
of it. A copy is not merely untidy: it goes stale the moment the token is
rotated, and silently.

```sh
# ~/.config/slack-scrollback.cfg — holds a path, not a credential
SLACK_BOT_TOKEN_JSON_PATH=/path/to/secrets.json
```

Any JSON object with a top-level `slack_bot_token` string works; nothing else in
the file is read. `$SLACK_BOT_TOKEN_JSON_PATH` does the same from the
environment. It is consulted last, so an explicit token always wins.

The token is sent in an `Authorization` header and never appears in output, logs,
or error messages — including when the config file itself is malformed, where the
offending line is reported by number rather than quoted back.

### 3. Give the bot something to see

**Membership is the access boundary.** The bot reads a conversation only if it is
in it. Invite it by typing `/invite @your-bot-name` in a channel.

To add it to every public channel at once — this **writes** (it joins channels),
so it is deliberately not part of the tool, and it needs `channels:join`:

Save as `join-all.sh` and run it with `sh join-all.sh`:

```sh
#!/bin/sh
# Joins the bot to every public channel it is not already in. Needs channels:join.
# printf '%s' rather than echo: echo mangles backslashes in JSON under some shells.
set -eu
: "${SLACK_BOT_TOKEN:?set SLACK_BOT_TOKEN to your xoxb- token}"

cursor=''
while : ; do
  page=$(curl -sS -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    "https://slack.com/api/conversations.list?types=public_channel&exclude_archived=true&limit=200&cursor=$cursor")

  printf '%s' "$page" | python3 -c '
import json, sys
page = json.load(sys.stdin)
if not page.get("ok"):
    sys.exit("slack error: " + str(page.get("error")))
for c in page.get("channels", []):
    if not c.get("is_member"):
        print(c["id"], c["name"])
' > /tmp/slack-join-todo

  while read -r id name; do
    curl -sS -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
      -X POST -d "channel=$id" https://slack.com/api/conversations.join > /dev/null
    echo "joined #$name"
  done < /tmp/slack-join-todo

  cursor=$(printf '%s' "$page" | python3 -c \
    'import json,sys; print((json.load(sys.stdin).get("response_metadata") or {}).get("next_cursor",""))')
  [ -n "$cursor" ] || break
done
rm -f /tmp/slack-join-todo
```

Private channels, DMs, and group DMs cannot be joined this way — someone has to
invite the bot.

## Use

```sh
slack-scrollback channels                                   # what can be read
slack-scrollback history '#general' --since today           # a channel today
slack-scrollback history '@alice' --since 7d                # a DM
slack-scrollback thread 'https://acme.slack.com/archives/C0EXAMPLE1/p1700000000123456'
slack-scrollback search 'budget' --in '#general' --since 30d
slack-scrollback search 'budget' --from '@alice' --json
slack-scrollback sync                                       # update the local archive
slack-scrollback file 'https://acme.slack.com/files/U0EXAMPLE1/F0EXAMPLE1/plan.pdf'
```

Quote channel names: an unquoted `#general` is a comment to the shell.

| Command | What it does | Its own flags |
|---|---|---|
| `channels` | Every readable conversation with its kind, member count, and real last-activity time, most recent first. | `--no-activity` |
| `history <channel>` | Messages in chronological order, newest at the bottom. Thread replies are fetched and indented under their parent. | `--since`, `--until`, `--limit`, `--no-threads` |
| `thread <permalink \| channel ts>` | One thread in full. A permalink to any reply resolves to the whole thread. | `--limit` |
| `search <query>` | Case-insensitive substring match. Over the archive when one exists (whole history, instant); over freshly-fetched history otherwise. `--from` matches any part of a name. | `--in`, `--from`, `--since`, `--until`, `--limit` |
| `sync` | Mirror everything the bot can read into the local archive: messages, threads, edits, deletions, file bytes. The only command that writes anything. | `--full`, `--recheck`, `--media`, `--media-max-bytes` |
| `file <id \| permalink>` | Print a local path to a shared file's bytes — from the archive when it has them, downloaded from Slack otherwise. | `--out`, `--live` |

Every subcommand also takes `--json`, `--token`, `--config`, `--archive-dir`
and `--timeout`; the four read commands also take `--links` and the backend
selectors `--live`/`--archive` (see [The archive](#the-archive)). `--limit`
defaults to 200.

`--since` and `--until` accept `today`, `yesterday`, `7d`, `24h`, `30m`,
`2026-01-31`, or `2026-01-31T14:00`. A bare date as `--until` includes that whole
day. Times are local, and a date resolves against the UTC offset in force on that
date rather than today's, so a window does not shift across a daylight saving
boundary.

Output is capped at `--limit` messages. When something is dropped the last line
says so and names the flag that lifts the cap, so an agent can always tell it did
not get everything. Under `--json` the same trailers arrive as
`{"type": "notice", ...}` records alongside the `{"type": "message", ...}` ones —
a truncated read never passes for a complete one.

In JSON output a message's `files` field is a list of objects —
`{"id", "name", "mimetype", "size", "permalink", "local_path"}` — so an agent
can hand an `id` or `permalink` straight to the `file` command. `local_path` is
filled whenever the archive holds the file's bytes, on live and archive reads
alike; without an archive it is null. `url_private` is never emitted, anywhere.

Exit codes: `0` success, `1` a stated error on stderr, `2` bad usage.

## The archive

The read commands above cost requests and reach only what Slack still serves.
`sync` removes both limits: it mirrors everything the bot can read into a local
SQLite archive — messages, thread replies, edits, deletions, file bytes — and
it is the only thing that ever writes that archive.

```sh
slack-scrollback sync           # incremental: new messages, recent edits/deletions, files
slack-scrollback sync --full    # re-read everything reachable; refresh names; re-render
```

Scheduling is deliberately external — cron, launchd, a systemd timer; every 30
minutes is a sensible cadence, with a `--full` pass every week or so.
Overlapping runs are harmless: a second `sync` finds `archive.lock` held and
exits 0. A crashed run changes nothing, because all of a run's writes commit as
a single transaction at the end; re-running is always safe.

### Where it lives

One directory holds everything, so relocating or sharing the archive is a
single setting:

```
~/.local/share/slack-scrollback/
  archive.db                 # SQLite
  media/<FILE_ID>/<name>     # downloaded file bytes
  archive.lock               # one sync at a time
```

Resolution order mirrors the token's: `--archive-dir PATH` (on every
subcommand), then `$SLACK_SCROLLBACK_ARCHIVE_DIR`, then `ARCHIVE_DIR=` in the
config file, then the default above. Mind the names: `--archive-dir` is a
path; `--archive` is a backend selector.

### Which backend answers

| Command | Default | Why |
|---|---|---|
| `history`, `thread` | live | chat is a *now* medium, and these cost 1–2 requests |
| `search` | archive if present, else live | whole-history substring search, instant, no request cost |
| `channels` | live list; activity column from the archive | true last-activity otherwise costs a request per conversation |
| `file` | archive first, live on a miss | bytes without re-downloading |

`--live` forces Slack; `--archive` forces the local archive (and errors,
naming `sync`, if there is none). The two are mutually exclusive, one answer
never mixes backends, and every archive-backed answer ends with its
provenance:

```
[from local archive, synced 2026-07-16 14:32 — pass --live to read Slack directly]
```

Archive-only reads touch no network and need no token at all — a useful
property for agents that should hold no credentials, and for reading an
archive of a workspace that no longer exists.

Window semantics (`--since`/`--until`) are identical across backends, and so
is the matching rule. Archive search does reach *more*, honestly: it covers
thread replies (live search scans channel-level history, where replies are
structurally invisible) and it matches the rendered text — `@alice` — where a
live scan sees the raw `<@U…>` form. Both differences make the archive the
better witness, which is why it is the default when present.

### What sync re-checks, and when

New messages are one problem; the recent past is another, because it is not
settled: messages get edited and deleted after being archived. Each
incremental run therefore re-reads a trailing window — `--recheck`, default
`7d` — in which edits (a changed `edited` timestamp) and deletions are
detected. Deletions are soft: the row is marked gone and disappears from every
read, but nothing is ever erased from the archive, and downloaded bytes stay.

Deletion is an inference from absence, and absence only counts where Slack
demonstrably still serves history: at or above the oldest message the response
actually contained. Below that line — and in a window Slack returned empty — a
missing message looks exactly like one hidden by a retention policy, so it is
left standing. That bias is deliberate: the failure mode is serving a deleted
message a little longer, never marking retained history gone. It is also what
makes "the archive outlives retention" true while syncs keep running.

Thread replies have a structural wrinkle: a new reply to an old thread leaves
no trace in a windowed history fetch. `sync` re-asks every thread whose parent
appeared in the window with moved reply counts, and every thread with any
archived activity inside the window. What that still misses — a thread silent
for longer than the recheck window coming back to life — is picked up by the
next `--full` run.

### Media

File *metadata* is always archived. File *bytes* are downloaded by tier:
`--media` (or `MEDIA_TIERS=` in the config) is a comma-separated subset of
`documents`, `images`, `audio`, `video`, defaulting to `documents,images`;
`none` disables downloads. `--media-max-bytes` (or `MEDIA_MAX_BYTES=`) caps
the per-file size; there is no cap by default — the tier list is what bounds
an archive — and `0` also means uncapped, so a config file's cap can be
lifted from the command line. Every download is verified — HTTP 200, a
non-HTML content type, and a byte count exactly matching Slack's metadata —
before it is stored, and a failed download is retried on the next run.

Files that live outside Slack (`mode: external` — Google Docs shares and the
like) are recorded as metadata only; their bytes were never on Slack. Deleted
files keep their downloaded bytes: that is what an archive is for, and the
`file` command says so when it serves one.

### `file`: from a reference to bytes

```sh
slack-scrollback file F0EXAMPLE1
slack-scrollback file 'https://acme.slack.com/files/U0EXAMPLE1/F0EXAMPLE1/plan.pdf'
```

The first output line is an absolute path to the bytes. If the archive has
them, that is the archive's copy (`source: archive`, no network). Otherwise —
or with `--live` — the file is downloaded to `--out` (default: the current
directory) using metadata the archive holds; a live download never writes into
the archive, because `sync` is the only archive writer and will pick the file
up on its next tick. The file must be known to the archive at least as
metadata: nothing in the read-only method allowlist can resolve a bare file ID,
and the allowlist does not grow.

### For external indexers: `messages_flat`

The archive database is a read surface for other tools, and one view is a
stable, documented contract — a denormalized row stream any indexer can
consume without knowing this tool's schema:

| Column | Meaning |
|---|---|
| `msg_id` | unique: `<channel>:<ts>` for messages, `<channel>:<ts>:<file id>` for files |
| `chat_jid`, `chat_name` | conversation ID and rendered name (`#general`, `@alice`) |
| `ts` | epoch seconds, REAL |
| `sender_name` | resolved display name |
| `text` | rendered message text; empty on file rows |
| `media_type` | `document`, `image`, or `other` — file rows only |
| `filename`, `mime_type`, `local_path` | file rows only, `local_path` set iff bytes are on disk |

Semantics an indexer may rely on: rows are append-mostly; an edit changes
`text` in place; a soft-deleted message or file vanishes from the view (so a
full reconcile against the view prunes it); housekeeping subtypes
(joins/renames/topic changes) never appear; file rows exist only for files
whose bytes are on disk. Stored `local_path` values are absolute but always
contain a `/media/` segment — a reader that wants relocation-proof paths keeps
the part after the last `/media/` and rejoins it to its own configured media
directory, which is exactly what the `file` command does.

Readers should open the database read-only (`mode=ro`, or `immutable=1` for
indexers that must never block); the archive deliberately uses the DELETE
journal mode rather than WAL so that read-only consumers need no write access
to sidecar files, and commits each sync as one transaction to keep torn reads
to a millisecond window. When this SQLite has FTS5, archive search rides a
trigram index (`messages_fts`); when it does not, everything still works by
full scan, and the output says so.

## Read-only

The guarantee rests on three independent layers, all in
[`api.py`](src/slack_scrollback/api.py):

1. **A method allowlist**, checked before a request is constructed. The complete
   set is `auth.test`, `conversations.history`, `conversations.info`,
   `conversations.list`, `conversations.replies` and `users.info` — nothing else,
   and nothing the code does not actually call. Anything else raises before
   touching the network, whatever the token's scopes happen to be. This matters
   in practice: bot tokens are routinely granted `chat:write` and friends for
   unrelated reasons, and the allowlist — not the token — is what makes the tool
   safe.
2. **GET only.** Slack's mutating methods require POST.
3. **An explicit host contract.** API requests go to `slack.com` and a redirect
   anywhere is refused outright, because urllib would otherwise carry the bot
   token to whatever host the `Location` named. Media download (`sync` and
   `file`) widens this to exactly three hosts, each with its own rule, measured
   live rather than assumed: `files.slack.com` is sent the token and serves
   image bytes directly; document downloads answer with a redirect, and exactly
   one hop is followed, only when it targets
   `https://slack-files.com/files-pri-safe/…` — Slack's signed CDN — and that
   second request carries **no token**, because the URL's signature alone
   authorizes it. Any other redirect target (the workspace login page is the
   one Slack actually uses to say "no access") is refused with nothing fetched.
   Every download is verified — status, content type, exact byte count against
   Slack's metadata — before it is stored, so a login page cannot be archived
   as a PDF. No telemetry, no update checks, no fourth host, ever.

`sync` grants the allowlist nothing new: it reads through the same six methods
listed above. All of this is unit-tested: thirty write methods are asserted
both to be refused *and* to leave no request attempted, the allowlist is
asserted to grant nothing the code does not use, redirect refusal is exercised
on both the API and download paths, and the download tests assert that
`slack-files.com` never receives an `Authorization` header. `make test` proves
it.

## Why search is local

Slack has a server-side search API. **It does not accept bot tokens**, so this
tool does not use it. Verified against a live workspace whose bot token had been
granted every `search:read.*` scope:

| Call | Result |
|---|---|
| `search.messages`, `search.all`, `search.files` | `not_allowed_token_type` |
| `assistant.search.context` (Real-time Search API) | `invalid_action_token` |

`search.messages` is legacy and user-token-only; `search:read` cannot be granted
to a bot at all. The newer `search:read.*` scopes belong to
`assistant.search.context`, which requires an `action_token` that Slack only
mints from a live message or mention event — something a CLI invoked from a shell
can never hold. Granting the scopes changes neither outcome. This is structural,
not a plan or rollout gate.

So `search` reads history through the same allowlisted method as `history` and
matches locally. The trade-offs are honest ones:

- It is substring matching, not ranked or semantic search.
- It costs one pass of history per conversation, which is why it defaults to a
  30-day window. `--in '#channel'` narrows it to a single cheap call.
- It reaches exactly what the bot can read — including private channels and DMs,
  which bot-token search could not have reached even if it did work.

Nothing is indexed or written; each search re-reads Slack.

## Rate limits

`conversations.history` and `conversations.replies` sit in Slack's Tier 3
(50+ requests/minute, up to 1000 messages per request) for **Marketplace apps and
internal custom apps** — which covers the normal case of an app built for your
own workspace.

Apps *commercially distributed outside* the Slack Marketplace are capped at
**1 request per minute, 15 messages per request**. The failure mode is silent:
Slack quietly ignores a larger `limit` rather than returning an error, so a fetch
that should take seconds takes hours with nothing to explain why. The tool
detects that signature — asking for more and receiving exactly 15 with more
pending — and reports it in the output rather than assuming it. An unexpected
notice points at the app's public distribution setting; turning distribution off
restores Tier 3.

HTTP 429 is honoured via `Retry-After`, requests are serial, and names are cached
per run, so a long fetch degrades into slowness rather than failure.

## Known limitations

- **Retention beats the API — but not the archive.** On free Slack plans
  messages older than ~90 days are hidden from the API too; nothing on-demand
  can reach past that. The archive can: everything `sync` captured stays
  readable and searchable after Slack stops serving it.
- **Substring search only** — see above. Archive search is index-accelerated
  but has identical semantics by construction.
- **A revived old thread can lag the archive.** New replies to a thread that
  has been silent longer than the `--recheck` window leave no trace in a
  windowed history fetch, so an incremental `sync` cannot see them; the next
  `--full` run picks them up.
- **Thread expansion costs a request per thread** on live reads. The `--limit`
  cap bounds it; `--no-threads` removes it.
- **`channels` costs a request per conversation** for true last activity —
  only when there is no archive; with one, the column is free and the output
  says where it came from. Slack's own `updated` field tracks channel metadata
  and drifts from real activity by months in both directions, so it is not
  usable as a shortcut. `--no-activity` opts out.
- **`files.list` is deliberately unused.** Measured live, it is untrustworthy:
  a first page claimed three pages of results and later pages came back empty.
  Files are enumerated from message objects instead, which is also why the
  method allowlist needs nothing beyond history reads. Do not "fix" file
  enumeration by reaching for it.

## For agents

[`SKILL.md`](SKILL.md) is the agent-facing counterpart to this file: a selection
description plus one copy-pasteable command per user intent. It is written for
small local models, which do far better with exact recipes than with reference
documentation.

## Development

```sh
make all      # install + lint + test
make test     # pytest — no network; the HTTP layer is stubbed throughout
make lint     # ruff + ruff format --check + mypy --strict
make fmt      # apply formatting and autofixes
make clean    # remove the venv and caches
```

Every example in this repo uses synthetic workspace data (`#general`,
`U0EXAMPLE1`, `acme.slack.com`).

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 SmartPointer AG.
