"""The rest of the collaborator read rule (MIP-1579, follow-ups of MIP-1567).

MIP-1567 stopped a collaborator who cannot open a task from being added or notified. What it left:

  * collaborators who were listed BEFORE the rule: `prune_unreadable_task_collaborators` removes
    them (dry run first);
  * a task moved to another project keeps collaborators who are not on the destination: the move
    prunes them (every path runs `relocate_task_satellites`);
  * the task-comment fan-out reached a prior commenter who left the project: the participants
    (assignee, collaborators, prior commenters) now pass `task_reader_ids`. A direct @mention is
    kept as it was: it reaches a teammate who is not on the project on purpose;
  * `can_access_task` (REST) and `task_acl_user_ids` (search) are two rules: the exact
    difference is pinned here.
"""

import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
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
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.services import v3_activity
from origin.services.task_collaborator_revoke import (
    drop_unreadable_collaborators,
    unreadable_collaborator_rows,
)
from origin.services.task_comment_fanout import _participant_ids
from origin.services.task_reparent import move_task_to_project
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.scope_guards import can_access_task

User = get_user_model()


def _user(name):
    return User.objects.create_user(username=name, email=f"{name}@example.com", password="pw12345!")


class Base(BaseAPITestCase):
    """self.user owns `project` and is on it; `member` is on it; `teammate` is on the team only;
    `assignee` / `reporter` hold the task (team members, not on the project); `stranger` is
    nobody. `other` is a second project of the team, self.user and `member` are on it."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.project = self._project("Source")
        self.other = self._project("Destination")
        self.member = self.user2
        for project in (self.project, self.other):
            ProjectMembers.objects.create(team=self.team, project=project, attendee=self.member)
        self.teammate = _user("teammate")
        self.assignee = _user("assignee")
        self.reporter = _user("reporter")
        self.stranger = _user("stranger")
        self.leaver = _user("leaver")
        for person in (self.teammate, self.assignee, self.reporter, self.leaver):
            TeamMembers.objects.create(team=self.team, attendee=person)
        self.task = self._task(self.project)

    def _project(self, name):
        project = ProjectMaster.objects.create(
            team=self.team, project_name=name, owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        return project

    def _task(self, project, **kwargs):
        return TaskMaster.objects.create(
            team=self.team,
            project=project,
            title=kwargs.pop("title", "T"),
            status="Open",
            assignee=kwargs.pop("assignee", self.assignee),
            reporter=kwargs.pop("reporter", self.reporter),
            **kwargs,
        )

    def collaborators(self, task=None):
        return {u.id for u in (task or self.task).collaborators.all()}


class UnreadableRowsTests(Base):
    def test_it_is_the_read_rule_for_every_kind_of_person(self):
        people = [
            self.user,  # project owner and member
            self.member,  # project member
            self.assignee,
            self.reporter,
            self.teammate,  # team only
            self.stranger,  # nobody
        ]
        self.task.collaborators.set(people)
        expected_gone = {p.id for p in people if not can_access_task(self.task.task_id, p.id)}
        self.assertEqual(expected_gone, {self.teammate.id, self.stranger.id})

        rows = unreadable_collaborator_rows()

        through = TaskMaster.collaborators.through.objects
        gone = {through.get(pk=pk).customuser_id for pk, _task in rows}
        self.assertEqual(gone, expected_gone)

    def test_the_project_owner_without_a_member_row_is_a_reader(self):
        ProjectMembers.objects.filter(attendee=self.user, project=self.project).delete()
        self.task.collaborators.set([self.user])
        self.assertEqual(unreadable_collaborator_rows(), [])

    def test_a_task_with_no_project_keeps_its_assignee_and_reporter_only(self):
        loose = TaskMaster.objects.create(
            team=self.team, title="L", status="Open", assignee=self.assignee, reporter=self.reporter
        )
        loose.collaborators.set([self.member, self.assignee, self.reporter])
        rows = unreadable_collaborator_rows()
        through = TaskMaster.collaborators.through.objects
        self.assertEqual({through.get(pk=pk).customuser_id for pk, _t in rows}, {self.member.id})

    def test_the_queries_do_not_grow_with_the_rows(self):
        self.task.collaborators.set([self.teammate])
        with CaptureQueriesContext(connection) as one:
            unreadable_collaborator_rows()
        extra = [_user(f"x{i}") for i in range(6)]
        for i in range(4):
            self._task(self.project, title=f"t{i}").collaborators.set([self.teammate, *extra])
        with CaptureQueriesContext(connection) as many:
            unreadable_collaborator_rows()
        self.assertEqual(len(one), len(many))

    def test_it_can_be_limited_to_some_tasks(self):
        other_task = self._task(self.project, title="O")
        self.task.collaborators.set([self.teammate])
        other_task.collaborators.set([self.teammate])
        self.assertEqual(drop_unreadable_collaborators([other_task.task_id]), 1)
        self.assertEqual(self.collaborators(), {self.teammate.id})
        self.assertEqual(self.collaborators(other_task), set())


class PruneCommandTests(Base):
    def run_command(self, *args):
        out = StringIO()
        call_command("prune_unreadable_task_collaborators", *args, stdout=out)
        return out.getvalue()

    def setUp(self):
        super().setUp()
        self.task.collaborators.set([self.member, self.teammate, self.stranger, self.assignee])

    def test_a_dry_run_writes_nothing_and_says_how_many(self):
        before = list(TaskMaster.collaborators.through.objects.order_by("pk").values_list())

        out = self.run_command()

        self.assertEqual(
            before, list(TaskMaster.collaborators.through.objects.order_by("pk").values_list())
        )
        self.assertIn("DRY RUN", out)
        self.assertIn("would remove: 2", out)
        self.assertIn("--apply", out)

    def test_apply_removes_exactly_the_people_who_cannot_open_the_task(self):
        out = self.run_command("--apply")

        self.assertIn("removed: 2", out)
        self.assertEqual(self.collaborators(), {self.member.id, self.assignee.id})

    def test_apply_twice_finds_nothing_the_second_time(self):
        self.run_command("--apply")
        out = self.run_command("--apply")

        self.assertIn("removed: 0", out)
        self.assertEqual(self.collaborators(), {self.member.id, self.assignee.id})

    def test_the_output_names_nobody(self):
        out = self.run_command() + self.run_command("--apply")
        for secret in (
            "teammate",
            "stranger",
            "assignee",
            "example.com",
            self.task.title,
            str(self.teammate.id),
        ):
            self.assertNotIn(secret, out)

    def test_it_touches_no_other_row(self):
        self.run_command("--apply")
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.assignee.id)
        self.assertEqual(TaskMaster.objects.count(), 1)


class MoveTests(Base):
    """A task that changes project keeps only the collaborators who can open it there."""

    def setUp(self):
        super().setUp()
        # `member` is on both projects, `leaver` only on the source.
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.leaver)
        self.task.collaborators.set([self.member, self.leaver, self.assignee])

    def _put(self, **body):
        self.authenticate(self.user)
        return self.client.put(
            "/api/v2/task/", {"task_id": self.task.task_id, **body}, format="json"
        )

    def test_the_put_that_moves_a_task_drops_collaborators_not_on_the_destination(self):
        res = self._put(project=self.other.project_id)

        self.assertEqual(res.status_code, 200, res.data)
        # the member is on both; the assignee holds the task; the leaver is on neither side's rule
        self.assertEqual(self.collaborators(), {self.member.id, self.assignee.id})

    def test_a_put_that_does_not_move_it_prunes_nothing(self):
        res = self._put(title="renamed")

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.collaborators(), {self.member.id, self.leaver.id, self.assignee.id})

    def test_the_agent_move_prunes_too(self):
        move_task_to_project(self.task, self.other.project_id)

        self.assertEqual(self.collaborators(), {self.member.id, self.assignee.id})

    def test_the_sub_tasks_that_ride_along_are_pruned_by_their_own_holders(self):
        child = self._task(
            self.project, title="child", parent_task_id=self.task.task_id, assignee=self.leaver
        )
        child.collaborators.set([self.member, self.leaver, self.teammate])
        self.assertEqual(self._put(project=self.other.project_id).status_code, 200)

        # the leaver holds the child, so it stays on it; the teammate is on neither
        self.assertEqual(self.collaborators(child), {self.member.id, self.leaver.id})

    def test_a_task_in_another_project_is_left_alone(self):
        bystander = self._task(self.project, title="B")
        bystander.collaborators.set([self.leaver])

        self.assertEqual(self._put(project=self.other.project_id).status_code, 200)

        self.assertEqual(self.collaborators(bystander), {self.leaver.id})


class CommentFanoutTests(Base):
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
        self.authenticate(self.user)

    def _prior_comment(self, sender):
        number = TaskComments.objects.filter(task=self.task).count() + 1
        return TaskComments.objects.create(
            task=self.task,
            sender=sender,
            comment_id=number,
            comment_body=[{"type": "paragraph", "content": []}],
        )

    def _comment(self, *mention_ids):
        content = [{"type": "text", "text": "hello"}]
        for uid in mention_ids:
            content.append({"type": "mention", "props": {"userId": str(uid)}})
        return self.client.post(
            "/api/v2/task/comment/",
            {
                "task_id": self.task.task_id,
                "project_id": self.project.project_id,
                "sender_id": str(self.user.id),
                "comment_body": [{"type": "paragraph", "content": content}],
            },
            format="json",
        )

    def _recipients(self, kind):
        return set(
            Activity.objects.filter(activity_type=kind).values_list("recipient_id", flat=True)
        )

    def test_a_prior_commenter_who_left_the_project_is_not_told(self):
        self._prior_comment(self.leaver)  # an ex-member: team only now
        self._prior_comment(self.member)

        self.assertEqual(self._comment().status_code, 201)

        got = self._recipients(ActivityType.THREAD_REPLY)
        self.assertIn(self.member.id, got)
        self.assertIn(self.assignee.id, got)
        self.assertNotIn(self.leaver.id, got)

    def test_a_prior_commenter_who_holds_the_task_is_still_told(self):
        self._prior_comment(self.reporter)  # not on the project, but the reporter

        self._comment()

        self.assertIn(self.reporter.id, self._recipients(ActivityType.THREAD_REPLY))

    def test_participants_are_the_assignee_and_those_who_can_open_the_task(self):
        self.task.collaborators.set([self.member, self.teammate])
        self._prior_comment(self.leaver)
        self._prior_comment(self.member)
        self._prior_comment(self.reporter)

        self.assertEqual(
            _participant_ids(self.task.task_id, set()),
            {str(self.assignee.id), str(self.member.id), str(self.reporter.id)},
        )

    def test_the_participant_queries_do_not_grow_with_the_people(self):
        def count():
            with CaptureQueriesContext(connection) as ctx:
                _participant_ids(self.task.task_id, set())
            return len(ctx)

        self.task.collaborators.set([self.member])
        one = count()
        self.task.collaborators.set([self.member, self.teammate])
        for i in range(5):
            self._prior_comment(_user(f"c{i}"))
        self.assertEqual(count(), one)

    def test_a_direct_mention_still_reaches_a_teammate_who_is_not_on_the_project(self):
        # Kept on purpose (`test_direct_mention_recipients`): a mention is the sender's explicit
        # act, and the non-member-mention report tells them to add the person.
        self.assertEqual(self._comment(self.teammate.id, self.member.id).status_code, 201)

        got = self._recipients(ActivityType.MENTION)
        self.assertIn(self.member.id, got)
        self.assertIn(self.teammate.id, got)

    def test_the_non_member_mention_report_still_names_the_unreachable_person(self):
        res = self._comment(self.teammate.id)

        self.assertEqual(res.status_code, 201)
        self.assertIn(str(self.teammate.id), json.dumps(res.data["nonMemberMentions"], default=str))

    def test_a_body_comment_reaches_the_assignee_and_readable_collaborators_only(self):
        # `_task_stakeholders` already complied (MIP-1567): pinned, since the prior commenters
        # of the legacy comment feature are deliberately not part of it.
        self.task.collaborators.set([self.member, self.teammate])
        self._prior_comment(self.leaver)
        self.assertEqual(
            v3_activity._task_stakeholders(self.task.task_id),
            {str(self.assignee.id), str(self.member.id)},
        )


class TwoRulesTests(Base):
    """`can_access_task` (REST: open and edit) and `task_acl_user_ids` (search ACL, the agent's
    tools) are two rules that differ in ONE way, pinned here. The search rule is never wider than
    the REST one; where it is narrower, that is on purpose:

      * a person who LEFT THE TEAM (an assignee, a reporter, or the project's owner) can still
        open the task but is not on the search ACL: a removed member stops finding what they
        were on (MIP-1371). MIP-1592 makes REST follow search here.

    The other difference this class used to pin, the project OWNER with no `ProjectMembers`
    row, is gone (MIP-1591): the owner is on the search ACL, as on the REST rule.
    """

    def acl(self, task=None):
        t = task or self.task
        return task_acl_user_ids(t.project_id, t.assignee_id, t.reporter_id)

    def rest(self, people, task=None):
        t = task or self.task
        return {str(p.id) for p in people if can_access_task(t.task_id, p.id)}

    def everyone(self):
        return [self.user, self.member, self.teammate, self.assignee, self.reporter, self.stranger]

    def test_they_agree_for_every_ordinary_person(self):
        self.assertEqual(self.rest(self.everyone()), self.acl())

    def test_search_is_never_wider_than_rest(self):
        ProjectMembers.objects.filter(attendee=self.user, project=self.project).delete()
        TeamMembers.objects.filter(attendee=self.assignee).update(is_deleted=True)
        people = self.everyone()
        self.assertLessEqual(self.acl() & {str(p.id) for p in people}, self.rest(people))

    def test_a_project_owner_without_a_member_row_is_on_both_rules(self):
        ProjectMembers.objects.filter(attendee=self.user, project=self.project).delete()
        self.assertTrue(can_access_task(self.task.task_id, self.user.id))
        self.assertIn(str(self.user.id), self.acl())  # MIP-1591: no longer a difference

    def test_a_project_owner_who_left_the_team_is_still_a_difference(self):
        # The owner's member row goes with the team membership (`drop_in_team_access`); the
        # project keeps its `owner`. REST still opens the task; search, rightly, does not.
        owner = _user("projowner")
        TeamMembers.objects.create(team=self.team, attendee=owner)
        project = self._project("Theirs")
        ProjectMaster.objects.filter(pk=project.pk).update(owner=owner)
        task = self._task(project)
        self.assertIn(str(owner.id), self.acl(task))
        TeamMembers.objects.filter(attendee=owner).update(is_deleted=True)
        self.assertTrue(can_access_task(task.task_id, owner.id))
        self.assertNotIn(str(owner.id), self.acl(task))

    def test_an_assignee_who_left_the_team_is_the_difference(self):
        TeamMembers.objects.filter(attendee=self.assignee).update(is_deleted=True)
        self.assertTrue(can_access_task(self.task.task_id, self.assignee.id))
        self.assertNotIn(str(self.assignee.id), self.acl())
        self.assertIn(str(self.reporter.id), self.acl())  # the other holder is unaffected
