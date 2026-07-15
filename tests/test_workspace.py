"""Conversation resolution, name caching, and message assembly."""

from __future__ import annotations

from typing import Any

import pytest

from slack_scrollback.errors import UsageError
from slack_scrollback.workspace import Workspace, _ts_sort_key, is_readable
from tests.conftest import channel, err, make_client, message, ok

USER_BODIES = {
    "U0EXAMPLE1": ok(
        user={"id": "U0EXAMPLE1", "name": "alice.j", "real_name": "Alice Jones", "profile": {"display_name": "alice"}}
    ),
    "U0EXAMPLE2": ok(
        user={"id": "U0EXAMPLE2", "name": "bob.s", "real_name": "Bob Smith", "profile": {"display_name": ""}}
    ),
}


def users_handler(params: dict[str, str]) -> dict[str, Any]:
    return USER_BODIES.get(params.get("user", ""), err("user_not_found"))


def build(handlers: dict[str, Any]) -> tuple[Workspace, Any]:
    client, transport = make_client(handlers)
    return Workspace(client), transport


# -- readability -----------------------------------------------------------


def test_a_channel_is_readable_only_when_the_bot_is_a_member() -> None:
    assert is_readable(channel(is_member=True))
    assert not is_readable(channel(is_member=False))


def test_dms_are_readable_even_though_slack_omits_is_member() -> None:
    """Slack sets no is_member on DMs; testing it alone would discard every DM."""
    assert is_readable({"id": "D0EXAMPLE1", "is_im": True, "user": "U0EXAMPLE1"})
    assert is_readable({"id": "G0EXAMPLE1", "is_mpim": True})


# -- names -----------------------------------------------------------------


def test_display_name_wins_over_real_name() -> None:
    workspace, _ = build({"users.info": users_handler})
    assert workspace.user_name("U0EXAMPLE1") == "alice"


def test_an_empty_display_name_falls_through_to_real_name() -> None:
    """Slack returns "" rather than omitting the key, so presence is not enough."""
    workspace, _ = build({"users.info": users_handler})
    assert workspace.user_name("U0EXAMPLE2") == "Bob Smith"


def test_an_unresolvable_user_keeps_their_id() -> None:
    workspace, _ = build({"users.info": users_handler})
    assert workspace.user_name("U0GHOST") == "U0GHOST"


def test_names_are_cached_for_the_life_of_the_run() -> None:
    workspace, transport = build({"users.info": users_handler})
    for _ in range(5):
        workspace.user_name("U0EXAMPLE1")
    assert transport.methods.count("users.info") == 1


def test_a_failed_lookup_is_cached_too() -> None:
    workspace, transport = build({"users.info": users_handler})
    workspace.user_name("U0GHOST")
    workspace.user_name("U0GHOST")
    assert transport.methods.count("users.info") == 1


def test_no_user_id_renders_as_unknown() -> None:
    workspace, transport = build({})
    assert workspace.user_name(None) == "unknown"
    assert transport.calls == []


# -- conversations ---------------------------------------------------------

CONVERSATIONS = ok(
    channels=[
        channel("C0EXAMPLE1", "general"),
        channel("C0EXAMPLE2", "random", is_member=False),
        channel("C0EXAMPLE3", "secrets", is_private=True, is_member=True),
        channel("C0EXAMPLE4", "old", is_archived=True),
        {"id": "D0EXAMPLE1", "is_im": True, "user": "U0EXAMPLE1"},
        {"id": "G0EXAMPLE1", "is_mpim": True, "name": "mpdm-alice--bob--carol-1", "num_members": 3},
    ]
)

BASE = {"conversations.list": CONVERSATIONS, "users.info": users_handler}


def test_readable_conversations_exclude_non_member_and_archived() -> None:
    workspace, _ = build(BASE)
    names = [c.name for c in workspace.readable_conversations()]
    assert "#general" in names
    assert "#secrets" in names
    assert "#random" not in names
    assert "#old" not in names


def test_dms_are_named_after_the_other_person() -> None:
    workspace, _ = build(BASE)
    names = [c.name for c in workspace.readable_conversations()]
    assert "@alice" in names


def test_group_dms_are_named_after_their_members() -> None:
    workspace, _ = build(BASE)
    names = [c.name for c in workspace.readable_conversations()]
    assert "group DM: alice, bob, carol" in names


def test_conversations_are_fetched_once() -> None:
    workspace, transport = build(BASE)
    workspace.readable_conversations()
    workspace.readable_conversations()
    assert transport.methods.count("conversations.list") == 1


@pytest.mark.parametrize("spec", ["#general", "general", "C0EXAMPLE1"])
def test_a_conversation_resolves_by_name_bare_name_or_id(spec: str) -> None:
    workspace, _ = build(BASE)
    assert workspace.resolve(spec).id == "C0EXAMPLE1"


def test_a_dm_resolves_by_the_persons_name() -> None:
    workspace, _ = build(BASE)
    assert workspace.resolve("@alice").id == "D0EXAMPLE1"


def test_a_typo_suggests_the_closest_channel() -> None:
    workspace, _ = build(BASE)
    with pytest.raises(UsageError) as caught:
        workspace.resolve("#genral")
    message_text = str(caught.value)
    assert "general" in message_text
    assert "slack-scrollback channels" in message_text


def test_an_unrecognisable_name_lists_what_is_available() -> None:
    workspace, _ = build(BASE)
    with pytest.raises(UsageError) as caught:
        workspace.resolve("#zzzzzzzz")
    assert "#general" in str(caught.value)


def test_a_channel_the_bot_is_not_in_says_to_invite_it() -> None:
    """The distinction between "no such channel" and "not invited" is the whole fix."""
    workspace, _ = build(BASE)
    with pytest.raises(UsageError) as caught:
        workspace.resolve("#random")
    assert "/invite" in str(caught.value)


def test_an_empty_channel_argument_is_rejected() -> None:
    workspace, _ = build(BASE)
    with pytest.raises(UsageError):
        workspace.resolve("   ")


# -- history ---------------------------------------------------------------


def test_history_is_returned_oldest_first() -> None:
    handlers = dict(
        BASE,
        **{
            "conversations.history": ok(
                messages=[
                    message("300.000100", "third"),
                    message("200.000100", "second"),
                    message("100.000100", "first"),
                ]
            )
        },
    )
    workspace, _ = build(handlers)
    result = workspace.fetch_history(workspace.resolve("#general"), limit=10)
    assert [e.message["text"] for e in result.entries] == ["first", "second", "third"]


def test_history_always_bounds_the_window_at_both_ends() -> None:
    """Slack drops the newest messages when oldest is sent without latest.

    Verified against the live API: with only `oldest`, paging anchors at the old
    end of the window and the most recent messages never appear at all.
    """
    import time as _time

    before = _time.time()
    workspace, transport = build(dict(BASE, **{"conversations.history": ok(messages=[])}))
    workspace.fetch_history(workspace.resolve("#general"), oldest="100.000000", limit=10)
    after = _time.time()
    history_call = next(c for c in transport.calls if c.method == "conversations.history")
    assert history_call.params["oldest"] == "100.000000"
    # Not merely present: it must default to *now*, or the window still loses its
    # newest messages.
    assert before <= float(history_call.params["latest"]) <= after


def test_an_explicit_until_is_passed_through_untouched() -> None:
    workspace, transport = build(dict(BASE, **{"conversations.history": ok(messages=[])}))
    workspace.fetch_history(workspace.resolve("#general"), oldest="100.000000", latest="200.000000", limit=10)
    history_call = next(c for c in transport.calls if c.method == "conversations.history")
    assert history_call.params["latest"] == "200.000000"


def test_threads_are_expanded_and_nested() -> None:
    parent = message("100.000100", "question", thread_ts="100.000100", reply_count=1)
    handlers = dict(
        BASE,
        **{
            "conversations.history": ok(messages=[parent]),
            "conversations.replies": ok(messages=[parent, message("150.000100", "answer", thread_ts="100.000100")]),
        },
    )
    workspace, _ = build(handlers)
    result = workspace.fetch_history(workspace.resolve("#general"), limit=10)
    assert [(e.message["text"], e.depth) for e in result.entries] == [("question", 0), ("answer", 1)]


def test_a_thread_broadcast_is_shown_once_not_twice() -> None:
    """Broadcasts appear in history AND in the parent's replies; dedupe on ts."""
    parent = message("100.000100", "question", thread_ts="100.000100", reply_count=1)
    broadcast = message("150.000100", "answer", thread_ts="100.000100", subtype="thread_broadcast")
    handlers = dict(
        BASE,
        **{
            "conversations.history": ok(messages=[broadcast, parent]),
            "conversations.replies": ok(messages=[parent, broadcast]),
        },
    )
    workspace, _ = build(handlers)
    result = workspace.fetch_history(workspace.resolve("#general"), limit=10)
    assert [e.message["ts"] for e in result.entries].count("150.000100") == 1


def test_replies_dropped_by_the_cap_are_announced() -> None:
    """The cap counts replies, so a thread cut short must still set `truncated`.

    Nothing else notices: the page has one message and no has_more, so without an
    explicit signal from the reply fetch the trailer would claim a complete read.
    """
    parent = message("100.000100", "question", thread_ts="100.000100", reply_count=10)
    replies = [parent] + [message(f"2{i:02d}.000100", f"reply {i}", thread_ts="100.000100") for i in range(10)]
    workspace, _ = build(
        dict(
            BASE,
            **{
                "conversations.history": ok(messages=[parent], has_more=False),
                "conversations.replies": ok(messages=replies),
            },
        )
    )
    result = workspace.fetch_history(workspace.resolve("#general"), limit=5)
    assert len(result.entries) == 5
    assert result.truncated


def test_a_thread_that_fits_is_not_reported_as_truncated() -> None:
    parent = message("100.000100", "question", thread_ts="100.000100", reply_count=2)
    replies = [
        parent,
        message("110.000100", "a", thread_ts="100.000100"),
        message("120.000100", "b", thread_ts="100.000100"),
    ]
    workspace, _ = build(
        dict(
            BASE,
            **{
                "conversations.history": ok(messages=[parent], has_more=False),
                "conversations.replies": ok(messages=replies),
            },
        )
    )
    result = workspace.fetch_history(workspace.resolve("#general"), limit=5)
    assert len(result.entries) == 3
    assert not result.truncated


def test_a_thread_reached_exactly_at_the_cap_is_announced() -> None:
    """budget == 0 still means replies exist that are not being shown."""
    parent = message("100.000100", "question", thread_ts="100.000100", reply_count=3)
    workspace, _ = build(
        dict(
            BASE,
            **{
                "conversations.history": ok(messages=[parent], has_more=False),
                "conversations.replies": ok(messages=[parent, message("110.000100", "a", thread_ts="100.000100")]),
            },
        )
    )
    result = workspace.fetch_history(workspace.resolve("#general"), limit=1)
    assert len(result.entries) == 1
    assert result.truncated


def test_no_threads_skips_the_replies_call() -> None:
    parent = message("100.000100", "question", thread_ts="100.000100", reply_count=1)
    workspace, transport = build(dict(BASE, **{"conversations.history": ok(messages=[parent])}))
    workspace.fetch_history(workspace.resolve("#general"), limit=10, expand_threads=False)
    assert "conversations.replies" not in transport.methods


def test_a_parent_with_no_replies_costs_no_extra_call() -> None:
    lone = message("100.000100", "hi", thread_ts="100.000100", reply_count=0)
    workspace, transport = build(dict(BASE, **{"conversations.history": ok(messages=[lone])}))
    workspace.fetch_history(workspace.resolve("#general"), limit=10)
    assert "conversations.replies" not in transport.methods


def test_the_limit_caps_output_and_marks_truncation() -> None:
    workspace, _ = build(
        dict(
            BASE,
            **{
                "conversations.history": ok(messages=[message(f"{i}00.000100") for i in range(9, 0, -1)], has_more=True)
            },
        )
    )
    result = workspace.fetch_history(workspace.resolve("#general"), limit=3)
    assert len(result.entries) == 3
    assert result.truncated


def test_the_newest_messages_are_the_ones_kept() -> None:
    workspace, _ = build(
        dict(
            BASE,
            **{
                "conversations.history": ok(
                    messages=[message("900.000100", "new"), message("500.000100", "mid"), message("100.000100", "old")]
                )
            },
        )
    )
    result = workspace.fetch_history(workspace.resolve("#general"), limit=2)
    assert [e.message["text"] for e in result.entries] == ["mid", "new"]


def test_a_silently_capped_page_is_reported_as_throttling() -> None:
    """Asking for 200 and getting exactly 15 with more pending is the Slack cap."""
    workspace, _ = build(
        dict(
            BASE,
            **{"conversations.history": ok(messages=[message(f"{i:03d}.000100") for i in range(15)], has_more=True)},
        )
    )
    result = workspace.fetch_history(workspace.resolve("#general"), limit=200)
    assert result.throttled


def test_a_short_final_page_is_not_mistaken_for_throttling() -> None:
    workspace, _ = build(
        dict(
            BASE,
            **{"conversations.history": ok(messages=[message(f"{i:03d}.000100") for i in range(15)], has_more=False)},
        )
    )
    assert not workspace.fetch_history(workspace.resolve("#general"), limit=200).throttled


# -- threads ---------------------------------------------------------------


def test_a_thread_puts_the_parent_first_and_indents_the_rest() -> None:
    parent = message("100.000100", "q", thread_ts="100.000100", reply_count=2)
    handlers = dict(
        BASE,
        **{
            "conversations.replies": ok(
                messages=[
                    parent,
                    message("150.000100", "a1", thread_ts="100.000100"),
                    message("160.000100", "a2", thread_ts="100.000100"),
                ]
            )
        },
    )
    workspace, _ = build(handlers)
    result = workspace.fetch_thread(workspace.resolve("#general"), "100.000100")
    assert [(e.message["text"], e.depth) for e in result.entries] == [("q", 0), ("a1", 1), ("a2", 1)]


def test_an_empty_thread_explains_itself() -> None:
    workspace, _ = build(dict(BASE, **{"conversations.replies": ok(messages=[])}))
    with pytest.raises(UsageError) as caught:
        workspace.fetch_thread(workspace.resolve("#general"), "100.000100")
    assert "permalink" in str(caught.value)


# -- search ----------------------------------------------------------------

SEARCHABLE = {
    "C0EXAMPLE1": [
        message("300.000100", "the budget is fine"),
        message("200.000100", "unrelated"),
        message("100.000100", "BUDGET talk", user="U0EXAMPLE2"),
    ],
    "C0EXAMPLE3": [message("400.000100", "secret budget")],
}


def search_handler(params: dict[str, str]) -> dict[str, Any]:
    return ok(messages=SEARCHABLE.get(params.get("channel", ""), []))


def test_search_matches_case_insensitively_across_conversations() -> None:
    workspace, _ = build(dict(BASE, **{"conversations.history": search_handler}))
    result = workspace.search("budget", conversations=workspace.readable_conversations())
    assert [e.message["text"] for e in result.entries] == ["BUDGET talk", "the budget is fine", "secret budget"]


def test_search_can_filter_by_speaker_name_or_id() -> None:
    workspace, _ = build(dict(BASE, **{"conversations.history": search_handler}))
    conversations = workspace.readable_conversations()
    by_name = workspace.search("budget", conversations=conversations, from_user="@Bob Smith")
    assert [e.message["text"] for e in by_name.entries] == ["BUDGET talk"]
    by_id = workspace.search("budget", conversations=conversations, from_user="U0EXAMPLE2")
    assert [e.message["text"] for e in by_id.entries] == ["BUDGET talk"]


@pytest.mark.parametrize("spelling", ["@Bob", "bob", "BOB", "Smith", "@bob smith"])
def test_from_matches_the_part_of_the_name_someone_would_type(spelling: str) -> None:
    """ "Bob" must find "Bob Smith": nobody types the full display name."""
    workspace, _ = build(dict(BASE, **{"conversations.history": search_handler}))
    found = workspace.search("budget", conversations=workspace.readable_conversations(), from_user=spelling)
    assert [e.message["text"] for e in found.entries] == ["BUDGET talk"]


def test_from_nobody_says_who_did_speak_instead_of_returning_silence() -> None:
    """An empty result cannot distinguish "said nothing" from "misspelt"."""
    workspace, _ = build(dict(BASE, **{"conversations.history": search_handler}))
    found = workspace.search("budget", conversations=workspace.readable_conversations(), from_user="@nobody")
    assert found.entries == []
    note = " ".join(found.notes)
    assert "nobody matching 'nobody'" in note
    assert "Alice" in note or "alice" in note


def test_from_someone_who_spoke_but_not_about_it_says_so() -> None:
    workspace, _ = build(dict(BASE, **{"conversations.history": search_handler}))
    found = workspace.search("zzzz", conversations=workspace.readable_conversations(), from_user="@alice")
    assert found.entries == []
    assert "said nothing matching the query" in " ".join(found.notes)


def test_search_scans_every_conversation_before_capping() -> None:
    """Stopping at `limit` would return the first channels scanned, not the newest hits."""
    workspace, _ = build(dict(BASE, **{"conversations.history": search_handler}))
    result = workspace.search("budget", conversations=workspace.readable_conversations(), limit=1)
    assert [e.message["text"] for e in result.entries] == ["secret budget"]
    assert result.truncated


def test_search_ignores_join_noise() -> None:
    handlers = dict(
        BASE,
        **{
            "conversations.history": lambda p: ok(
                messages=[message("100.000100", "alice has joined the channel", subtype="channel_join")]
            )
        },
    )
    workspace, _ = build(handlers)
    assert workspace.search("joined", conversations=workspace.readable_conversations()).entries == []


def test_search_survives_a_conversation_that_refuses_history() -> None:
    """The Slackbot DM is listed but returns channel_not_found."""

    def handler(params: dict[str, str]) -> dict[str, Any]:
        if params.get("channel") == "C0EXAMPLE1":
            return err("channel_not_found")
        return ok(messages=SEARCHABLE.get(params.get("channel", ""), []))

    workspace, _ = build(dict(BASE, **{"conversations.history": handler}))
    result = workspace.search("budget", conversations=workspace.readable_conversations())
    assert [e.message["text"] for e in result.entries] == ["secret budget"]


def test_an_empty_query_is_rejected() -> None:
    workspace, _ = build(BASE)
    with pytest.raises(UsageError):
        workspace.search("  ", conversations=[])


# -- permalinks ------------------------------------------------------------


def test_a_permalink_is_composed_from_the_workspace_url() -> None:
    workspace, _ = build(dict(BASE, **{"auth.test": ok(url="https://acme.slack.com/")}))
    conversation = workspace.resolve("#general")
    link = workspace.permalink(conversation, message("1700000000.000100"))
    assert link == "https://acme.slack.com/archives/C0EXAMPLE1/p1700000000000100"


def test_anything_in_a_thread_links_into_the_thread_pane() -> None:
    """Slack decorates a thread parent's own permalink too, not just its replies."""
    workspace, _ = build(dict(BASE, **{"auth.test": ok(url="https://acme.slack.com/")}))
    conversation = workspace.resolve("#general")
    parent = message("1700000000.000100", thread_ts="1700000000.000100")
    assert workspace.permalink(conversation, parent).endswith("?thread_ts=1700000000.000100&cid=C0EXAMPLE1")


def test_the_workspace_url_is_fetched_once() -> None:
    workspace, transport = build(dict(BASE, **{"auth.test": ok(url="https://acme.slack.com/")}))
    conversation = workspace.resolve("#general")
    for _ in range(3):
        workspace.permalink(conversation, message("1700000000.000100"))
    assert transport.methods.count("auth.test") == 1


# -- last activity ---------------------------------------------------------


def test_last_activity_reads_the_newest_message() -> None:
    workspace, _ = build(dict(BASE, **{"conversations.history": ok(messages=[message("1700000000.000100")])}))
    assert workspace.last_activity(workspace.resolve("#general")) == "1700000000.000100"


def test_last_activity_tolerates_a_conversation_that_refuses() -> None:
    workspace, _ = build(dict(BASE, **{"conversations.history": err("channel_not_found")}))
    assert workspace.last_activity(workspace.resolve("#general")) is None


def test_last_activity_of_an_empty_conversation_is_nothing() -> None:
    workspace, _ = build(dict(BASE, **{"conversations.history": ok(messages=[])}))
    assert workspace.last_activity(workspace.resolve("#general")) is None


# -- timestamp ordering ----------------------------------------------------


def test_timestamps_sort_without_float_precision_loss() -> None:
    assert _ts_sort_key("1358878755.000001") < _ts_sort_key("1358878755.000002")
    assert _ts_sort_key("100.000100") < _ts_sort_key("200.000001")
    assert _ts_sort_key(None) == (0, 0)
    assert _ts_sort_key("garbage") == (0, 0)
