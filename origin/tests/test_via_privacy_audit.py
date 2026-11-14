"""MIP-1524: every reader of a write's `via` keeps another member's key or app name to its owner.

api #776/#758 (MIP-1470, MIP-1485) gave `via_for_viewer` one rule: a key writes as its owner, so
on the row the viewer made `via` is whole, and on anyone else's it is the kind only. The task's
history tools, the web client's activity feed and the comment list were pinned one by one. This
module is the audit of the rest: every other surface that reads `TaskActivity` for a colleague
(`get_timeline`, `get_item_line`, `get_my_briefing`, the in-app `get_task_history`) must not carry
the key's id, name or prefix, or an app's connection id or client name, whatever shape it tells.

Nothing in `timeline`, `item_lines` or `briefing` tells a write's `via` (their `via` is a routing
word: task, chip, claim, marker) and the only app name they carry is the member's OWN marker. The
tests here pin that, with a control: the same secrets ARE readable by their owner on the surface
that shows `via`, so a clean result is not an empty one.
"""

from __future__ import annotations

import json

from rest_framework import status

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import item_lines, timeline
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_item_lines import LineBase

KEY = {
    "kind": "api_key",
    "key_id": "k-alice-1",
    "key_name": "Alice private phone",
    "key_prefix": "gk_alicepfx",
}
APP = {"kind": "oauth", "connection_id": "conn-alice-9", "client_name": "Alice Claude Desktop"}
SECRETS = (
    "Alice private phone",
    "k-alice-1",
    "gk_alicepfx",
    "conn-alice-9",
    "Alice Claude Desktop",
)


def leaks(payload) -> list[str]:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return [s for s in SECRETS if s in text]


class TestEveryReaderOfViaKeepsTheNameToItsOwner(LineBase):
    """The viewer is `self.user`; the writes are `self.user2`'s, made with a key and an app."""

    def setUp(self):
        super().setUp()
        self.task = self.make_task("Ship the thing", assignee=self.user, reporter=self.user)
        self.row("status_changed", "status", "Open", "WIP", KEY, 10)
        self.row("assignee_changed", "assignee", None, str(self.user.id), APP, 20)
        self.row("description_edited", "description", None, None, KEY, 30)
        self.row("description_edited", "description", None, None, APP, 31)
        self.row("status_changed", "status", "WIP", "Closed", APP, 40)
        self.row("priority_changed", "priority", "Low", "High", KEY, 50)

    def row(self, action, field, old, new, via, minutes):
        row = TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=self.user2,
            action_type=action,
            field_name=field,
            old_value=old,
            new_value=new,
            metadata={"via": via},
        )
        TaskActivity.objects.filter(pk=row.pk).update(ts_created_at=self.at(minutes))
        return row

    def tool_text(self, name, **args):
        text, is_error = self._tool_text(self._call(name, args))
        self.assertFalse(is_error, text)
        return text

    def test_the_item_line_service_and_tool_carry_no_key_or_app_identity(self):
        out = item_lines.item_lines(self.ctx, self.task.task_id, self.since, self.until)
        self.assertIn("activity", json.dumps(out, default=str), "the rows were read")
        self.assertEqual(leaks(out), [])
        text = self.tool_text(
            "get_item_line",
            task_id=str(self.task.task_id),
            since=self.since.isoformat(),
            until=self.until.isoformat(),
        )
        self.assertEqual(leaks(text), [])

    def test_the_timeline_service_and_both_tools_carry_no_key_or_app_identity(self):
        out = timeline.timeline(
            self.ctx,
            "task",
            str(self.task.task_id),
            since=self.since.isoformat(),
            until=self.until.isoformat(),
        )
        events = out["events"]
        self.assertTrue(events, "the task's history was read")
        self.assertEqual(leaks(out), [])
        self.assertTrue(all(e.get("via") != "api_key" for e in events))
        text = self.tool_text(
            "get_timeline",
            anchor_type="task",
            anchor_id=str(self.task.task_id),
            since=self.since.isoformat(),
            until=self.until.isoformat(),
        )
        self.assertEqual(leaks(text), [])

    def test_the_briefing_carries_no_key_or_app_identity(self):
        # The briefing tells the member's OWN writes ("what you did", `via` = the kind word only)
        # and a colleague's changes without any `via`: a mutation that adds the key's identity to
        # the colleague's row is not reachable here, so this pins the shape, not a gate.
        TaskActivity.objects.filter(task=self.task).update(ts_created_at=self.at(0))
        text = self.tool_text("get_my_briefing", window="week")
        self.assertEqual(leaks(text), [])

    def test_the_in_app_task_history_hides_it_from_a_colleague_and_shows_it_to_the_owner(self):
        tool = REGISTRY["get_task_history"]
        for_colleague = tool.run(
            {"task_id": self.task.task_id},
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id)),
        )
        self.assertEqual(leaks(for_colleague), [])
        # The control: the owner reads the same rows whole, so the secrets are findable here.
        for_owner = tool.run(
            {"task_id": self.task.task_id},
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id)),
        )
        self.assertIn("Alice private phone", json.dumps(for_owner, default=str))

    def test_the_member_sees_only_their_own_app_in_a_marker(self):
        # The one place a surface names an app: the member's OWN markers ("your AI app").
        from origin.services import member_events  # noqa: PLC0415

        member_events.record(
            user_id=str(self.user2.id),
            team_id=str(self.team.team_id),
            items=[("task", str(self.task.task_id))],
            kind=member_events.KIND_WRITE,
            client="Alice Claude Desktop",
            now=self.at(15),
        )
        from origin.models.common.member_event_models import MemberEvent  # noqa: PLC0415

        self.assertEqual(
            MemberEvent.objects.filter(
                user_id=self.user2.id, client="Alice Claude Desktop"
            ).count(),
            1,
            "control: the marker was written",
        )
        out = item_lines.item_lines(self.ctx, self.task.task_id, self.since, self.until)
        self.assertNotIn("Alice Claude Desktop", json.dumps(out, default=str))


class TestTheCommentListReducesVia(BaseAPITestCase):
    """The web client's comment list: a comment written with another member's key."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Site",
            code="SIT",
            owner=self.user,
            project_system_user=self.user,
        )
        for member in (self.user, self.user2):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Spec", status="Open"
        )
        self.comment = TaskComments.objects.create(
            task=self.task,
            sender=self.user2,
            comment_id=1,
            comment_body=[
                {"type": "paragraph", "content": [{"type": "text", "text": "hi", "styles": {}}]}
            ],
        )
        TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=self.user2,
            action_type="comment_added",
            field_name="comment",
            metadata={"commentId": 1, "via": KEY},
        )

    def comments(self, viewer):
        self.authenticate(viewer)
        res = self.client.get("/api/v2/task/comment/", {"task_id": self.task.task_id})
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        return res.data

    def test_a_colleague_reads_the_kind_only(self):
        data = self.comments(self.user)
        self.assertEqual(data[0]["via"], {"kind": "api_key"})
        self.assertEqual(leaks(data), [])

    def test_the_writer_reads_their_own_key_whole(self):
        self.assertEqual(self.comments(self.user2)[0]["via"], KEY)
