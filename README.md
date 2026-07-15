# slack-scrollback

Read-only Slack history and search for LLM agents, over a bot token, as
deterministic CLI calls.

An agent with shell access can already talk to Slack; what it usually cannot do
is *read what was said*. `slack-scrollback` fills that gap and nothing else: four
subcommands that fetch channel history, follow a thread, search messages, and
list what is visible. It cannot post, edit, react, or delete — that is enforced
in code, not by convention.

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
- **Stateless.** No database, no index, no cache on disk. Every call reads Slack
  fresh; the only file ever read is the config.
- **No dependencies.** Pure Python 3.11+ standard library. The venv exists only
  to hold the test and lint tooling.

## Install

```sh
make install     # creates .venv and installs the package plus dev tooling
make all         # install + lint + test
```

The CLI lands at `.venv/bin/slack-scrollback`. Put it on `PATH` however suits
you — a symlink into `~/.local/bin`, a shell alias, or by calling the full path.

`make` is the only entry point: `install`, `lint`, `test`, `fmt`, `clean`, `all`.

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

The config file is one `KEY=VALUE` per line. A line whose first non-blank
character is `#` is a comment; `#` elsewhere is part of the value. It is data,
not a shell fragment: no interpolation, no `export`, no substitution, so a value
like `$HOME` stays those five characters. Point elsewhere with `--config PATH`.

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
```

Quote channel names: an unquoted `#general` is a comment to the shell.

| Command | What it does | Its own flags |
|---|---|---|
| `channels` | Every readable conversation with its kind, member count, and real last-activity time, most recent first. Costs one request per conversation. | `--no-activity` |
| `history <channel>` | Messages in chronological order, newest at the bottom. Thread replies are fetched and indented under their parent. | `--since`, `--until`, `--limit`, `--no-threads` |
| `thread <permalink \| channel ts>` | One thread in full. A permalink to any reply resolves to the whole thread. | `--limit` |
| `search <query>` | Case-insensitive substring match over freshly-fetched history. `--from` matches any part of a name. | `--in`, `--from`, `--since`, `--until`, `--limit` |

Every subcommand also takes `--json`, `--links`, `--token`, `--config` and
`--timeout`. `--limit` defaults to 200.

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

Exit codes: `0` success, `1` a stated error on stderr, `2` bad usage.

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
3. **Host pinning** to `slack.com`, enforced both before the request goes out and
   against redirects: a 30x away from `slack.com` is refused rather than
   followed, because urllib would otherwise carry the bot token to whatever host
   the `Location` named. No telemetry, no update checks, no other host, ever.

All three are unit-tested: thirty write methods are asserted both to be refused
*and* to leave no request attempted, the allowlist is asserted to grant nothing
the code does not use, and the redirect refusal is exercised directly.
`make test` proves it.

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

- **Retention beats the API.** On free Slack plans messages older than ~90 days
  are hidden from the API too. Nothing on-demand can reach past that.
- **No archive.** v1 is stateless by design, so it cannot outlive retention. A
  separate `sync` layer that continuously archives into a local store is the
  natural next step, and is deliberately out of scope here.
- **Substring search only** — see above.
- **Thread expansion costs a request per thread.** The `--limit` cap bounds it;
  `--no-threads` removes it.
- **`channels` costs a request per conversation** to report true last activity.
  Slack's own `updated` field tracks channel metadata and drifts from real
  activity by months in both directions, so it is not usable as a shortcut.
  `--no-activity` opts out.

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
