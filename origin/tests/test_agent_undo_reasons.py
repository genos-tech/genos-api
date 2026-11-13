"""What `undo.UNDO_REASONS` says about telling people is true of what the tool does.

The approval card shows the reason code before a person approves ("can't be undone: ..."),
and `notified_people` is the one that is about other people being told. A tool filed under
it has to tell someone. So the tools are run here, through `watching_notices`, and what they
send is compared with how they are filed:

  * `manage_note_sharing` and `manage_project_members` give access and send nothing, so they
    are `not_yet`: restorable in principle (the same tool, or the page's own remove, puts it
    back), the restore is not built. (A task's own history row, `TaskActivity`, and a
    security-audit row are records, not notices, and are not watched.)
  * the tools that DO tell somebody are filed that way, with the notice they send:
    `approve_join_request` and `decline_join_request` write the person an inbox item and
    push it; `create_calendar_event` hands its guests to Google as attendees (it sends no
    email of its own: `sendUpdates=none`); `assign_task` tells the new assignee (a mention on
    the task's card and a push, MIP-1461) and tells nobody when it unassigns.
"""

from __future__ import annotations

from unittest import mock

from origin.models.common.inbox_models import InboxItems
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import undo
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.assign_task import ASSIGN_TASK
from origin.search_engine.agent.tools.create_calendar_event import CREATE_CALENDAR_EVENT
from origin.search_engine.agent.tools.manage_project_members import MANAGE_PROJECT_MEMBERS
from origin.services.task_card_post import post_task_created_card

from .notice_watch import watching_notices
from .test_agent_tool_join_requests import JoinToolBase
from .test_agent_tool_note_sharing import SharingBase
from .test_base import BaseAPITestCase

NOBODY = {"rows": {}, "emails": 0, "pushes": {}}


class SharingTellsNobody(SharingBase):
    def test_sharing_a_note_sends_nothing_to_anyone(self):
        with watching_notices() as seen:
            self.manage(self.share_args(role="editor"))
        self.assertEqual(seen, NOBODY)

    def test_taking_the_share_back_sends_nothing_to_anyone(self):
        self.manage(self.share_args(role="viewer"))
        with watching_notices() as seen:
            self.manage({**self.share_args(role="viewer"), "action": "unshare"})
        self.assertEqual(seen, NOBODY)

    def test_so_it_is_filed_as_not_yet_and_never_as_telling_people(self):
        code, line = undo.UNDO_REASONS["manage_note_sharing"]
        self.assertEqual(code, undo.NOT_YET)
        self.assertIn("nobody is told", line)
        self.assertEqual(
            undo.proposal("manage_note_sharing", {}), undo.Proposal(False, undo.NOT_YET)
        )


class ProjectToolsBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Site",
            code="SIT",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))


class ProjectMembersTellNobody(ProjectToolsBase):
    def members(self, **args):
        return MANAGE_PROJECT_MEMBERS.run(
            {"project_id": self.project.project_id, "user_id": str(self.user2.id), **args}, self.ctx
        )

    def test_adding_a_member_sends_nothing_to_anyone(self):
        with watching_notices() as seen:
            out = self.members(action="add")
        self.assertFalse(out["already_member"])
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.project, attendee=self.user2).exists()
        )
        self.assertEqual(seen, NOBODY)

    def test_changing_a_role_sends_nothing_to_anyone(self):
        self.members(action="add")
        with watching_notices() as seen:
            self.members(action="set_role", role="viewer")
        self.assertEqual(seen, NOBODY)

    def test_so_it_is_filed_as_not_yet(self):
        code, line = undo.UNDO_REASONS["manage_project_members"]
        self.assertEqual(code, undo.NOT_YET)
        self.assertIn("nobody is told", line)


class AssigningATaskTellsTheNewAssignee(ProjectToolsBase):
    """Handing someone an existing task tells them (MIP-1461); taking it off anyone tells nobody."""

    def setUp(self):
        super().setUp()
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="T", status="Open"
        )

    def _assign(self, assignee_id=None):
        args = {"task_id": self.task.task_id}
        if assignee_id:
            args["assignee_id"] = str(assignee_id)
        return ASSIGN_TASK.run(args, self.ctx)

    def test_assigning_tells_the_new_assignee_once_on_the_tasks_card(self):
        post_task_created_card(self.task, actor_id=self.user.id)  # the usual state: it has a card
        with watching_notices() as seen:
            out = self._assign(self.user2.id)
        self.assertEqual(out["assignee_id"], str(self.user2.id))
        self.assertEqual(seen["rows"], {"Activity": 1})
        self.assertEqual(seen["pushes"], {"schedule_push_for_activities": 1})
        self.assertEqual(seen["emails"], 0)

    def test_a_task_with_no_card_is_given_one_whose_mention_is_the_notice(self):
        with watching_notices() as seen:
            self._assign(self.user2.id)
        self.assertEqual(seen["rows"], {"Activity": 1, "Message": 1, "MessageMention": 2})
        self.assertEqual(seen["pushes"], {"schedule_push_for_activities": 1})

    def test_unassigning_sends_nothing_to_anyone(self):
        post_task_created_card(self.task, actor_id=self.user.id)
        self._assign(self.user2.id)
        with watching_notices() as seen:
            self._assign()
        self.assertEqual(seen, NOBODY)

    def test_so_it_is_filed_as_telling_people(self):
        code, line = undo.UNDO_REASONS["assign_task"]
        self.assertEqual(code, undo.NOTIFIED_PEOPLE)
        self.assertIn("is told", line)


class AnsweringAJoinRequestTellsThePerson(JoinToolBase):
    def test_approving_writes_the_person_a_notice_and_pushes_it(self):
        item_id = self.request()
        with watching_notices() as seen:
            self.approve(item_id)
        self.assertEqual(seen["rows"], {"InboxItems": 1})
        self.assertEqual(seen["pushes"], {"schedule_push_for_inbox_item": 1})
        self.assertEqual(self.notices_to(self.attacker).count(), 1)

    def test_declining_writes_the_person_a_notice_and_pushes_it(self):
        item_id = self.request()
        with watching_notices() as seen:
            self.decline(item_id)
        self.assertEqual(seen["rows"], {"InboxItems": 1})
        self.assertEqual(seen["pushes"], {"schedule_push_for_inbox_item": 1})
        self.assertEqual(InboxItems.objects.filter(receiver=self.attacker, item_type=0).count(), 1)

    def test_so_both_are_filed_as_telling_people(self):
        for name in ("approve_join_request", "decline_join_request"):
            self.assertEqual(undo.UNDO_REASONS[name][0], undo.NOTIFIED_PEOPLE, name)


class CalendarGuestsAreInvitedByTheCalendar(ProjectToolsBase):
    def test_the_guests_go_to_google_as_attendees_and_no_email_is_sent_from_here(self):
        sent = {}

        def google(account, method, path, **kwargs):
            sent.update(kwargs)
            reply = mock.Mock(ok=True)
            reply.json.return_value = {
                "id": "e1",
                "summary": "S",
                "attendees": [{"email": "g@x.io"}],
            }
            return reply

        tool = "origin.search_engine.agent.tools.create_calendar_event."
        with (
            mock.patch(tool + "resolve_google_calendar_account"),
            mock.patch(tool + "google_calendar_request", side_effect=google),
            watching_notices() as seen,
        ):
            CREATE_CALENDAR_EVENT.run(
                {
                    "summary": "S",
                    "start_iso": "2026-12-01T10:00:00Z",
                    "end_iso": "2026-12-01T11:00:00Z",
                    "attendees": ["g@x.io"],
                },
                self.ctx,
            )
        self.assertEqual(sent["json"]["attendees"], [{"email": "g@x.io"}])
        self.assertEqual(sent["params"]["sendUpdates"], "none")
        self.assertEqual(seen, NOBODY)
        self.assertEqual(undo.UNDO_REASONS["create_calendar_event"][0], undo.NOTIFIED_PEOPLE)
