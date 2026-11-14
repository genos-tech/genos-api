"""A collaborator who cannot open the task is not told about it, and cannot be added (MIP-1567).

`can_access_task` (project member or owner, assignee, reporter) never counted a collaborator as a
reader, and the product decided it stays so. What was wrong is that a teammate who is not on the
project could still be put on a task as a collaborator (the write paths accepted "on the team or
the project") and was then told about its comments, although the activity feed withholds the task
from them (MIP-1560).

Pinned here:
  * `task_reader_ids` is `can_access_task` in batch form, in a fixed number of queries;
  * the notification fan-outs (task comments, task-body comments) skip a collaborator who cannot
    open the task and keep one who can;
  * leaving the project takes the person off the collaborators of its tasks (not where they are
    the assignee or reporter, not when they are still on it);
  * a milestone converted to a task does not make its outside assignees collaborators;
  * the briefing's reader (`LabelAccess`) reads a collaborator-only task as unreadable.

The write gates themselves are pinned beside their old tests (`test_task_collaborators`,
`test_task_put_people`, `test_milestone_people`, the agent tool tests).
"""

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    Message,
)
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.label_acl import LabelAccess
from origin.services import v3_activity
from origin.services.task_collaborator_revoke import drop_collaborator_on_project_leave
from origin.services.task_collaborators import notifiable_collaborator_ids
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.scope_guards import can_access_task, task_reader_ids

User = get_user_model()


def _user(name):
    return User.objects.create_user(username=name, email=f"{name}@example.com", password="pw12345!")


class ReadRuleBase(BaseAPITestCase):
    """self.user owns the project and is on it; `member` is on it; `teammate` is on the team
    only; `assignee` and `reporter` are team members who hold the task; `stranger` is nobody."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Read Rule",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.member = self.user2
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.member)
        self.teammate = _user("teammate")
        self.assignee = _user("assignee")
        self.reporter = _user("reporter")
        self.stranger = _user("stranger")
        for person in (self.teammate, self.assignee, self.reporter):
            TeamMembers.objects.create(team=self.team, attendee=person)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="T",
            status="Open",
            assignee=self.assignee,
            reporter=self.reporter,
        )

    def readers(self, people):
        t = self.task
        return task_reader_ids(t.project_id, t.assignee_id, t.reporter_id, [p.id for p in people])


class TaskReaderIdsTests(ReadRuleBase):
    def test_it_is_can_access_task_for_every_kind_of_person(self):
        people = [
            self.user,  # project owner and member
            self.member,  # project member
            self.assignee,
            self.reporter,
            self.teammate,  # team only
            self.stranger,  # nobody
        ]
        expected = {str(p.id) for p in people if can_access_task(self.task.task_id, p.id)}
        self.assertEqual(
            expected, {str(p.id) for p in (self.user, self.member, self.assignee, self.reporter)}
        )
        self.assertEqual(self.readers(people), expected)

    def test_the_project_owner_without_a_member_row_reads(self):
        ProjectMembers.objects.filter(attendee=self.user).delete()
        self.assertTrue(can_access_task(self.task.task_id, self.user.id))
        self.assertEqual(self.readers([self.user]), {str(self.user.id)})

    def test_a_task_in_no_project_is_read_by_its_assignee_and_reporter_only(self):
        loose = TaskMaster.objects.create(
            team=self.team, title="L", status="Open", assignee=self.assignee, reporter=self.reporter
        )
        got = task_reader_ids(
            None,
            loose.assignee_id,
            loose.reporter_id,
            [self.member.id, self.assignee.id, self.reporter.id],
        )
        self.assertEqual(got, {str(self.assignee.id), str(self.reporter.id)})

    def test_nobody_asked_means_nothing_read_and_no_query(self):
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(task_reader_ids(self.project.project_id, None, None, []), set())
            self.assertEqual(task_reader_ids(self.project.project_id, None, None, [None, ""]), set())
        self.assertEqual(len(ctx), 0)

    def test_a_malformed_project_id_reads_nothing_instead_of_raising(self):
        self.assertEqual(task_reader_ids("abc", None, None, [self.member.id]), set())

    def test_the_number_of_queries_does_not_grow_with_the_people(self):
        def count(people):
            with CaptureQueriesContext(connection) as ctx:
                self.readers(people)
            return len(ctx)

        extra = [_user(f"extra{i}") for i in range(6)]
        self.assertEqual(count([self.member]), count([self.member, self.teammate, *extra]))
        self.assertLessEqual(count([self.member]), 2)


class NotifiableCollaboratorsTests(ReadRuleBase):
    def test_only_collaborators_who_can_open_the_task_are_notifiable(self):
        # An old row: the teammate was added before the rule held. A stranger likewise.
        self.task.collaborators.set([self.member, self.teammate, self.stranger, self.assignee])
        self.assertEqual(
            notifiable_collaborator_ids(self.task.task_id),
            {str(self.member.id), str(self.assignee.id)},
        )

    def test_a_collaborator_who_left_the_project_is_no_longer_notifiable(self):
        self.task.collaborators.set([self.member])
        ProjectMembers.objects.filter(attendee=self.member).delete()
        # (the leave hook also takes them off; the send-time rule holds without it)
        self.task.collaborators.set([self.member])
        self.assertEqual(notifiable_collaborator_ids(self.task.task_id), set())

    def test_a_missing_task_has_none(self):
        self.assertEqual(notifiable_collaborator_ids(None), set())
        self.assertEqual(notifiable_collaborator_ids(10**9), set())

    def test_the_number_of_queries_does_not_grow_with_the_collaborators(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                notifiable_collaborator_ids(self.task.task_id)
            return len(ctx)

        self.task.collaborators.set([self.member])
        one = count()
        self.task.collaborators.set(
            [self.member, self.teammate, self.stranger, *[_user(f"c{i}") for i in range(5)]]
        )
        self.assertEqual(count(), one)


class TaskCommentFanoutTests(ReadRuleBase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )
        # The commenter is the project owner, who can open the task.
        self.authenticate(self.user)

    def _comment(self):
        return self.client.post(
            "/api/v2/task/comment/",
            {
                "task_id": self.task.task_id,
                "project_id": self.project.project_id,
                "sender_id": str(self.user.id),
                "comment_body": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}
                ],
            },
            format="json",
        )

    def _recipients(self):
        return set(
            Activity.objects.filter(activity_type=ActivityType.THREAD_REPLY).values_list(
                "recipient_id", flat=True
            )
        )

    def test_a_collaborator_who_cannot_open_the_task_is_not_told_of_a_comment(self):
        self.task.collaborators.set([self.member, self.teammate])

        self.assertEqual(self._comment().status_code, 201)

        got = self._recipients()
        self.assertIn(self.member.id, got)  # on the project: told, as before
        self.assertIn(self.assignee.id, got)  # the assignee: told, as before
        self.assertNotIn(self.teammate.id, got)  # cannot open it: told nothing

    def test_an_outsider_who_is_also_the_assignee_is_still_told_as_the_assignee(self):
        # The assignee is not a collaborator-only case: the rule does not touch them.
        self.task.assignee = self.teammate
        self.task.save()
        self.task.collaborators.set([self.teammate])

        self._comment()

        self.assertIn(self.teammate.id, self._recipients())


class TaskBodyCommentFanoutTests(ReadRuleBase):
    def _recipients_of_a_body_comment(self):
        rows = v3_activity.create_surface_comment_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=v3_activity.SURFACE_TASK_BODY,
            entity_key="body-comment-1",
            meta={"taskId": self.task.task_id},
        )
        return {str(a.recipient_id) for a in rows}

    def test_a_collaborator_who_cannot_open_the_task_is_not_told_of_a_body_comment(self):
        self.task.collaborators.set([self.member, self.teammate])

        got = self._recipients_of_a_body_comment()

        self.assertEqual(got, {str(self.assignee.id), str(self.member.id)})

    def test_stakeholders_are_the_assignee_and_the_collaborators_who_can_open_it(self):
        self.task.collaborators.set([self.member, self.teammate, self.stranger])
        self.assertEqual(
            v3_activity._task_stakeholders(self.task.task_id),
            {str(self.assignee.id), str(self.member.id)},
        )


class LeaveProjectTests(ReadRuleBase):
    def test_leaving_the_project_takes_the_person_off_its_tasks_collaborators(self):
        other = TaskMaster.objects.create(
            team=self.team, project=self.project, title="O", status="Open"
        )
        self.task.collaborators.set([self.member, self.user])
        other.collaborators.set([self.member])

        ProjectMembers.objects.get(attendee=self.member, project=self.project).delete()

        self.assertEqual({u.id for u in self.task.collaborators.all()}, {self.user.id})
        self.assertEqual(other.collaborators.count(), 0)

    def test_it_takes_nothing_off_other_projects_tasks(self):
        elsewhere = ProjectMaster.objects.create(
            team=self.team, project_name="Else", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=elsewhere, attendee=self.member)
        theirs = TaskMaster.objects.create(
            team=self.team, project=elsewhere, title="E", status="Open"
        )
        theirs.collaborators.set([self.member])

        ProjectMembers.objects.get(attendee=self.member, project=self.project).delete()

        self.assertEqual({u.id for u in theirs.collaborators.all()}, {self.member.id})

    def test_where_they_hold_the_task_they_stay_listed(self):
        mine = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="M",
            status="Open",
            assignee=self.member,
            reporter=self.user,
        )
        reported = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="R",
            status="Open",
            assignee=self.user,
            reporter=self.member,
        )
        mine.collaborators.set([self.member])
        reported.collaborators.set([self.member])

        ProjectMembers.objects.get(attendee=self.member, project=self.project).delete()

        self.assertEqual(mine.collaborators.count(), 1)
        self.assertEqual(reported.collaborators.count(), 1)

    def test_a_person_still_on_the_project_loses_nothing(self):
        self.task.collaborators.set([self.member])
        # e.g. a duplicate or a stale row went; the person is still a member.
        self.assertEqual(
            drop_collaborator_on_project_leave(
                user_id=self.member.id, project_id=self.project.project_id
            ),
            0,
        )
        self.assertEqual(self.task.collaborators.count(), 1)

    def test_a_missing_id_does_nothing(self):
        self.assertEqual(drop_collaborator_on_project_leave(user_id=None, project_id=None), 0)
        self.assertEqual(
            drop_collaborator_on_project_leave(user_id=self.member.id, project_id=None), 0
        )

    def test_a_team_departure_reaches_it_through_the_project_rows(self):
        from origin.services.team_membership import drop_in_team_access

        self.task.collaborators.set([self.member])

        drop_in_team_access(self.team.team_id, self.member.id)

        self.assertEqual(self.task.collaborators.count(), 0)


class BriefingReaderTests(ReadRuleBase):
    def test_a_collaborator_only_task_is_not_readable_for_the_briefing(self):
        """The briefing, the timeline and the per-day lines list a task the member holds by role
        (collaborators included) only through `LabelAccess`, which never counted a collaborator
        as a reader: the title of a task they cannot open is not handed to them."""
        self.task.collaborators.set([self.teammate])
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.teammate.id))

        self.assertEqual(LabelAccess(ctx).tasks([self.task.task_id]), {})
        self.assertFalse(can_access_task(self.task.task_id, self.teammate.id))
