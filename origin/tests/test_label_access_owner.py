"""The project's OWNER with no member row reads its tasks and milestones through the briefing,
the timeline, `get_item_line` and `list_my_unread` too (MIP-1609, item 1 of the leftovers of
MIP-1591).

`can_access_task` (REST) and, since #880, the search ACL (`acl.task_acl_user_ids`,
`milestone_acl_user_ids`, the two chunkers) let the owner in with no `ProjectMembers` row. The read
rule those four surfaces apply is stated twice, `LabelAccess.task_readable` (and
`milestone_readable`) in Python and `item_lines.readable_task_q` in SQL, and neither had the owner:
such an owner was refused the task (`get_item_line`), saw it dropped from `list_my_unread` and
named by its raw id on an approval card. It fails closed, so nobody saw too much; the owner saw too
little. Both copies now have the owner, ONLY while the owner is still on the team, like
`acl.active_project_owners` (an owner who left the team has their member row dropped and stays
`ProjectMaster.owner`: REST lets them in, search does not, and neither does this).

The milestone's REPORTER was on the index (`milestone_chunker`) and on neither the fetch-time
`milestone_acl_user_ids` nor `milestone_readable`, and was never narrowed to the team. The reporter
rides on the milestone's backing task, where they are a reader like any task's reporter, so the
three now agree: the reporter counts while they are on the team.
"""

from __future__ import annotations

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType, Channel, ChannelKind, Message
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.acl import milestone_acl_user_ids
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolForbidden
from origin.search_engine.chunkers.milestone_chunker import iter_milestone_chunks
from origin.services import item_lines, unread_digest
from origin.tests.test_item_lines import LineBase

User = get_user_model()


class OwnerBase(LineBase):
    """`powner` owns `owned` (a team member, NOT the team's owner) and has no member row;
    `self.user2` is on it. `ex` owns `gone`'s project and has left the team."""

    def person(self, name, *, on_team=True):
        user = User.objects.create_user(
            username=name, email=f"{name}@m1609.test", password="pw12345!"
        )
        if on_team:
            TeamMembers.objects.create(team=self.team, attendee=user)
        return user

    def setUp(self):
        super().setUp()
        self.powner = self.person("powner")
        self.owned = ProjectMaster.objects.create(
            team=self.team, project_name="Owned", owner=self.powner, project_system_user=self.powner
        )
        ProjectMembers.objects.create(team=self.team, project=self.owned, attendee=self.user2)
        self.t_owned = self.make_task(
            "In the owned project", project=self.owned, assignee=self.user2, reporter=self.user2
        )

    def ctx_of(self, user):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))

    def leave_team(self, user):
        TeamMembers.objects.filter(team=self.team, attendee=user).update(is_deleted=True)

    def python_rule(self, user, *tasks):
        return {t.task_id for t in tasks if LabelAccess(self.ctx_of(user)).task_readable(t)}

    def sql_rule(self, user, *tasks):
        return set(
            TaskMaster.objects.filter(task_id__in=[t.task_id for t in tasks])
            .filter(item_lines.readable_task_q(self.ctx_of(user), ""))
            .values_list("task_id", flat=True)
        )


class TheTaskRuleInPythonAndInSql(OwnerBase):
    def test_the_owner_with_no_member_row_reads_the_task_in_both_copies(self):
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.owned, attendee=self.powner).exists()
        )
        self.assertEqual(self.python_rule(self.powner, self.t_owned), {self.t_owned.task_id})
        self.assertEqual(self.sql_rule(self.powner, self.t_owned), {self.t_owned.task_id})
        self.assertEqual(
            LabelAccess(self.ctx_of(self.powner)).task_display_id(self.t_owned.task_id),
            self.t_owned.display_id,
        )

    def test_an_owner_who_left_the_team_reads_it_in_neither(self):
        self.leave_team(self.powner)
        self.assertEqual(self.python_rule(self.powner, self.t_owned), set())
        self.assertEqual(self.sql_rule(self.powner, self.t_owned), set())

    def test_the_teams_own_owner_counts_without_a_team_row(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user).delete()
        mine = ProjectMaster.objects.create(
            team=self.team, project_name="Mine", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.filter(project=mine, attendee=self.user).delete()
        task = self.make_task("t", project=mine, assignee=self.user2, reporter=self.user2)
        self.assertEqual(self.python_rule(self.user, task), {task.task_id})
        self.assertEqual(self.sql_rule(self.user, task), {task.task_id})

    def test_a_teammate_who_owns_nothing_here_reads_nothing(self):
        mate = self.person("mate")
        self.assertEqual(self.python_rule(mate, self.t_owned), set())
        self.assertEqual(self.sql_rule(mate, self.t_owned), set())

    def test_a_project_with_no_owner_gives_nobody_the_owner_rule(self):
        ProjectMaster.objects.filter(pk=self.owned.pk).update(owner=None)
        self.assertEqual(self.python_rule(self.powner, self.t_owned), set())
        self.assertEqual(self.sql_rule(self.powner, self.t_owned), set())

    def test_the_run_team_must_be_the_tasks_team_for_the_owner_too(self):
        other = TeamMaster.objects.create(
            team_name="Else", team_email="else@m1609.test", owner=self.powner
        )
        ctx = ToolContext(team_id=str(other.team_id), user_id=str(self.powner.id))
        self.assertFalse(LabelAccess(ctx).task_readable(self.t_owned))
        self.assertFalse(
            TaskMaster.objects.filter(task_id=self.t_owned.task_id)
            .filter(item_lines.readable_task_q(ctx, ""))
            .exists()
        )

    def test_a_deleted_task_is_not_read_by_the_owner_either(self):
        TaskMaster.objects.filter(pk=self.t_owned.pk).update(is_deleted=True)
        self.t_owned.refresh_from_db()
        self.assertEqual(self.python_rule(self.powner, self.t_owned), set())
        self.assertEqual(self.sql_rule(self.powner, self.t_owned), set())

    def test_the_two_copies_agree_for_every_person_and_task_and_through_a_prefix(self):
        """The pin of `test_item_lines`, widened to the owner: every kind of person over every
        kind of task, in `readable_task_q("")` and `readable_task_q("task__")`."""
        ex = self.person("exowner")
        gone_project = ProjectMaster.objects.create(
            team=self.team, project_name="Gone", owner=ex, project_system_user=ex
        )
        self.leave_team(ex)
        in_gone = self.make_task(
            "g", project=gone_project, assignee=self.user2, reporter=self.user2
        )
        loose = self.make_task("loose", assignee=self.user2, reporter=self.user2)
        TaskMaster.objects.filter(pk=loose.pk).update(project=None)
        loose.refresh_from_db()
        reported = self.make_task(
            "reported", project=self.owned, assignee=self.user2, reporter=self.outsider
        )
        tasks = [self.t_owned, in_gone, loose, reported]
        for t in tasks:
            TaskComments.objects.create(task=t, sender=self.user2, comment_id=1, comment_body=[])
        people = [self.powner, ex, self.user, self.user2, self.outsider, self.person("mate")]
        for user in people:
            with self.subTest(user=user.username):
                python = self.python_rule(user, *tasks)
                self.assertEqual(self.sql_rule(user, *tasks), python)
                via_prefix = set(
                    TaskComments.objects.filter(task__in=tasks)
                    .filter(item_lines.readable_task_q(self.ctx_of(user), "task__"))
                    .values_list("task_id", flat=True)
                )
                self.assertEqual(via_prefix, python)
        self.assertEqual(
            self.python_rule(self.powner, *tasks), {self.t_owned.task_id, reported.task_id}
        )
        self.assertEqual(self.python_rule(ex, *tasks), set())


class WhoSeesWhatDoesNotDependOnTheOwnerRow(OwnerBase):
    """With and without the owner's `ProjectMembers` row, the owner is shown the same bytes."""

    def setUp(self):
        super().setUp()
        self.now = timezone.now() + timedelta(minutes=1)
        said = self.comment(self.t_owned, self.user2, 10)
        channel = Channel.objects.get(project_id=self.owned.project_id, kind=ChannelKind.PM)
        card = Message.objects.create(
            channel=channel,
            sender=self.user2,
            seq=4,
            body=[],
            body_text="card",
            task_id=self.t_owned.task_id,
        )
        self.mirror = Message.objects.create(
            channel=channel,
            sender=self.user2,
            seq=5,
            body=[],
            body_text="a comment",
            task_id=self.t_owned.task_id,
            metadata={"taskCommentId": said.comment_id},
            thread_root=card,
            is_thread_reply=True,
        )
        Activity.objects.create(
            team=self.team,
            recipient=self.powner,
            actor=self.user2,
            activity_type=ActivityType.THREAD_REPLY,
            channel=self.mirror.channel,
            message=self.mirror,
            meta={},
        )

    now = None

    def shown(self):
        ctx = self.ctx_of(self.powner)
        now = self.now
        line = item_lines.item_lines(ctx, self.t_owned.task_id, self.since, self.until)
        unread = unread_digest.build_unread(ctx, now=now)
        return json.dumps({"line": line, "unread": unread}, sort_keys=True, default=str)

    def test_the_line_and_the_unread_list_are_the_same_with_and_without_the_row(self):
        without = self.shown()
        ProjectMembers.objects.create(team=self.team, project=self.owned, attendee=self.powner)
        with_row = self.shown()
        self.assertEqual(without, with_row)
        # and it is not an empty answer that agrees with itself
        self.assertIn(self.t_owned.display_id, without)
        self.assertEqual(len(json.loads(without)["unread"]["items"]), 1)

    def test_an_owner_who_left_the_team_is_shown_nothing_in_both(self):
        self.leave_team(self.powner)
        unread = unread_digest.build_unread(self.ctx_of(self.powner), now=self.now)
        self.assertEqual(unread["items"], [])

    def test_a_teammate_on_neither_is_shown_nothing(self):
        mate = self.person("mate")
        with self.assertRaises(ToolForbidden):
            item_lines.line_for_item(
                self.ctx_of(mate), self.t_owned.task_id, self.since, self.until
            )


class TheMilestoneRule(OwnerBase):
    """`milestone_readable`, `milestone_acl_user_ids` and the index say the same."""

    def setUp(self):
        super().setUp()
        self.rep = self.person("rep")  # reporter, on the team, not on the project
        self.ex_rep = self.person("exrep")  # reporter who left the team
        self.assignee = self.person("assignee")
        self.mate = self.person("mate")
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.owned, title="Q4 launch", reporter=self.rep
        )
        MilestoneAssignees.objects.create(
            team=self.team, milestone=self.milestone, user=self.assignee
        )
        self.leave_team(self.ex_rep)

    def readable(self, user, milestone=None):
        return LabelAccess(self.ctx_of(user)).milestone_readable(milestone or self.milestone)

    def indexed(self, milestone=None):
        milestone = milestone or self.milestone
        for entity in iter_milestone_chunks():
            if entity.entity_id == f"milestone:{milestone.milestone_id}":
                return set(entity.chunks[0].acl_user_ids)
        self.fail("milestone not chunked")

    def fetched(self, milestone=None):
        milestone = milestone or self.milestone
        return milestone_acl_user_ids(milestone.project_id, milestone.milestone_id)

    def test_the_owner_with_no_member_row_reads_the_milestone(self):
        self.assertTrue(self.readable(self.powner))

    def test_an_owner_who_left_the_team_does_not(self):
        self.leave_team(self.powner)
        self.assertFalse(self.readable(self.powner))

    def test_the_reporter_reads_it_while_on_the_team_and_not_after(self):
        self.assertTrue(self.readable(self.rep))
        self.leave_team(self.rep)
        self.assertFalse(self.readable(self.rep))

    def test_the_three_statements_of_the_rule_agree_for_everyone(self):
        ex = MilestoneMaster.objects.create(
            team=self.team, project=self.owned, title="Old", reporter=self.ex_rep
        )
        people = [self.powner, self.user2, self.rep, self.ex_rep, self.assignee, self.mate]
        for milestone in (self.milestone, ex):
            fetched, indexed = self.fetched(milestone), self.indexed(milestone)
            for user in people:
                with self.subTest(user=user.username, milestone=milestone.title):
                    mine = self.readable(user, milestone)
                    self.assertEqual(str(user.id) in fetched, mine)
                    self.assertEqual(str(user.id) in indexed, mine)
        self.assertEqual(
            self.fetched(self.milestone),
            {str(u.id) for u in (self.powner, self.user2, self.rep, self.assignee)},
        )
        self.assertEqual(self.fetched(ex), {str(u.id) for u in (self.powner, self.user2)})

    def test_a_milestone_with_no_reporter_and_no_project_is_unchanged(self):
        MilestoneMaster.objects.filter(pk=self.milestone.pk).update(reporter=None)
        self.milestone.refresh_from_db()
        self.assertEqual(
            self.fetched(),
            {str(u.id) for u in (self.powner, self.user2, self.assignee)},
        )
        self.assertFalse(self.readable(self.rep))

    def test_nobody_else_gains(self):
        self.assertFalse(self.readable(self.mate))
        self.assertNotIn(str(self.mate.id), self.fetched())
        self.assertNotIn(str(self.mate.id), self.indexed())
