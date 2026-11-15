"""A person who LEFT the team stops opening tasks as the assignee or reporter (MIP-1592).

`can_access_task` let the assignee and the reporter in with no look at the team, so someone
removed from the team (or a deleted account) still opened, read and edited the task by its id,
while search had dropped them (MIP-1371). The rule is now:

    assignee or reporter  AND  (on the project (member row or owner)  OR  still on the task's team)

"still on the team" is `member_removal.active_team_member_ids`: a live `TeamMembers` row, or the
team's owner. The project path is a separate OR, so nobody who legitimately works on the task
loses it: guests and the participants of a shared project (a `ProjectMembers` row and no team
row), the team owner (no rows at all), the system user and the demo users (both hold team rows).

Pinned here, for every place the rule is stated (`can_access_task`, `task_reader_ids`, the feed's
`_readable_tasks` and the live card's `task_reader_pairs`, the collaborator prune's `unreadable_collaborator_rows`, the notification
fan-outs built on `task_readers_among`):

  * who keeps access and who loses it, per kind of person;
  * the refusal is the answer of a missing task (no oracle), end to end over REST;
  * a live task still costs one query for a current holder (the active test rides on the
    task query), and the batch forms never ask per person.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import account_deletion, v3_activity
from origin.services.activity_minimal_card import task_reader_pairs
from origin.services.activity_surface_access import unreadable_surface_activity_ids
from origin.services.task_collaborator_revoke import unreadable_collaborator_rows
from origin.services.task_collaborators import notifiable_collaborator_ids, task_readers_among
from origin.services.task_comment_fanout import _participant_ids
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.scope_guards import can_access_task, task_reader_ids

User = get_user_model()

MISSING = 987_654_321


def _user(name, **extra):
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", password="pw12345!", **extra
    )


class Base(BaseAPITestCase):
    """self.user owns the team and the project and is on both; `member` (user2) is on both;
    `holder` is a team member who is not on the project. Tasks are made per test."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Leavers", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.member = self.user2
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.member)
        self.holder = _user("holder")
        TeamMembers.objects.create(team=self.team, attendee=self.holder)

    def task(self, *, assignee=None, reporter=None, project="default", team="default", **extra):
        return TaskMaster.objects.create(
            team=self.team if team == "default" else team,
            project=self.project if project == "default" else project,
            title=extra.pop("title", "T"),
            status="Open",
            assignee=assignee,
            reporter=reporter,
            **extra,
        )

    def leave_team(self, person, team=None):
        """What a removal leaves: the team row soft-deleted, the project rows gone."""
        TeamMembers.objects.filter(team=team or self.team, attendee=person).update(is_deleted=True)
        ProjectMembers.objects.filter(attendee=person).delete()

    # The rule, asked in every shape the code asks it. All must agree.
    def opens(self, task, person):
        """`{rule name: bool}` for one person and one task."""
        t = TaskMaster.objects.get(task_id=task.task_id)
        pid = str(person.id)
        through = TaskMaster.collaborators.through.objects
        # The collaborator prune: put the person on the task and see whether the row would go.
        t.collaborators.add(person)
        row_pks = [pk for pk, _ in unreadable_collaborator_rows([t.task_id])]
        listed = through.filter(taskmaster_id=t.task_id, customuser_id=person.id).first()
        pruned = listed is not None and listed.pk in row_pks
        t.collaborators.remove(person)
        return {
            "can_access_task": can_access_task(t.task_id, person.id),
            "task_reader_ids": pid
            in task_reader_ids(
                t.project_id, t.assignee_id, t.reporter_id, [person.id], team_id=t.team_id
            ),
            "task_readers_among": pid in task_readers_among(t.task_id, [person.id]),
            "minimal card": (pid, t.task_id) in task_reader_pairs([person.id], [t.task_id]),
            "feed": not unreadable_surface_activity_ids(
                person.id, [(1, v3_activity.SURFACE_TASK_BODY, {"taskId": t.task_id})]
            ),
            "prune keeps the collaborator row": not pruned,
        }

    def assertOpens(self, task, person, expected=True):
        got = self.opens(task, person)
        self.assertEqual(
            got, {name: expected for name in got}, f"expected {expected} everywhere, got {got}"
        )


class HoldersWhoLeftTests(Base):
    def test_an_assignee_who_left_the_team_is_refused_everywhere(self):
        task = self.task(assignee=self.holder, reporter=self.user)
        self.assertOpens(task, self.holder)  # before: a team member holding the task
        self.leave_team(self.holder)
        self.assertOpens(task, self.holder, expected=False)

    def test_a_reporter_who_left_the_team_is_refused_everywhere(self):
        task = self.task(assignee=self.user, reporter=self.holder)
        self.leave_team(self.holder)
        self.assertOpens(task, self.holder, expected=False)

    def test_the_other_holder_and_the_members_are_unaffected(self):
        other = _user("bystander")
        TeamMembers.objects.create(team=self.team, attendee=other)
        task = self.task(assignee=self.holder, reporter=other)
        self.leave_team(self.holder)
        self.assertOpens(task, other)
        self.assertOpens(task, self.member)
        self.assertOpens(task, self.user)
        self.assertOpens(task, self.holder, expected=False)

    def test_a_stranger_is_still_refused(self):
        self.assertOpens(self.task(assignee=self.holder), _user("stranger"), expected=False)

    def test_coming_back_to_the_team_gives_the_task_back(self):
        task = self.task(assignee=self.holder)
        self.leave_team(self.holder)
        self.assertOpens(task, self.holder, expected=False)
        TeamMembers.objects.filter(team=self.team, attendee=self.holder).update(is_deleted=False)
        self.assertOpens(task, self.holder)

    def test_someone_who_never_had_a_team_row_is_refused_as_a_holder(self):
        outsider = _user("outsider")  # no team row at all, no project row
        self.assertOpens(self.task(assignee=outsider, reporter=outsider), outsider, expected=False)

    def test_leaving_another_team_does_not_matter(self):
        # The question is the TASK'S team: a live row elsewhere is not one here.
        elsewhere = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.holder
        )
        TeamMembers.objects.create(team=elsewhere, attendee=self.holder)
        self.leave_team(self.holder)
        self.assertOpens(self.task(assignee=self.holder), self.holder, expected=False)

    def test_a_soft_deleted_task_is_not_opened_by_a_holder_either(self):
        task = self.task(assignee=self.holder)
        TaskMaster.objects.filter(task_id=task.task_id).update(is_deleted=True)
        self.assertFalse(can_access_task(task.task_id, self.holder.id))
        self.assertTrue(can_access_task(task.task_id, self.holder.id, include_deleted=True))
        self.leave_team(self.holder)
        # The same rule on the include_deleted path (the task-to-milestone conversion).
        self.assertFalse(can_access_task(task.task_id, self.holder.id, include_deleted=True))

    def test_a_malformed_id_still_answers_false(self):
        self.assertFalse(can_access_task("not-a-number", self.holder.id))
        self.assertFalse(can_access_task(self.task(assignee=self.holder).task_id, "not-a-uuid"))
        self.assertFalse(can_access_task(None, self.holder.id))
        self.assertFalse(can_access_task(MISSING, self.holder.id))


class WhoMustKeepAccessTests(Base):
    """The paths the narrowing must not break: each is a person with NO live team row (or no
    row at all) who legitimately works on the task."""

    def test_a_guest_who_holds_the_task_keeps_it(self):
        # A guest or shared-project participant: a ProjectMembers row and no TeamMembers row.
        guest = _user("guest")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=guest)
        self.assertFalse(TeamMembers.objects.filter(attendee=guest).exists())
        self.assertOpens(self.task(assignee=guest, reporter=guest), guest)

    def test_a_holder_with_a_project_row_keeps_it_although_the_team_row_is_gone(self):
        # The project path is its own OR: a leftover row (or a grant) is a legitimate path.
        person = _user("person")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=person)
        self.assertOpens(self.task(assignee=person), person)

    def test_the_team_owner_with_no_rows_at_all_keeps_it(self):
        owner = _user("owner")
        team = TeamMaster.objects.create(team_name="Own", team_email="o@example.com", owner=owner)
        project = ProjectMaster.objects.create(
            team=team, project_name="P", owner=self.user, project_system_user=self.user
        )
        self.assertFalse(TeamMembers.objects.filter(attendee=owner).exists())
        task = self.task(assignee=owner, reporter=owner, project=project, team=team)
        self.assertOpens(task, owner)

    def test_the_team_owner_holding_a_task_with_no_project_keeps_it(self):
        owner = _user("owner")
        team = TeamMaster.objects.create(team_name="Own", team_email="o@example.com", owner=owner)
        self.assertOpens(self.task(assignee=owner, project=None, team=team), owner)

    def test_the_project_owner_who_does_not_hold_the_task_keeps_it(self):
        ProjectMembers.objects.filter(attendee=self.user, project=self.project).delete()
        self.assertTrue(can_access_task(self.task(assignee=self.holder).task_id, self.user.id))

    def test_a_system_user_holding_a_task_keeps_it(self):
        system = _user("system", is_system_user=True)
        TeamMembers.objects.create(team=self.team, attendee=system)  # `project_manage` writes it
        self.assertOpens(self.task(assignee=system, reporter=system), system)

    def test_a_demo_user_holding_a_task_keeps_it(self):
        demo = _user("demo", is_demo=True)
        TeamMembers.objects.create(team=self.team, attendee=demo)  # `demo_seeder` writes it
        self.assertOpens(self.task(assignee=demo, reporter=demo), demo)

    def test_a_project_owner_who_left_the_team_still_opens_it_NOT_CHANGED_HERE(self):
        # `drop_in_team_access` removes the member row, `owner` stays set. Refusing this is a
        # different, riskier change (MIP-1592 report): pinned so a later change is deliberate.
        ProjectMembers.objects.filter(attendee=self.user, project=self.project).delete()
        TeamMembers.objects.filter(attendee=self.user).update(is_deleted=True)
        task = self.task(assignee=self.holder)
        self.assertTrue(can_access_task(task.task_id, self.user.id))


class TasksWithNoProjectOrNoTeamTests(Base):
    def test_a_task_in_no_project_asks_about_its_own_team(self):
        task = self.task(assignee=self.holder, reporter=self.member, project=None)
        self.assertOpens(task, self.holder)
        self.assertOpens(task, self.member)
        self.leave_team(self.holder)
        self.assertOpens(task, self.holder, expected=False)

    def test_a_task_in_no_project_is_not_opened_by_a_member_who_does_not_hold_it(self):
        task = self.task(assignee=self.holder, project=None)
        self.assertOpens(task, self.user, expected=False)

    def test_a_task_with_no_project_and_no_team_refuses_its_holders(self):
        # The team was deleted (`SET_NULL`): there is nothing to be a member of.
        task = self.task(assignee=self.holder, reporter=self.member, project=None, team=None)
        self.assertOpens(task, self.holder, expected=False)
        self.assertOpens(task, self.member, expected=False)

    def test_a_task_with_a_project_and_no_team_asks_the_projects_team(self):
        task = self.task(assignee=self.holder, team=None)
        self.assertOpens(task, self.holder)
        self.leave_team(self.holder)
        self.assertOpens(task, self.holder, expected=False)

    def test_the_projects_team_wins_over_the_tasks_when_they_differ(self):
        # Production has none of these (counted 2026-11-15); search resolves the PROJECT's team
        # (`acl.task_acl_user_ids`), so REST does too.
        other = TeamMaster.objects.create(
            team_name="Other", team_email="x@example.com", owner=_user("o2")
        )
        task = self.task(assignee=self.holder, team=other)
        self.assertOpens(task, self.holder)  # on the project's team
        self.leave_team(self.holder)
        TeamMembers.objects.create(team=other, attendee=self.holder)
        self.assertOpens(task, self.holder, expected=False)


class DeletedAccountTests(Base):
    """The 149-style holders: a deleted account that still named as assignee / reporter."""

    def test_a_deleted_account_holding_the_task_is_refused_everywhere(self):
        task = self.task(assignee=self.holder, reporter=self.holder)
        account_deletion.delete_account(self.holder)
        self.holder.refresh_from_db()
        self.assertTrue(self.holder.is_deleted)
        self.assertOpens(task, self.holder, expected=False)

    def test_a_deleted_account_that_was_the_reporter_only_is_refused(self):
        task = self.task(assignee=self.user, reporter=self.holder)
        account_deletion.delete_account(self.holder)
        self.assertOpens(task, self.holder, expected=False)

    def test_a_deleted_account_that_was_a_guest_is_refused(self):
        guest = _user("guest")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=guest)
        task = self.task(assignee=guest)
        account_deletion.delete_account(guest)
        self.assertOpens(task, guest, expected=False)

    def test_the_others_still_open_a_task_a_deleted_account_held(self):
        task = self.task(assignee=self.holder, reporter=self.holder)
        account_deletion.delete_account(self.holder)
        self.assertOpens(task, self.member)
        self.assertOpens(task, self.user)

    def test_a_solo_owner_who_deleted_the_account_is_known_to_pass_the_helper(self):
        # `active_team_member_ids` counts `TeamMaster.owner` with no `is_deleted` filter (the
        # search rule, kept), so the deleted owner of a team that went with them still reads as
        # "on the team". They cannot authenticate (`is_active=False`, the JWT re-reads it), so
        # nothing can ask; pinned so the day the helper learns about deleted teams this changes
        # deliberately.
        solo = _user("solo")
        team = TeamMaster.objects.create(team_name="Solo", team_email="s@example.com", owner=solo)
        TeamMembers.objects.create(team=team, attendee=solo)
        task = self.task(assignee=solo, project=None, team=team)
        account_deletion.delete_account(solo)
        solo.refresh_from_db()
        self.assertFalse(solo.is_active)
        self.assertTrue(can_access_task(task.task_id, solo.id))


class QueryCountTests(Base):
    """The active test rides on the task query: a current holder costs what it always did."""

    def count(self, fn):
        with CaptureQueriesContext(connection) as ctx:
            fn()
        return len(ctx)

    def test_a_current_holder_costs_one_query(self):
        task = self.task(assignee=self.holder, reporter=self.member)
        self.assertEqual(self.count(lambda: can_access_task(task.task_id, self.holder.id)), 1)

    def test_a_holder_who_is_the_team_owner_costs_one_query(self):
        task = self.task(assignee=self.user)
        self.assertEqual(self.count(lambda: can_access_task(task.task_id, self.user.id)), 1)

    def test_the_cost_for_everyone_else_is_what_it_was(self):
        task = self.task(assignee=self.holder)
        stranger = _user("stranger")
        # task row + owner EXISTS + member EXISTS (+ the project's own queries): unchanged.
        self.assertEqual(self.count(lambda: can_access_task(task.task_id, self.member.id)), 3)
        self.assertEqual(self.count(lambda: can_access_task(task.task_id, stranger.id)), 3)
        self.assertEqual(self.count(lambda: can_access_task(task.task_id, self.user.id)), 2)

    def test_a_refused_holder_pays_the_project_path_once(self):
        task = self.task(assignee=self.holder)
        self.leave_team(self.holder)
        self.assertEqual(self.count(lambda: can_access_task(task.task_id, self.holder.id)), 3)

    def test_the_batch_does_not_ask_per_person(self):
        people = [_user(f"p{i}") for i in range(6)]
        for p in people[:3]:
            TeamMembers.objects.create(team=self.team, attendee=p)
        task = self.task(assignee=people[0], reporter=people[3])

        def ask(ids):
            return task_reader_ids(
                task.project_id, task.assignee_id, task.reporter_id, ids, team_id=task.team_id
            )

        one = self.count(lambda: ask([people[0].id]))
        many = self.count(lambda: ask([p.id for p in people]))
        self.assertLessEqual(many, 4)
        self.assertLessEqual(many, one + 2)
        self.assertEqual(self.count(lambda: ask([])), 0)

    def test_nobody_who_holds_the_task_asked_means_no_team_query(self):
        task = self.task(assignee=self.holder)

        def ask():
            return task_reader_ids(
                task.project_id, task.assignee_id, task.reporter_id, [self.member.id],
                team_id=task.team_id,
            )  # fmt: skip

        self.assertEqual(self.count(ask), 2)  # the project's two, no team query

    def test_the_feed_and_the_prune_do_not_grow_with_the_rows(self):
        def feed(n):
            tasks = [self.task(assignee=self.holder) for _ in range(n)]
            rows = [
                (i, v3_activity.SURFACE_TASK_BODY, {"taskId": t.task_id})
                for i, t in enumerate(tasks)
            ]
            return self.count(lambda: unreadable_surface_activity_ids(self.holder.id, rows))

        self.assertEqual(feed(1), feed(6))

        def pairs(n):
            tasks = [self.task(assignee=self.holder) for _ in range(n)]
            ids = [t.task_id for t in tasks]
            who = [self.holder.id, self.member.id, self.user.id]
            return self.count(lambda: task_reader_pairs(who, ids))

        self.assertEqual(pairs(1), pairs(6))

        def prune(n):
            for _ in range(n):
                self.task(assignee=self.holder).collaborators.set([self.holder])
            return self.count(unreadable_collaborator_rows)

        a = prune(1)
        self.assertEqual(a, prune(5))


class SignatureTests(Base):
    def test_task_reader_ids_cannot_be_asked_without_the_team(self):
        task = self.task(assignee=self.holder)
        with self.assertRaises(TypeError):
            task_reader_ids(task.project_id, task.assignee_id, task.reporter_id, [self.holder.id])


class NotificationPathsTests(Base):
    """Built on the rule: an ex-member who holds the task is not told about it (nobody gains)."""

    def test_a_collaborator_who_is_also_the_assignee_and_left_is_not_notifiable(self):
        task = self.task(assignee=self.holder, reporter=self.user)
        task.collaborators.set([self.holder, self.member])
        self.assertEqual(
            notifiable_collaborator_ids(task.task_id), {str(self.holder.id), str(self.member.id)}
        )
        self.leave_team(self.holder)
        self.assertEqual(notifiable_collaborator_ids(task.task_id), {str(self.member.id)})

    def test_a_comment_does_not_ping_an_assignee_who_left(self):
        task = self.task(assignee=self.holder, reporter=self.user)
        task.collaborators.set([self.member])
        self.assertEqual(
            _participant_ids(task.task_id, set()), {str(self.holder.id), str(self.member.id)}
        )
        self.leave_team(self.holder)
        self.assertEqual(_participant_ids(task.task_id, set()), {str(self.member.id)})

    def test_a_body_comment_does_not_ping_an_assignee_who_left(self):
        task = self.task(assignee=self.holder, reporter=self.user)
        task.collaborators.set([self.member])
        self.assertEqual(
            v3_activity._task_stakeholders(task.task_id),
            {str(self.holder.id), str(self.member.id)},
        )
        self.leave_team(self.holder)
        self.assertEqual(v3_activity._task_stakeholders(task.task_id), {str(self.member.id)})

    def test_a_guest_assignee_is_still_told(self):
        guest = _user("guest")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=guest)
        task = self.task(assignee=guest)
        self.assertEqual(v3_activity._task_stakeholders(task.task_id), {str(guest.id)})
        self.assertEqual(_participant_ids(task.task_id, set()), {str(guest.id)})


class RestAnswersLikeAMissingTaskTests(Base):
    """End to end: the refusal is the answer of an id that does not exist."""

    def routes(self, task_id):
        c = self.client
        team, proj = str(self.team.team_id), self.project.project_id
        return {
            "attachment GET": lambda: c.get(f"/api/v2/task/attachment/?task_id={task_id}"),
            "getTask": lambda: c.get(
                f"/api/v2/task/getTask/?team_id={team}&project_id={proj}&task_id={task_id}"
            ),
            "task PUT": lambda: c.put(
                "/api/v2/task/", {"task_id": task_id, "title": "changed"}, format="json"
            ),
        }

    @staticmethod
    def answer(res):
        # The task PUT words a missing id ("Task not found to delete.") differently from a
        # refused one ("Task not found.") for ANY caller who may not open the task, a stranger
        # included (`task_views.py`, before this change): its status is compared, not its text.
        # A separate finding, not changed here.
        if res.request["REQUEST_METHOD"] == "PUT":
            return res.status_code, None
        return res.status_code, getattr(res, "data", None)

    def test_a_person_who_left_gets_the_answer_of_a_missing_id(self):
        task = self.task(assignee=self.holder, reporter=self.holder, title="keep")
        self.leave_team(self.holder)
        self.authenticate(self.holder)
        missing = {name: self.answer(call()) for name, call in self.routes(MISSING).items()}
        for name, call in self.routes(task.task_id).items():
            with self.subTest(route=name):
                self.assertEqual(self.answer(call()), missing[name])
        task.refresh_from_db()
        self.assertEqual(task.title, "keep")

    def test_a_current_holder_still_opens_and_edits_it(self):
        task = self.task(assignee=self.holder, reporter=self.holder, title="keep")
        self.authenticate(self.holder)
        missing = {name: self.answer(call()) for name, call in self.routes(MISSING).items()}
        for name, call in self.routes(task.task_id).items():
            with self.subTest(route=name):
                self.assertNotEqual(self.answer(call()), missing[name])
        task.refresh_from_db()
        self.assertEqual(task.title, "changed")

    def test_a_guest_holder_still_opens_it(self):
        guest = _user("guest")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=guest)
        task = self.task(assignee=guest)
        self.authenticate(guest)
        res = self.routes(task.task_id)["attachment GET"]()
        self.assertEqual(res.status_code, 200)
