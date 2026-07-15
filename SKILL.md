---
name: slack-scrollback
description: Reads and searches Slack history — channels, DMs, and threads — with the read-only slack-scrollback CLI. Use when asked what was said in a Slack channel, what happened on Slack today, to find Slack messages about a topic, what someone said on Slack, or to summarize a Slack thread.
---

# Slack history and search (read-only)

`slack-scrollback` reads Slack. It cannot post, edit, react, or delete —
attempting to is refused in code, so there is no way to change anything.

Four commands. Each one answers a whole question in a single call. Never chain
two commands when one will do.

| The request | The command |
|---|---|
| "what was said in #general today?" | `slack-scrollback history '#general' --since today` |
| "what's been happening in #general?" | `slack-scrollback history '#general' --since 7d` |
| "what was said yesterday?" | `slack-scrollback history '#general' --since yesterday --until yesterday` |
| "find messages about the budget" | `slack-scrollback search 'budget'` |
| "anything about the budget in #general last month?" | `slack-scrollback search 'budget' --in '#general' --since 30d` |
| "what did Alice say about the budget?" | `slack-scrollback search 'budget' --from 'alice'` |
| "summarize this thread: <permalink>" | `slack-scrollback thread '<permalink>'` |
| "what channels are there?" / "what can you see?" | `slack-scrollback channels` |
| "what did Alice say in her DM?" | `slack-scrollback history '@alice' --since 7d` |

**Always put the channel name in single quotes.** A shell treats a bare
`#general` as a comment and throws the argument away. `'#general'` and
`'@Alice'` are safe. A bare `general` (no `#`) also works.

## Recipes

### What was said in a channel

```sh
slack-scrollback history '#general' --since today
```
```
[2026-07-15 09:02] alice: morning — deploy is green
[2026-07-15 09:14] bob: nice. shipping the billing fix today?
  [2026-07-15 09:20] alice: yes, after standup
[2026-07-15 11:40] carol: [file: q3-plan.pdf]
```
Oldest first, so the last line is the newest. Indented lines are thread replies.

`--since` and `--until` both take `today`, `yesterday`, `7d`, `24h`, `30m`, or a
date like `2026-01-31`. `--since` alone runs up to now, which is usually what is
wanted; add `--until` only to close the far end of the window:

```sh
slack-scrollback history '#general' --since yesterday --until yesterday   # just yesterday
slack-scrollback history '#general' --since 2026-01-01 --until 2026-01-31 # just January
```

### Find messages about a topic

```sh
slack-scrollback search 'budget'
```
```
[2026-06-30 15:36] #finance alice: the budget is approved
[2026-07-02 11:12] #general bob: where did the budget doc go?
```
Searches every conversation the bot can read, over the last 30 days by default.
The channel is shown because results span channels. To go further back or narrow
down:

```sh
slack-scrollback search 'budget' --since 90d          # look further back
slack-scrollback search 'budget' --in '#finance'      # one channel (much faster)
slack-scrollback search 'budget' --from 'alice'       # only what Alice said
```

Matching is plain case-insensitive substring — `budget` finds `Budget` and
`budgeting`. It is not a fuzzy or semantic search, so search for a word that
would literally appear in the message. If a search returns nothing, try a shorter
or more common word before concluding nothing was said.

`--from` matches any part of a person's name, so `alice` finds "Alice Jones".
When nobody matches, the output names the people who did speak — re-run with one
of those.

### Read a whole thread

```sh
slack-scrollback thread 'https://acme.slack.com/archives/C0EXAMPLE1/p1700000000123456'
```
```
[2026-07-15 09:14] bob: shipping the billing fix today?
  [2026-07-15 09:20] alice: yes, after standup
  [2026-07-15 09:31] carol: ping me when it's out
```
The parent comes first, replies are indented. A permalink to any reply pulls up
the whole thread. If there is no permalink, pass the channel and the thread's
timestamp instead: `slack-scrollback thread '#general' 1700000000.123456`.

### See what is readable

```sh
slack-scrollback channels
```
```
CHANNEL     KIND       MEMBERS  LAST ACTIVITY        ID
#general    public           3  2026-07-15 11:40     C0EXAMPLE1
#finance    private          4  2026-07-14 17:02     C0EXAMPLE2
@alice      dm               2  2026-07-15 08:55     D0EXAMPLE1
```
Most recently active first. Run this when a channel name is not recognised, or
to answer "what can you see?".

## Rules

- **Quote channel names**: `'#general'`, not `#general`.
- **One command per question.** `history` already includes thread replies — do
  not follow it with `thread` unless asked about one specific thread.
- **Do not invent flags.** The flags in this file are all of them.
- **Read the last line.** If the output ends in `[truncated: ...]` you did not
  get everything; either say so or re-run with the larger `--limit` it names.
- **Quote what you find.** Every line is a real message, so report names and
  times as shown rather than paraphrasing them away.

## When a command fails

Errors say what to do next. Follow them literally.

| It says | Do this |
|---|---|
| `no conversation matches '#x' — did you mean ...?` | Run the suggested name, or `slack-scrollback channels` to list them |
| `the bot can see #x but is not a member` | It cannot be read. Tell the person to invite the bot: `/invite @bot` in that channel |
| `no Slack bot token found` | Not configured. Say so — do not guess a token |
| `the Slack app is missing the 'x' scope` | Report the named scope to the person; it needs adding in Slack |

## Every flag there is

That is the whole list — there are no others to guess at.

| Flag | Applies to | Meaning |
|---|---|---|
| `--since WHEN` | `history`, `search` | start of the window (`today`, `7d`, `2026-01-31`) |
| `--until WHEN` | `history`, `search` | end of the window; a bare date includes that whole day |
| `--limit N` | `history`, `thread`, `search` | cap on messages (default 200) |
| `--in CHANNEL` | `search` | search one conversation instead of all |
| `--from NAME` | `search` | only messages by that person |
| `--no-threads` | `history` | skip thread replies |
| `--no-activity` | `channels` | skip the last-activity lookup (faster) |
| `--json` | all | one JSON object per line |
| `--links` | all | append each message's permalink |

## Good to know

- Times are local and messages come oldest first, so the last line is the newest.
- The bot only sees conversations it was invited to. "Not in `channels`" means
  invisible, not non-existent. It reads its own DMs, not other people's.
- Nothing is stored between calls — every run reads Slack fresh.
