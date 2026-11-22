"""A comment an AI app writes on a note tells people, as the editor's own comment does (MIP-1669).

In the app, the people a comment on a note reaches (its owner and everyone who can open it) are told by the
COMMENTING PERSON'S BROWSER. A comment written over MCP (`comment_on_note`, `reply_to_note_comment`) goes
straight into the note's Yjs doc and has no browser, so it reached nobody. Two pings, both pinned here:

- **the people** (A): everyone the note reaches except the commenter gets the same "commented" activity the
  app's own comment makes (same surface, same entity key and id, so a retry collapses), pushed to their open
  app and as Web Push; a failing notification never fails the comment, and nothing is filed when collab said
  no;
- **the key's owner** (B): their own AI-app notice "commented on a note", linking the thread, under its own
  switch `ai_app_note_commented` in Settings > What my AI apps do (on by default), exactly as a task comment.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.common.notification_models import NotificationPreference
from origin.models.note.common_note_models import NotePermissionMaster
from origin.services import ai_app_switches as switches
from origin.services import mcp_notice, note_comment_notify
from origin.services.v3_activity import surface_activity_id
from origin.services.webpush_gating import _COARSE_FIELD, _PUSH_DEFAULTS
from origin.tests.ai_app_defaults import all_notices_on_by_default

from .test_mcp_note_thread_writes import (
    EDITOR_ROLE,
    REPLY,
    RESOLVE,
    THREAD,
    NoteThreadWriteBase,
)

COMMENT = "comment_on_note"
CREATED = {
    "ok": True,
    "threadId": THREAD,
    "commentId": "c-1",
    "blockId": "b-intro",
    "occurrence": 1,
    "matches": 1,
}
PERSONAL_SURFACE = 6


def surface_rows(user, surface=PERSONAL_SURFACE):
    return list(
        Activity.objects.filter(recipient=user, surface_type=surface).order_by("ts_created_at")
    )


def notices(user):
    return list(Activity.objects.filter(recipient=user, activity_type=ActivityType.AI_APP))


class NotifyBase(NoteThreadWriteBase):
    def setUp(self):
        super().setUp()
        all_notices_on_by_default(self)  # MIP-2009
        self._key(scope=SCOPE_WRITE)
        # The second member can open the note (an editor), so the comment reaches them.
        self._grant(self.user2, EDITOR_ROLE)
        self.answer(CREATED)
        for target in (
            "origin.services.webpush_dispatch.schedule_push_for_activities",
            "origin.services.realtime_push.push_activities",
            "origin.services.realtime_push.push_activity",
        ):
            patcher = mock.patch(target)
            setattr(self, target.rsplit(".", 1)[1], patcher.start())
            self.addCleanup(patcher.stop)

    def start(self, **extra):
        text, is_error = self._tool_text(
            self._call(
                COMMENT,
                {
                    "note_type": "personal",
                    "note_id": self.note.note_id,
                    "anchor_text": "180 days",
                    "text": "Why 180?\nAnd for how long?",
                    **extra,
                },
            )
        )
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def reply(self, comment_id="c-2"):
        self.answer(
            {"ok": True, "commentId": comment_id, "commentCount": 2, "threadResolved": False}
        )
        text, is_error = self._tool_text(
            self._call(
                REPLY,
                {
                    "note_type": "personal",
                    "note_id": self.note.note_id,
                    "thread_id": THREAD,
                    "text": "Because the audit needs six months.",
                },
            )
        )
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])


class ThePeopleTheCommentReachesTests(NotifyBase):
    def test_a_new_thread_files_the_commented_activity_for_the_other_member(self):
        self.start()
        (row,) = surface_rows(self.user2)
        self.assertEqual(row.actor_id, self.user.id)
        self.assertEqual(row.activity_type, ActivityType.THREAD_REPLY)
        self.assertEqual(row.team_id, self.team.team_id)
        meta = row.meta
        self.assertEqual(meta["noteId"], self.note.note_id)
        self.assertEqual(meta["noteType"], 1)
        self.assertEqual(meta["noteTitle"], "Plan")
        self.assertEqual(meta["firstLineContent"], "Why 180?")
        self.assertIs(meta["isComment"], True)
        self.assertIs(meta["isCommentParticipant"], True)
        self.assertTrue(meta["tsSent"])

    def test_the_row_is_the_one_the_editors_own_comment_would_make(self):
        # Same deterministic id as genos-sockets' forward: surface 6, `comment-participant:<id>`.
        self.start()
        (row,) = surface_rows(self.user2)
        self.assertEqual(
            row.id,
            surface_activity_id(
                surface_type=PERSONAL_SURFACE,
                entity_key="comment-participant:c-1",
                recipient_id=self.user2.id,
            ),
        )

    def test_the_commenter_is_not_told_by_this(self):
        self.start()
        self.assertEqual(surface_rows(self.user), [])

    def test_a_reply_files_one_too_under_its_own_comment_id(self):
        self.start()
        self.reply("c-2")
        rows = surface_rows(self.user2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {r.meta["firstLineContent"] for r in rows},
            {"Why 180?", "Because the audit needs six months."},
        )

    def test_resolving_files_none(self):
        self.answer({"ok": True, "resolved": True, "alreadyResolved": False, "resolvedBy": "u"})
        text, is_error = self._tool_text(
            self._call(
                RESOLVE,
                {"note_type": "personal", "note_id": self.note.note_id, "thread_id": THREAD},
            )
        )
        self.assertFalse(is_error, text)
        self.assertEqual(surface_rows(self.user2), [])

    def test_the_rows_are_pushed_to_web_push_and_to_the_open_app(self):
        self.start()
        (row,) = surface_rows(self.user2)
        # The other member's row goes out by both routes. (Web Push is also called once more, for the
        # key owner's own notice; that one is pinned in TheKeysOwnerNoticeTests.)
        pushed = [
            [a.id for a in c.args[0]] for c in self.schedule_push_for_activities.call_args_list
        ]
        self.assertIn([row.id], pushed)
        self.push_activities.assert_called_once()
        self.assertEqual([a.id for a in self.push_activities.call_args.args[0]], [row.id])

    def test_a_retry_of_the_same_comment_files_nothing_new_and_pushes_nothing_new(self):
        self.start()
        self.schedule_push_for_activities.reset_mock()
        self.push_activities.reset_mock()
        again = note_comment_notify.notify_note_comment(
            self.note, note_type=1, actor_id=str(self.user.id), comment_id="c-1", text="Why 180?"
        )
        self.assertEqual(again, 0)
        self.assertEqual(len(surface_rows(self.user2)), 1)
        # Only genuinely new rows come back, so the second call pushed an empty list.
        self.assertEqual(list(self.push_activities.call_args.args[0]), [])

    def test_a_refusal_from_collab_files_nothing(self):
        self.answer({"error": "refused", "code": "anchor_not_found"}, 404)
        text, is_error = self._tool_text(
            self._call(
                COMMENT,
                {
                    "note_type": "personal",
                    "note_id": self.note.note_id,
                    "anchor_text": "x",
                    "text": "y",
                },
            )
        )
        self.assertTrue(is_error, text)
        self.assertEqual(surface_rows(self.user2), [])
        self.assertEqual(notices(self.user), [])

    def test_a_failing_notification_never_fails_the_comment(self):
        with mock.patch(
            "origin.services.v3_activity.create_surface_comment_activities",
            side_effect=RuntimeError("boom"),
        ):
            out = self.start()
        self.assertEqual(out["thread_id"], THREAD)
        self.assertEqual(surface_rows(self.user2), [])

    def test_a_member_who_cannot_open_the_note_is_not_told(self):
        # A personal note outside a team folder reaches its owner and the people granted it: a
        # teammate with no grant hears nothing about a comment they could not open.
        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self.start()
        self.assertEqual(surface_rows(self.user2), [])


class WhatEachNoteKindCarriesTests(NotifyBase):
    """The meta a chat note or a task note needs to route a click, without driving a whole chat."""

    def fake_note(self, **fields):
        base = dict(note_id=7, title="N", team_id=1, task_id=None, project_id=None)
        base.update(fields)
        return SimpleNamespace(**base)

    def test_a_task_note_carries_its_task_and_project(self):
        meta = note_comment_notify._meta(self.fake_note(task_id=9, project_id=3), 2, "hi")
        self.assertEqual((meta["noteType"], meta["taskId"], meta["projectId"]), (2, 9, 3))

    def test_a_chat_note_carries_its_chat_routing_and_thread(self):
        note = self.fake_note(
            chat_type=2, channel_id="abc-uuid", is_thread=True, thread_root_id="thread-uuid"
        )
        meta = note_comment_notify._meta(note, 3, "hi")
        self.assertEqual(
            (meta["noteType"], meta["chatType"], meta["chatId"], meta["threadId"]),
            (3, 2, "abc-uuid", "thread-uuid"),
        )

    def test_a_personal_note_carries_no_routing_beyond_itself(self):
        meta = note_comment_notify._meta(self.fake_note(), 1, "hi")
        for key in ("taskId", "projectId", "chatType", "chatId", "threadId"):
            self.assertNotIn(key, meta)

    def test_the_first_line_is_the_first_non_empty_one_and_is_clipped(self):
        self.assertEqual(note_comment_notify.first_line("\n  \n second\nthird"), "second")
        self.assertEqual(
            len(note_comment_notify.first_line("x" * 500)), note_comment_notify.FIRST_LINE_CHARS
        )
        self.assertEqual(note_comment_notify.first_line(""), "")

    def test_an_unknown_note_kind_files_nothing(self):
        self.assertEqual(
            note_comment_notify.notify_note_comment(
                self.fake_note(), note_type=9, actor_id="x", comment_id="c", text="t"
            ),
            0,
        )


class TheKeysOwnerNoticeTests(NotifyBase):
    def only(self):
        rows = notices(self.user)
        self.assertEqual(len(rows), 1, [r.meta for r in rows])
        return rows[0]

    def prefs(self, **category_settings):
        NotificationPreference.objects.update_or_create(
            user=self.user, defaults={"category_settings": category_settings}
        )

    def test_a_new_thread_tells_the_owner_their_ai_app_commented_and_links_the_thread(self):
        self.start()
        row = self.only()
        meta = row.meta
        self.assertEqual((meta["verb"], meta["kind"], meta["count"]), ("commented", "note", 1))
        self.assertEqual(meta["tool"], COMMENT)
        self.assertEqual(meta["items"][0]["title"], "Plan")
        self.assertEqual(meta["url"], f"/workspace/notes/my/{self.note.note_id}?comment={THREAD}")
        self.assertEqual(row.surface_type, 10)

    def test_a_reply_does_too_under_the_same_switch(self):
        self.reply()
        meta = self.only().meta
        self.assertEqual((meta["verb"], meta["kind"], meta["tool"]), ("commented", "note", REPLY))
        self.assertTrue(meta["url"].endswith(f"?comment={THREAD}"))

    def test_resolving_does_not(self):
        self.answer({"ok": True, "resolved": True, "alreadyResolved": False, "resolvedBy": "u"})
        self._tool_text(
            self._call(
                RESOLVE,
                {"note_type": "personal", "note_id": self.note.note_id, "thread_id": THREAD},
            )
        )
        self.assertEqual(notices(self.user), [])

    def test_it_is_on_by_default(self):
        row = NotificationPreference.objects.filter(user=self.user).first()
        self.assertTrue(row is None or "ai_app_note_commented" not in (row.category_settings or {}))
        self.start()
        self.only()

    def test_its_own_switch_turns_it_off_and_only_it(self):
        self.prefs(ai_app_note_commented=False)
        self.start()
        self.assertEqual(notices(self.user), [])
        # ...and the other people are still told: the switch is about the owner's own feed.
        self.assertEqual(len(surface_rows(self.user2)), 1)

    def test_the_task_comment_switch_does_not_govern_it(self):
        self.prefs(ai_app_task_commented=False)
        self.start()
        self.only()

    def test_the_group_master_turns_it_off(self):
        NotificationPreference.objects.update_or_create(
            user=self.user, defaults={"enable_ai_apps": False}
        )
        self.start()
        self.assertEqual(notices(self.user), [])

    def test_it_is_not_made_when_the_comment_was_not_written(self):
        self.answer({"error": "refused", "code": "anchor_not_found"}, 404)
        self._tool_text(
            self._call(
                COMMENT,
                {
                    "note_type": "personal",
                    "note_id": self.note.note_id,
                    "anchor_text": "x",
                    "text": "y",
                },
            )
        )
        self.assertEqual(notices(self.user), [])

    def test_the_other_member_gets_no_ai_app_notice(self):
        self.start()
        self.assertEqual(notices(self.user2), [])


class SwitchAndWordingTests(NotifyBase):
    def test_the_switch_is_in_the_table_on_by_default_and_gates_the_push_like_the_others(self):
        self.assertEqual(switches.key_for("commented", "note"), "ai_app_note_commented")
        self.assertIn("ai_app_note_commented", switches.KEYS)
        self.assertIs(switches.DEFAULTS["ai_app_note_commented"], True)
        self.assertIs(_PUSH_DEFAULTS["ai_app_note_commented"], True)
        self.assertEqual(_COARSE_FIELD["ai_app_note_commented"], "enable_ai_apps")

    def test_the_task_comment_switch_is_unchanged(self):
        self.assertEqual(switches.key_for("commented", "comment"), "ai_app_task_commented")

    def test_the_push_sentence_names_the_note(self):
        meta = {"verb": "commented", "kind": "note", "count": 1, "items": [{"title": "Plan"}]}
        self.assertEqual(mcp_notice.sentence(meta), "commented on note Plan")
        self.assertEqual(
            mcp_notice.sentence({"verb": "commented", "kind": "note", "count": 1, "items": [{}]}),
            "commented on a note",
        )

    def test_the_task_comment_sentence_is_unchanged(self):
        meta = {
            "verb": "commented",
            "kind": "comment",
            "count": 1,
            "items": [{"displayId": "QR-1"}],
        }
        self.assertEqual(mcp_notice.sentence(meta), "commented on QR-1")

    def test_both_tools_have_a_rule_and_neither_is_silent_any_more(self):
        self.assertIn(COMMENT, mcp_notice.RULES)
        self.assertIn(REPLY, mcp_notice.RULES)
        self.assertNotIn(RESOLVE, mcp_notice.RULES)

    def test_a_thread_less_payload_makes_no_notice(self):
        self.assertIsNone(mcp_notice.build_notice(COMMENT, {}, {"note_id": 3}))
