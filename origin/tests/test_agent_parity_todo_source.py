"""`create_todo_item` takes `task_id` and `comment_id` (MIP-1562, flag `AGENT_PARITY_TOOLS`).

The app's "Add to To-Do" on a task comment makes a to-do whose notes open with one italic line,
"From task comment: <link to the comment>" (`appendTodoFromMessage.ts`, MIP-1434 / 1436). The tool
could make the to-do and the reminder but not the link back. The core test makes the to-do through
the app's own `POST /api/v2/todo/items/` with the notes the app builds, and through the tool, on
twin comments, and compares what each leaves. The rest pin each guard: a refused call leaves no
to-do behind.
"""

from __future__ import annotations

from datetime import date
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model

from origin.models.chat.todo_models import ToDoItem, ToDoItemReminder
from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.add_comment import ADD_COMMENT
from origin.search_engine.agent.tools.base import ToolForbidden
from origin.search_engine.agent.tools.create_todo_item import CREATE_TODO_ITEM
from origin.services.user_time import today_for_user_id

from .notice_watch import watching_notices
from .test_base import BaseAPITestCase

User = get_user_model()
PARITY = {"AGENT_PARITY_TOOLS": True}


def ui_notes(label: str, href: str) -> list[dict]:
    """The notes `appendTodoFromMessage.ts` builds, as it sends them."""
    return [
        {
            "type": "paragraph",
            "props": {
                "textColor": "default",
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [
                {"text": f"{label}: ", "type": "text", "styles": {"italic": True}},
                {
                    "href": href,
                    "type": "link",
                    "content": [{"text": href, "type": "text", "styles": {}}],
                },
            ],
            "children": [],
        }
    ]


def shape(notes) -> list:
    """What a reader sees in the notes: each block's type, its inline pieces, link targets."""
    return [
        (
            block.get("type"),
            [
                (
                    piece.get("type"),
                    piece.get("text") or piece.get("href"),
                    sorted((piece.get("styles") or {}).items()),
                    [c.get("text") for c in piece.get("content") or []]
                    if piece.get("type") == "link"
                    else None,
                )
                for piece in block.get("content") or []
            ],
        )
        for block in notes or []
    ]


class TodoSourceBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
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
        channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        Message.objects.create(
            channel=channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="x"
        )
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=other, attendee=self.stranger)
        self.stranger_ctx = ToolContext(team_id=str(other.team_id), user_id=str(self.stranger.id))
        self.base = settings.FRONTEND_BASE_URL.rstrip("/")

    def comment(self, task=None, text="Can you check the footer spacing?"):
        task = task or self.task
        return ADD_COMMENT.run({"task_id": task.task_id, "body_text": text}, self.ctx2)[
            "comment_id"
        ]

    def todo(self, **extra):
        return CREATE_TODO_ITEM.run({"title": "Check the footer", **extra}, self.ctx)

    def href(self, comment_id=None, task=None):
        task = task or self.task
        path = f"/workspace/tasks/project/{self.project.project_id}/task/{task.task_id}"
        return f"{self.base}{path}" + (f"/comment/{comment_id}" if comment_id else "")

    def refused(self, message=None, ctx=None, exc=ToolError, **extra):
        before = ToDoItem.objects.count()
        with self.assertRaises(exc) as caught:
            CREATE_TODO_ITEM.run({"title": "Check the footer", **extra}, ctx or self.ctx)
        if message:
            self.assertIn(message, str(caught.exception))
        self.assertEqual(ToDoItem.objects.count(), before, "a refused call left a to-do behind")
        return str(caught.exception)

    def item(self, item_id) -> ToDoItem:
        return ToDoItem.objects.select_related("group").get(item_id=item_id)


class TwinTests(TodoSourceBase):
    def ui_add(self, comment_id):
        self.authenticate()
        res = self.client.post(
            "/api/v2/todo/items/",
            {
                "team_id": str(self.team.team_id),
                "local_date": today_for_user_id(self.user.id).isoformat(),
                "title": "Can you check the footer spacing?",
                "notes": ui_notes("From task comment", self.href(comment_id)),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, getattr(res, "data", None))
        return res.data["item_id"] if "item_id" in res.data else res.data["itemId"]

    def test_the_to_do_links_back_as_the_apps_add_to_to_do_does(self):
        by_tool, by_ui = self.comment(), self.comment()

        with watching_notices() as tool_notices:
            out = self.todo(
                title="Can you check the footer spacing?",
                task_id=self.task.task_id,
                comment_id=by_tool,
            )
        with watching_notices() as ui_notices:
            ui_id = self.ui_add(by_ui)

        mine, theirs = self.item(out["item_id"]), self.item(ui_id)
        self.assertEqual(mine.title, theirs.title)
        self.assertEqual(mine.group.local_date, theirs.group.local_date)
        self.assertEqual(mine.category_id, theirs.category_id)
        self.assertEqual(mine.is_completed, theirs.is_completed)
        # The notes: one italic "From task comment: " and the link to THAT comment, both ways.
        self.assertEqual(
            shape(mine.notes),
            shape(ui_notes("From task comment", self.href(by_tool))),
        )
        self.assertEqual(
            shape(theirs.notes),
            shape(ui_notes("From task comment", self.href(by_ui))),
        )
        self.assertEqual(
            len(mine.notes), len(theirs.notes), "the notes are the one line, as the app's are"
        )
        # A to-do tells nobody, whichever way it was made.
        self.assertEqual(tool_notices, ui_notices)
        self.assertEqual(tool_notices, {"rows": {}, "emails": 0, "pushes": {}})
        self.assertEqual(out["source"]["comment_id"], by_tool)
        self.assertIn("from task comment on", out["__summary__"])

    def test_a_task_alone_links_the_task_as_the_cards_add_to_to_do_does(self):
        out = self.todo(task_id=self.task.task_id)
        self.assertEqual(
            shape(self.item(out["item_id"]).notes), shape(ui_notes("From task", self.href()))
        )
        self.assertIsNone(out["source"]["comment_id"])

    def test_the_link_opens_the_page_the_app_routes_to(self):
        comment_id = self.comment()
        out = self.todo(task_id=self.task.task_id, comment_id=comment_id)
        link = self.item(out["item_id"]).notes[0]["content"][1]
        self.assertEqual(
            link["href"],
            f"{self.base}/workspace/tasks/project/{self.project.project_id}"
            f"/task/{self.task.task_id}/comment/{comment_id}",
        )
        self.assertEqual(link["content"][0]["text"], link["href"])


class ComposesWithTheRestTests(TodoSourceBase):
    def test_the_agents_own_notes_follow_the_source_line(self):
        comment_id = self.comment()
        out = self.todo(
            task_id=self.task.task_id,
            comment_id=comment_id,
            notes_text="Ask Sam for the new spacing values",
        )
        notes = self.item(out["item_id"]).notes
        self.assertEqual(
            shape(notes)[0], shape(ui_notes("From task comment", self.href(comment_id)))[0]
        )
        self.assertIn("Ask Sam for the new spacing values", str(notes[1]))
        self.assertEqual(notes[-1]["content"], [], "the editor's trailing blank line is kept")

    def test_a_reminder_is_set_in_the_same_call(self):
        comment_id = self.comment()
        out = self.todo(task_id=self.task.task_id, comment_id=comment_id, remind_in_minutes=60)
        self.assertTrue(ToDoItemReminder.objects.filter(item_id=out["item_id"]).exists())
        self.assertIn("reminder_at", out)
        self.assertEqual(out["source"]["comment_id"], comment_id)

    def test_a_display_id_names_the_task(self):
        comment_id = self.comment()
        out = self.todo(task_id=self.task.display_id, comment_id=comment_id)
        self.assertEqual(out["source"]["task_id"], self.task.task_id)

    def test_a_task_url_names_the_task(self):
        out = self.todo(task_id=self.href())
        self.assertEqual(out["source"]["task_id"], self.task.task_id)

    def test_without_a_source_nothing_changes(self):
        out = self.todo(notes_text="plain")
        self.assertNotIn("source", out)
        notes = self.item(out["item_id"]).notes
        self.assertNotIn("From task", str(notes))

    def test_the_item_lands_on_the_target_date_as_before(self):
        comment_id = self.comment()
        out = self.todo(task_id=self.task.task_id, comment_id=comment_id, target_date="2026-12-01")
        self.assertEqual(self.item(out["item_id"]).group.local_date, date(2026, 12, 1))


class GuardTests(TodoSourceBase):
    def test_a_comment_needs_its_task(self):
        self.refused("`comment_id` needs `task_id`", comment_id=1)

    def test_an_unknown_or_deleted_comment_reads_as_missing(self):
        gone = self.comment()
        TaskComments.objects.filter(task=self.task, comment_id=gone).update(is_deleted=True)
        for comment_id in (gone, 9999):
            with self.subTest(comment_id=comment_id):
                self.refused(
                    f"Comment {comment_id} not found on {self.task.display_id}",
                    task_id=self.task.task_id,
                    comment_id=comment_id,
                )

    def test_a_comment_of_another_task_is_not_found_on_this_one(self):
        other = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Other", status="WIP"
        )
        comment_id = self.comment(task=other)
        self.refused("not found", task_id=self.task.task_id, comment_id=comment_id + 40)

    def test_another_teams_task_reads_as_missing(self):
        comment_id = self.comment()
        message = self.refused(
            ctx=self.stranger_ctx,
            exc=ToolForbidden,
            task_id=self.task.task_id,
            comment_id=comment_id,
        )
        self.assertEqual(message, f"Task {self.task.task_id} not found.")

    def test_a_task_the_caller_cannot_open_is_refused(self):
        outsider = User.objects.create_user(username="out", email="out@example.com", password="x")
        TeamMembers.objects.create(team=self.team, attendee=outsider)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(outsider.id))
        comment_id = self.comment()
        self.refused(ctx=ctx, exc=ToolForbidden, task_id=self.task.task_id, comment_id=comment_id)

    def test_a_deleted_task_is_refused(self):
        TaskMaster.objects.filter(task_id=self.task.task_id).update(is_deleted=True)
        self.refused("deleted", task_id=self.task.task_id)

    def test_a_reference_that_names_nothing_is_refused(self):
        self.refused(task_id="NOPE-1")
        self.refused("No task WRD-999", task_id="WRD-999")

    def test_the_comments_text_is_not_copied_into_the_to_do(self):
        """The title is the model's own words; the comment's text never reaches the to-do through
        the link, whatever the caller may read."""
        comment_id = self.comment(text="secret launch date 2027-01-01")
        out = self.todo(task_id=self.task.task_id, comment_id=comment_id)
        item = self.item(out["item_id"])
        self.assertNotIn("secret", item.title)
        self.assertNotIn("secret", str(item.notes))


class FlagOffTests(TodoSourceBase):
    """With the flag off the tool is what it was: the two parameters are not declared, and a call
    that sends them anyway has them ignored, even a reference that names nothing."""

    def setUp(self):
        super().setUp()
        off = mock.patch.dict(settings.SEARCH_ENGINE, {"AGENT_PARITY_TOOLS": False})
        off.start()
        self.addCleanup(off.stop)

    def test_the_to_do_is_made_with_no_source(self):
        comment_id = self.comment()
        out = self.todo(task_id=self.task.task_id, comment_id=comment_id)
        self.assertNotIn("source", out)
        self.assertIsNone(self.item(out["item_id"]).notes)

    def test_a_reference_that_names_nothing_is_never_looked_up(self):
        out = self.todo(task_id="NOPE-1", comment_id="x")
        self.assertNotIn("source", out)
        self.assertEqual(ToDoItem.objects.count(), 1)
