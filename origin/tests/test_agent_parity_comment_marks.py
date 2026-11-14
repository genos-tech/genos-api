"""`update_comment` takes `reaction` and `flag` (MIP-1562, flag `AGENT_PARITY_TOOLS`).

The comment bubble can react to a comment, flag it for later, mark the flag done and remove it. The
agent and MCP could do none of it. The two actions go through `update_comment` as parameters
instead of new tools (a write tool cannot be flag-gated, and each parameter is one fewer
declaration), and through the same rules the app's own endpoints apply:

  * a reaction: `POST|DELETE /api/v2/task/comment/reaction/` (what the sockets handler
    `task_comment_reaction` calls), `services/task_comment_reactions`;
  * a flag: `POST|PATCH|DELETE /api/v3/messages/<mirror id>/flag/`, `services/message_flags`.

The core tests drive the same action through the app's endpoint and through the tool on twin
comments, record every way the app tells a person something (`notice_watch`), and compare what
each leaves. The rest pin each guard: a refused call changes nothing.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    Flag,
    Message,
)
from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskCommentReactionFact, TaskComments, TaskMaster
from origin.search_engine.agent.tools import REGISTRY, ToolContext, ToolError
from origin.search_engine.agent.tools.add_comment import ADD_COMMENT
from origin.search_engine.agent.tools.base import ToolForbidden
from origin.search_engine.agent.tools.update_comment import UPDATE_COMMENT
from origin.services import message_reminders, presence, realtime_push
from origin.services.task_comment_reactions import MAX_EMOJI_CHARS
from origin.services.unified_writer import task_comment_message_uuid
from origin.services.v3_activity import task_surface

from .notice_watch import watching_notices
from .test_base import BaseAPITestCase

User = get_user_model()
PARITY = {"AGENT_PARITY_TOOLS": True}
REACTION_URL = "/api/v2/task/comment/reaction/"


class MarksBase(BaseAPITestCase):
    """`self.user` reacts and flags; `self.user2` writes the comments they react to."""

    def setUp(self):
        super().setUp()
        cache.clear()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, PARITY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user2,
            title="Implementation",
            status="WIP",
        )
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        self.header = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))

        # Someone who can open the task (its assignee) without being on the project, so not in the
        # project chat; and someone from another team.
        self.guest = User.objects.create_user(
            username="guest", email="guest@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.guest)
        self.guest_task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.guest,
            title="Guest's",
            status="WIP",
        )
        self.guest_ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.guest.id))
        self.stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="x"
        )
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=other, attendee=self.stranger)
        self.stranger_ctx = ToolContext(team_id=str(other.team_id), user_id=str(self.stranger.id))

    # ---- fixtures -----------------------------------------------------------------------

    def comment(self, task=None, ctx=None, text="Can you check the footer spacing?"):
        """A real comment, with its mirror, through the tool the agent writes comments with."""
        task = task or self.task
        return ADD_COMMENT.run({"task_id": task.task_id, "body_text": text}, ctx or self.ctx2)[
            "comment_id"
        ]

    def mirror(self, comment_id, task=None) -> Message:
        task = task or self.task
        return Message.objects.get(id=task_comment_message_uuid(task.task_id, comment_id))

    # ---- the tool ----------------------------------------------------------------------

    def mark(self, comment_id, ctx=None, task=None, **extra):
        task = task or self.task
        return UPDATE_COMMENT.run(
            {"task_id": task.task_id, "comment_id": comment_id, **extra}, ctx or self.ctx
        )

    def refused(self, comment_id, message=None, ctx=None, task=None, exc=ToolError, **extra):
        with self.assertRaises(exc) as caught:
            self.mark(comment_id, ctx=ctx, task=task, **extra)
        if message:
            self.assertIn(message, str(caught.exception))
        return str(caught.exception)

    # ---- what a comment looks like afterwards ---------------------------------------------

    def reactions(self, comment_id, task=None):
        task = task or self.task
        return list(
            TaskCommentReactionFact.objects.filter(task=task, comment_id=comment_id)
            .order_by("reaction_id")
            .values_list("reaction_id", "reaction_emoji", "sender_id", "team_id")
        )

    def reaction_activities(self, comment_id):
        return [
            (str(a.recipient_id), str(a.actor_id), a.meta)
            for a in Activity.objects.filter(
                message=self.mirror(comment_id), activity_type=ActivityType.REACTION
            ).order_by("ts_created_at")
        ]

    def flags(self, comment_id, user=None):
        return list(
            Flag.objects.filter(
                user=user or self.user, message=self.mirror(comment_id)
            ).values_list("completed_at", flat=True)
        )


class _Pushes:
    """The live refresh a write asks sockets for (`push_task_comment`), recorded."""

    def __enter__(self):
        self.patch = mock.patch.object(realtime_push, "push_task_comment")
        self.push = self.patch.start()
        return self.push

    def __exit__(self, *exc):
        self.patch.stop()


# ---- reactions: the tool against the app's endpoint ---------------------------------------


class ReactionTwinTests(MarksBase):
    def ui_react(self, comment_id, emoji="👍", user=None, method="post"):
        self.authenticate(user or self.user)
        if method == "post":
            res = self.client.post(
                REACTION_URL,
                {
                    "team_id": str(self.team.team_id),
                    "task_id": self.task.task_id,
                    "comment_id": comment_id,
                    "reaction_emoji": emoji,
                    "sender_id": str((user or self.user).id),
                },
                format="json",
            )
            self.assertEqual(res.status_code, 201, getattr(res, "data", None))
        else:
            query = urlencode(
                {
                    "team_id": str(self.team.team_id),
                    "sender_id": str((user or self.user).id),
                    "task_id": self.task.task_id,
                    "comment_id": comment_id,
                    "reaction_emoji": emoji,
                }
            )
            res = self.client.delete(f"{REACTION_URL}?{query}")
            self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        return res

    def test_a_reaction_leaves_what_the_apps_endpoint_leaves(self):
        by_tool, by_ui = self.comment(), self.comment()

        with watching_notices() as tool_notices, _Pushes():
            out = self.mark(by_tool, reaction="👍")
        with watching_notices() as ui_notices:
            self.ui_react(by_ui, "👍")

        self.assertTrue(out["changed"])
        self.assertEqual(self.reactions(by_tool), self.reactions(by_ui))
        self.assertEqual(
            self.reactions(by_tool),
            [(1, "👍", self.user.id, self.team.team_id)],
        )
        self.assertEqual(self.reaction_activities(by_tool), self.reaction_activities(by_ui))
        self.assertEqual(
            self.reaction_activities(by_tool),
            [(str(self.user2.id), str(self.user.id), {"emoji": "👍"})],
        )
        # Every channel the app tells a person through, recorded: the same, and exactly one feed row
        # and one Web Push, for the comment's author.
        self.assertEqual(tool_notices, ui_notices)
        self.assertEqual(
            tool_notices,
            {"rows": {"Activity": 1}, "emails": 0, "pushes": {"schedule_push_for_activities": 1}},
        )

    def test_a_second_reaction_takes_the_next_counter_as_the_app_does(self):
        by_tool, by_ui = self.comment(), self.comment()
        with _Pushes():
            self.mark(by_tool, reaction="👍")
        self.ui_react(by_ui, "👍")
        # Someone else's reaction in between, then this person's second emoji.
        self.ui_react(by_tool, "🎉", user=self.user2)
        self.ui_react(by_ui, "🎉", user=self.user2)
        with _Pushes():
            self.mark(by_tool, reaction="❤️")
        self.ui_react(by_ui, "❤️")
        self.assertEqual(
            [(i, e, s) for i, e, s, _ in self.reactions(by_tool)],
            [(i, e, s) for i, e, s, _ in self.reactions(by_ui)],
        )
        self.assertEqual([i for i, *_ in self.reactions(by_tool)], [1, 2, 3])

    def test_taking_a_reaction_back_leaves_what_the_apps_delete_leaves(self):
        by_tool, by_ui = self.comment(), self.comment()
        self.mark(by_tool, reaction="👍")
        self.ui_react(by_ui, "👍")
        # A colleague's reaction on each stays.
        self.ui_react(by_tool, "👍", user=self.user2)
        self.ui_react(by_ui, "👍", user=self.user2)

        with watching_notices() as tool_notices, _Pushes():
            out = self.mark(by_tool, reaction="👍", remove_reaction=True)
        with watching_notices() as ui_notices:
            self.ui_react(by_ui, "👍", method="delete")

        self.assertTrue(out["changed"])
        self.assertEqual(
            [(e, s) for _, e, s, _ in self.reactions(by_tool)],
            [(e, s) for _, e, s, _ in self.reactions(by_ui)],
        )
        self.assertEqual(self.reactions(by_tool)[0][2], self.user2.id)
        # An unreact is not news: nobody is told, either way.
        self.assertEqual(tool_notices, ui_notices)
        self.assertEqual(tool_notices, {"rows": {}, "emails": 0, "pushes": {}})

    def test_reacting_to_your_own_comment_tells_nobody_as_in_the_app(self):
        own_by_tool, own_by_ui = self.comment(ctx=self.ctx), self.comment(ctx=self.ctx)
        with watching_notices() as tool_notices, _Pushes():
            self.mark(own_by_tool, reaction="👍")
        with watching_notices() as ui_notices:
            self.ui_react(own_by_ui, "👍")
        self.assertEqual(len(self.reactions(own_by_tool)), 1)
        self.assertEqual(tool_notices, ui_notices)
        self.assertEqual(tool_notices["rows"], {})
        self.assertEqual(tool_notices["pushes"].get("schedule_push_for_activities", 0), 1)

    def test_an_author_reading_the_task_is_not_told_as_in_the_app(self):
        by_tool, by_ui = self.comment(), self.comment()
        presence.mark_viewing(self.user2.id, task_surface(self.task.task_id), "device-a")
        with watching_notices() as tool_notices, _Pushes():
            self.mark(by_tool, reaction="👍")
        with watching_notices() as ui_notices:
            self.ui_react(by_ui, "👍")
        self.assertEqual(tool_notices, ui_notices)
        self.assertEqual(tool_notices["rows"], {})
        self.assertEqual(self.reaction_activities(by_tool), [])

    def test_the_tool_tells_nobody_beyond_the_authors_feed(self):
        """Every channel: no chat message, mention, email, webhook or inbox item, and no history
        row on the task (a reaction is not an edit)."""
        comment_id = self.comment()
        row_before = TaskComments.objects.get(task=self.task, comment_id=comment_id)
        history = TaskActivity.objects.count()
        mirror_before = Message.objects.get(pk=self.mirror(comment_id).pk).ts_updated_at
        with watching_notices() as seen, _Pushes():
            self.mark(comment_id, reaction="🎉")
        self.assertEqual(set(seen["rows"]), {"Activity"})
        self.assertEqual(seen["emails"], 0)
        self.assertEqual(TaskActivity.objects.count(), history)
        row_after = TaskComments.objects.get(task=self.task, comment_id=comment_id)
        self.assertEqual(row_after.ts_updated_at, row_before.ts_updated_at)
        self.assertEqual(row_after.comment_body, row_before.comment_body)
        self.assertEqual(
            Message.objects.get(pk=self.mirror(comment_id).pk).ts_updated_at, mirror_before
        )


class ReactionBehaviourTests(MarksBase):
    def test_reacting_twice_changes_nothing_and_tells_nobody_again(self):
        comment_id = self.comment()
        with _Pushes():
            first = self.mark(comment_id, reaction="👍")
        with watching_notices() as seen, _Pushes() as push:
            again = self.mark(comment_id, reaction="👍")
        self.assertTrue(first["changed"])
        self.assertFalse(again["changed"])
        self.assertIn("Already reacted", again["__summary__"])
        self.assertEqual(len(self.reactions(comment_id)), 1)
        self.assertEqual(seen, {"rows": {}, "emails": 0, "pushes": {}})
        push.assert_not_called()

    def test_taking_back_a_reaction_that_is_not_there_is_not_an_error(self):
        comment_id = self.comment()
        with watching_notices() as seen, _Pushes() as push:
            out = self.mark(comment_id, reaction="👍", remove_reaction=True)
        self.assertFalse(out["changed"])
        self.assertIn("nothing to remove", out["__summary__"])
        self.assertEqual(seen, {"rows": {}, "emails": 0, "pushes": {}})
        push.assert_not_called()

    def test_only_the_callers_own_reaction_is_taken_back(self):
        comment_id = self.comment()
        self.mark(comment_id, reaction="👍", ctx=self.ctx2)
        out = self.mark(comment_id, reaction="👍", remove_reaction=True)
        self.assertFalse(out["changed"])
        self.assertEqual([e for _, e, *_ in self.reactions(comment_id)], ["👍"])

    def test_the_project_room_is_asked_to_refresh_once_per_change(self):
        comment_id = self.comment()
        with _Pushes() as push:
            self.mark(comment_id, reaction="👍")
        push.assert_called_once_with(self.task.task_id, comment_id, realtime_push.EDITED)
        with _Pushes() as push:
            self.mark(comment_id, reaction="👍", remove_reaction=True)
        push.assert_called_once_with(self.task.task_id, comment_id, realtime_push.EDITED)

    def test_the_comment_itself_is_untouched(self):
        comment_id = self.comment()
        before = TaskComments.objects.filter(task=self.task, comment_id=comment_id).values().get()
        with _Pushes():
            self.mark(comment_id, reaction="👍")
        after = TaskComments.objects.filter(task=self.task, comment_id=comment_id).values().get()
        self.assertEqual(before, after)

    def test_the_result_names_the_comment_and_the_emoji(self):
        comment_id = self.comment()
        with _Pushes():
            out = self.mark(comment_id, reaction="🎉")
        self.assertEqual(out["comment_id"], comment_id)
        self.assertEqual(out["display_id"], self.task.display_id)
        self.assertEqual(out["reaction"], "🎉")
        self.assertIn("Reacted 🎉", out["__summary__"])

    def test_a_team_emoji_is_taken_by_its_shortcode(self):
        comment_id = self.comment()
        TeamEmojiMaster.objects.create(team=self.team, name="party-blob", image="x.png")
        with _Pushes():
            self.mark(comment_id, reaction=":party-blob:")
        self.assertEqual([e for _, e, *_ in self.reactions(comment_id)], [":party-blob:"])

    def test_a_global_default_emoji_counts_as_the_teams(self):
        comment_id = self.comment()
        TeamEmojiMaster.objects.create(team=None, name="shipit", image="x.png")
        with _Pushes():
            self.mark(comment_id, reaction=":shipit:")
        self.assertEqual(len(self.reactions(comment_id)), 1)

    def test_a_shortcode_the_catalog_lacks_is_refused(self):
        comment_id = self.comment()
        TeamEmojiMaster.objects.create(team=self.team, name="gone", image="x.png", is_deleted=True)
        other = TeamMaster.objects.get(team_name="Other")
        TeamEmojiMaster.objects.create(team=other, name="theirs", image="x.png")
        for code in (":nope:", ":gone:", ":theirs:"):
            with self.subTest(code=code):
                self.refused(comment_id, "custom emoji", reaction=code)
        self.assertEqual(self.reactions(comment_id), [])

    def test_what_is_not_an_emoji_is_refused(self):
        comment_id = self.comment()
        for bad in (
            "",
            "   ",
            "lol",
            "yes!",
            ":)",
            "了解",
            "👍 👍",
            "👍\n👍",
            "x" * (MAX_EMOJI_CHARS + 1),
            "👍" * (MAX_EMOJI_CHARS + 1),
            7,
            ["👍"],
            True,
        ):
            with self.subTest(bad=bad):
                self.refused(comment_id, reaction=bad)
        self.assertEqual(self.reactions(comment_id), [])

    def test_the_emoji_the_app_can_send_are_accepted(self):
        comment_id = self.comment()
        for good in ("👍", "👍🏽", "❤️", "🏳️‍🌈", "🇯🇵", "1️⃣", "#️⃣", "👨‍👩‍👧‍👦", "ℹ️"):
            with self.subTest(good=good), _Pushes():
                out = self.mark(comment_id, reaction=good)
                self.assertTrue(out["changed"], good)

    def test_a_padded_emoji_is_stored_trimmed(self):
        comment_id = self.comment()
        with _Pushes():
            self.mark(comment_id, reaction=" 👍 ")
        self.assertEqual([e for _, e, *_ in self.reactions(comment_id)], ["👍"])


class ReactionAccessTests(MarksBase):
    """The app's rule: a comment on a task the caller can open. Anything else reads as missing."""

    def test_a_comment_of_another_team_reads_as_missing(self):
        comment_id = self.comment()
        message = self.refused(comment_id, ctx=self.stranger_ctx, exc=ToolForbidden, reaction="👍")
        self.assertEqual(message, f"Task {self.task.task_id} not found.")
        self.assertEqual(self.reactions(comment_id), [])

    def test_a_task_the_caller_cannot_open_is_refused(self):
        comment_id = self.comment()
        outsider = User.objects.create_user(username="out", email="out@example.com", password="x")
        TeamMembers.objects.create(team=self.team, attendee=outsider)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(outsider.id))
        self.refused(comment_id, ctx=ctx, exc=ToolForbidden, reaction="👍")
        self.assertEqual(self.reactions(comment_id), [])

    def test_an_assignee_off_the_project_can_react_as_on_the_page(self):
        comment_id = self.comment(task=self.guest_task)
        with _Pushes():
            out = self.mark(comment_id, task=self.guest_task, ctx=self.guest_ctx, reaction="👍")
        self.assertTrue(out["changed"])

    def test_an_unknown_or_deleted_comment_reads_as_missing(self):
        gone = self.comment()
        TaskComments.objects.filter(task=self.task, comment_id=gone).update(is_deleted=True)
        for comment_id in (gone, 9999):
            with self.subTest(comment_id=comment_id):
                message = self.refused(comment_id, reaction="👍")
                self.assertIn(f"Comment {comment_id} not found on {self.task.display_id}", message)
        self.assertFalse(TaskCommentReactionFact.objects.exists())

    def test_a_comment_of_another_task_is_not_found_on_this_one(self):
        other = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Other", status="WIP"
        )
        comment_id = self.comment(task=other)
        # The same number on this task is another comment, or none.
        self.refused(comment_id + 40, reaction="👍")
        self.assertFalse(TaskCommentReactionFact.objects.exists())

    def test_a_bad_id_is_refused_not_a_server_error(self):
        for bad in ("abc", None, [1]):
            with self.subTest(bad=bad):
                self.refused(bad, reaction="👍")


class CallShapeTests(MarksBase):
    def test_one_of_body_text_reaction_flag_per_call(self):
        comment_id = self.comment(ctx=self.ctx)
        for extra in (
            {"body_text": "new", "reaction": "👍"},
            {"body_text": "new", "flag": "flag"},
            {"reaction": "👍", "flag": "flag"},
        ):
            with self.subTest(extra=extra):
                self.refused(comment_id, "ONE of", **extra)
        self.assertEqual(self.reactions(comment_id), [])
        self.assertEqual(self.flags(comment_id), [])
        row = TaskComments.objects.get(task=self.task, comment_id=comment_id)
        self.assertNotEqual(plain_text(row.comment_body), "new")

    def test_a_blank_body_text_beside_a_reaction_is_not_a_second_thing(self):
        comment_id = self.comment()
        with _Pushes():
            out = self.mark(comment_id, body_text="  ", reaction="👍")
        self.assertTrue(out["changed"])

    def test_remove_reaction_needs_a_reaction(self):
        comment_id = self.comment()
        self.refused(comment_id, "goes with `reaction`", remove_reaction=True)
        self.refused(comment_id, "true or false", reaction="👍", remove_reaction="yes")

    def test_an_unknown_flag_value_is_refused(self):
        comment_id = self.comment()
        self.refused(comment_id, "`flag` must be one of", flag="star")

    def test_nothing_to_do_says_what_to_send(self):
        comment_id = self.comment(ctx=self.ctx)
        self.refused(comment_id, "Send `body_text` to edit your comment, or `reaction` or `flag`.")

    def test_editing_is_as_it_was_and_still_only_your_own(self):
        own, theirs = self.comment(ctx=self.ctx), self.comment(ctx=self.ctx2)
        out = self.mark(own, body_text="reworded")
        self.assertEqual(out["comment_id"], own)
        self.refused(theirs, "Only your own comments can be changed", body_text="reworded")

    def test_it_still_needs_approval(self):
        self.assertTrue(UPDATE_COMMENT.requires_approval)
        self.assertIs(REGISTRY["update_comment"], UPDATE_COMMENT)


def plain_text(body) -> str:
    return "\n".join(
        "".join(span.get("text", "") for span in block.get("content") or []) for block in body or []
    ).strip()


# ---- flags: the tool against the app's endpoint ---------------------------------------------


class FlagTwinTests(MarksBase):
    def remind(self, comment_id):
        message_reminders.set_reminder(
            user=self.user,
            message=self.mirror(comment_id),
            remind_at=timezone.now() + timedelta(days=1),
        )

    def ui(self, method, comment_id, **body):
        self.authenticate(self.user)
        url = f"/api/v3/messages/{self.mirror(comment_id).id}/flag/"
        res = getattr(self.client, method)(url, body or None, format="json")
        self.assertLess(res.status_code, 300, getattr(res, "data", None))
        return res

    def test_flagging_leaves_what_the_apps_endpoint_leaves(self):
        by_tool, by_ui = self.comment(), self.comment()
        with watching_notices() as tool_notices:
            out = self.mark(by_tool, flag="flag")
        with watching_notices() as ui_notices:
            self.ui("post", by_ui)
        self.assertTrue(out["changed"])
        self.assertEqual(out["flag"], "flagged")
        self.assertEqual(self.flags(by_tool), [None])
        self.assertEqual(self.flags(by_tool), self.flags(by_ui))
        # A flag is the caller's own: nobody is told, through any channel.
        self.assertEqual(tool_notices, ui_notices)
        self.assertEqual(tool_notices, {"rows": {}, "emails": 0, "pushes": {}})

    def test_marking_done_leaves_what_the_apps_patch_leaves(self):
        by_tool, by_ui = self.comment(), self.comment()
        for comment_id in (by_tool, by_ui):
            self.remind(comment_id)
        with watching_notices() as tool_notices:
            out = self.mark(by_tool, flag="done")
        with watching_notices() as ui_notices:
            self.ui("patch", by_ui, completed=True)
        self.assertEqual(out["flag"], "done")
        self.assertIsNotNone(self.flags(by_tool)[0])
        self.assertIsNotNone(self.flags(by_ui)[0])
        # Done retires the pending reminder on both.
        self.assertEqual(
            list(message_reminders.pending_for_user(self.user)),
            [],
        )
        self.assertEqual(tool_notices, ui_notices)
        self.assertEqual(tool_notices["rows"], {})

    def test_unflagging_leaves_what_the_apps_delete_leaves(self):
        by_tool, by_ui = self.comment(), self.comment()
        self.mark(by_tool, flag="flag")
        self.ui("post", by_ui)
        for comment_id in (by_tool, by_ui):
            self.remind(comment_id)
        with watching_notices() as tool_notices:
            out = self.mark(by_tool, flag="unflag")
        with watching_notices() as ui_notices:
            self.ui("delete", by_ui)
        self.assertEqual(out["flag"], "not flagged")
        self.assertEqual(self.flags(by_tool), [])
        self.assertEqual(self.flags(by_ui), [])
        # Unflagging is the other way of being done with it: the reminder goes too.
        self.assertEqual(list(message_reminders.pending_for_user(self.user)), [])
        self.assertEqual(tool_notices, ui_notices)

    def test_flagging_a_done_flag_reopens_it_in_place(self):
        by_tool, by_ui = self.comment(), self.comment()
        self.mark(by_tool, flag="flag")
        self.mark(by_tool, flag="done")
        self.ui("post", by_ui)
        self.ui("patch", by_ui, completed=True)
        first_id = Flag.objects.get(user=self.user, message=self.mirror(by_tool)).pk

        out = self.mark(by_tool, flag="flag")
        self.ui("post", by_ui)

        self.assertTrue(out["changed"])
        self.assertEqual(self.flags(by_tool), [None])
        self.assertEqual(self.flags(by_ui), [None])
        self.assertEqual(
            Flag.objects.get(user=self.user, message=self.mirror(by_tool)).pk, first_id
        )

    def test_flagging_twice_changes_nothing(self):
        comment_id = self.comment()
        self.mark(comment_id, flag="flag")
        out = self.mark(comment_id, flag="flag")
        self.assertFalse(out["changed"])
        self.assertIn("Already flagged", out["__summary__"])
        self.assertEqual(self.flags(comment_id), [None])

    def test_unflagging_what_is_not_flagged_is_not_an_error(self):
        comment_id = self.comment()
        out = self.mark(comment_id, flag="unflag")
        self.assertFalse(out["changed"])
        self.assertEqual(self.flags(comment_id), [])

    def test_done_on_a_comment_that_is_not_flagged_says_so_and_changes_nothing(self):
        comment_id = self.comment()
        out = self.mark(comment_id, flag="done")
        self.assertFalse(out["changed"])
        self.assertEqual(out["flag"], "not flagged")
        self.assertIn("nothing to mark done", out["__summary__"])
        self.assertEqual(self.flags(comment_id), [])

    def test_done_twice_changes_nothing_the_second_time(self):
        comment_id = self.comment()
        self.mark(comment_id, flag="flag")
        first = self.mark(comment_id, flag="done")
        stamp = self.flags(comment_id)[0]
        again = self.mark(comment_id, flag="done")
        self.assertTrue(first["changed"])
        self.assertFalse(again["changed"])
        self.assertEqual(self.flags(comment_id)[0], stamp)

    def test_a_flag_is_the_callers_own(self):
        comment_id = self.comment()
        self.mark(comment_id, flag="flag", ctx=self.ctx2)
        self.mark(comment_id, flag="flag")
        self.mark(comment_id, flag="unflag", ctx=self.ctx2)
        self.assertEqual(self.flags(comment_id), [None])
        self.assertEqual(self.flags(comment_id, user=self.user2), [])

    def test_flagging_touches_neither_the_comment_nor_its_history(self):
        comment_id = self.comment()
        before = TaskComments.objects.filter(task=self.task, comment_id=comment_id).values().get()
        history = TaskActivity.objects.count()
        self.mark(comment_id, flag="flag")
        self.mark(comment_id, flag="done")
        self.assertEqual(
            TaskComments.objects.filter(task=self.task, comment_id=comment_id).values().get(),
            before,
        )
        self.assertEqual(TaskActivity.objects.count(), history)


class FlagAccessTests(MarksBase):
    def test_someone_off_the_project_chat_cannot_flag_as_the_app_does_not_offer_it(self):
        """The assignee of a task can open it without being on its project, and the flag lives on
        the project chat's copy of the comment: the page hides the item (MIP-1442), so the tool
        refuses, with nothing written."""
        comment_id = self.comment(task=self.guest_task)
        message = self.refused(comment_id, task=self.guest_task, ctx=self.guest_ctx, flag="flag")
        self.assertIn("can't be flagged", message)
        self.assertFalse(Flag.objects.exists())

    def test_the_app_refuses_the_same_person_the_same_way(self):
        comment_id = self.comment(task=self.guest_task)
        self.authenticate(self.guest)
        res = self.client.post(
            f"/api/v3/messages/{self.mirror(comment_id, self.guest_task).id}/flag/", format="json"
        )
        self.assertEqual(res.status_code, 404)

    def test_a_comment_with_no_copy_in_the_chat_cannot_be_flagged(self):
        comment_id = self.comment()
        self.mirror(comment_id).delete()
        self.refused(comment_id, "can't be flagged", flag="flag")
        self.assertFalse(Flag.objects.exists())

    def test_another_teams_task_and_unknown_comments_read_as_missing(self):
        comment_id = self.comment()
        self.refused(comment_id, ctx=self.stranger_ctx, exc=ToolForbidden, flag="flag")
        self.refused(9999, "not found", flag="flag")
        gone = self.comment()
        TaskComments.objects.filter(task=self.task, comment_id=gone).update(is_deleted=True)
        self.refused(gone, "not found", flag="flag")
        self.assertFalse(Flag.objects.exists())


class FlagOffTests(MarksBase):
    """With the flag off the tool is what it was: the new parameters are not declared, and a call
    that sends one anyway has it ignored."""

    def setUp(self):
        super().setUp()
        off = mock.patch.dict(settings.SEARCH_ENGINE, {"AGENT_PARITY_TOOLS": False})
        off.start()
        self.addCleanup(off.stop)

    def test_a_reaction_without_body_text_is_the_old_refusal(self):
        comment_id = self.comment(ctx=self.ctx)
        self.refused(comment_id, "`body_text` is required and must be non-empty.", reaction="👍")
        self.refused(comment_id, "`body_text` is required and must be non-empty.", flag="flag")
        self.assertFalse(TaskCommentReactionFact.objects.exists())
        self.assertFalse(Flag.objects.exists())

    def test_the_extras_beside_a_body_are_ignored_and_the_edit_goes_through(self):
        comment_id = self.comment(ctx=self.ctx)
        out = self.mark(comment_id, body_text="reworded", reaction="👍", flag="flag")
        self.assertEqual(out["comment_id"], comment_id)
        self.assertNotIn("changed", out)
        self.assertFalse(TaskCommentReactionFact.objects.exists())
        self.assertFalse(Flag.objects.exists())
        row = TaskComments.objects.get(task=self.task, comment_id=comment_id)
        self.assertEqual(plain_text(row.comment_body), "reworded")


# ---- the approval card --------------------------------------------------------------------


class ApprovalCardTests(MarksBase):
    """`update_comment` stays approval-gated. The card for a reaction or a flag names whose comment
    it is, so the approver can tell what they are agreeing to."""

    def card(self, ctx=None, **args):
        from origin.search_engine.agent import controller  # noqa: PLC0415

        return controller._friendly_arguments(
            {"task_id": self.task.task_id, **args}, "update_comment", ctx=ctx or self.ctx
        )

    def test_a_reaction_card_names_the_author_and_quotes_the_comment(self):
        comment_id = self.comment(text="Can you check the footer spacing?\n\nIt looks tight.")
        shown = self.card(comment_id=comment_id, reaction="👍")
        self.assertEqual(
            shown["comment"],
            {"author": "otheruser", "excerpt": "Can you check the footer spacing?"},
        )
        self.assertEqual(shown["task_id"], self.task.display_id)

    def test_a_flag_card_does_too(self):
        comment_id = self.comment()
        self.assertEqual(
            self.card(comment_id=comment_id, flag="flag")["comment"]["author"], "otheruser"
        )

    def test_a_long_first_line_is_cut(self):
        comment_id = self.comment(text="x" * 400)
        excerpt = self.card(comment_id=comment_id, reaction="👍")["comment"]["excerpt"]
        self.assertLessEqual(len(excerpt), 160)
        self.assertTrue(excerpt.endswith("…"))

    def test_an_edit_card_is_as_it_was(self):
        comment_id = self.comment(ctx=self.ctx)
        self.assertNotIn("comment", self.card(comment_id=comment_id, body_text="new"))

    def test_nothing_is_quoted_for_a_task_the_caller_cannot_read(self):
        comment_id = self.comment()
        shown = self.card(ctx=self.stranger_ctx, comment_id=comment_id, reaction="👍")
        self.assertNotIn("comment", shown)
        self.assertEqual(shown["comment_id"], comment_id)

    def test_nothing_is_quoted_for_a_deleted_or_unknown_comment(self):
        gone = self.comment()
        TaskComments.objects.filter(task=self.task, comment_id=gone).update(is_deleted=True)
        for comment_id in (gone, 9999, "abc"):
            with self.subTest(comment_id=comment_id):
                self.assertNotIn("comment", self.card(comment_id=comment_id, reaction="👍"))

    def test_with_the_flag_off_nothing_is_added(self):
        comment_id = self.comment()
        with mock.patch.dict(settings.SEARCH_ENGINE, {"AGENT_PARITY_TOOLS": False}):
            self.assertNotIn("comment", self.card(comment_id=comment_id, reaction="👍"))

    def test_a_reaction_has_nothing_to_edit_before_approving(self):
        from origin.search_engine.agent import approval_edits  # noqa: PLC0415

        comment_id = self.comment()
        call = {"task_id": self.task.task_id, "comment_id": comment_id, "reaction": "👍"}
        self.assertEqual(approval_edits.editable_paths("update_comment", call), [])
        edit = {**call, "body_text": "new"}
        self.assertEqual(approval_edits.editable_paths("update_comment", edit), ["body_text"])

    def test_it_is_predicted_as_not_undoable_with_the_codes_reason(self):
        from origin.search_engine.agent import undo  # noqa: PLC0415

        for args in ({"reaction": "👍"}, {"flag": "flag"}, {"body_text": "new"}):
            with self.subTest(args=args):
                prediction = undo.proposal("update_comment", {"comment_id": 1, **args})
                self.assertFalse(prediction.undoable)
                self.assertEqual(prediction.reason, undo.NO_SNAPSHOT)
