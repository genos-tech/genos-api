"""A task of a soft-deleted project is not reachable in the app (MIP-1621, the app half).

`ProjectMaster.is_deleted` marks a project that is gone (legacy state: the app's own delete removes
the row). Its tasks must not come back from a list, from search or from an id, and an id answers
EXACTLY as a task that never existed does. `can_access_task` (about 50 call sites) and the lists
behind `reachable_project_ids` did not look at the project, so a member, an assignee or a reporter
still read, listed, searched and edited such a task.

What is pinned here, per surface:

  * by id (the call matrix of `test_deleted_task_refused`): a task of a gone project answers
    exactly as a missing id, for the project's member, its owner, an assignee who is not on the
    project, and a teammate with no part in it; a task of a live project does not;
  * lists, search by display id, the `#` picker's team search: the gone project's task id, title,
    display id and project name appear nowhere in the answer, the live project's still do;
  * the project-scoped lists answer a gone project as a missing one;
  * the guards, the SQL twins and the search filter: query counts and the filter built for a
    live project are what they were.
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.api_key_models import SCOPE_READ, ApiKey, generate_key, hash_key
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine import search as search_module
from origin.services import live_projects
from origin.services.activity_minimal_card import task_reader_pairs
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_deleted_task_refused import (
    MISSING,
    DeletedTaskBase,
    TestEndpointsRefuseADeletedTask,
    _answer,
)
from origin.views.utils.scope_guards import (
    can_access_task,
    is_project_member,
    member_project_ids,
    reachable_project_ids,
)

User = get_user_model()

GONE_TITLE = "Renew the harbour lease"
GONE_PROJECT = "Wound-down harbour"
LIVE_TITLE = "Repaint the lighthouse"


class World(BaseAPITestCase):
    """A gone project and a live twin, with every kind of person a task has.

    self.user   owner of both projects and reporter of both tasks
    self.user2  member of both projects (a `ProjectMembers` row)
    self.holder assignee of both tasks, on the team, NOT on either project
    self.mate   on the team, nothing to do with either project
    """

    def setUp(self):
        super().setUp()
        self.holder = User.objects.create_user(
            username="holder", email="holder@example.com", password="pw"
        )
        self.mate = User.objects.create_user(
            username="mate", email="mate@example.com", password="pw"
        )
        for person in (self.holder, self.mate):
            TeamMembers.objects.create(team=self.team, attendee=person)
        self.gone = self.project("GON", GONE_PROJECT, is_deleted=True)
        self.live = self.project("LIV", "Lighthouse", is_deleted=False)
        self.gone_task = self.task(self.gone, GONE_TITLE)
        self.live_task = self.task(self.live, LIVE_TITLE)

    def project(self, code, name, *, is_deleted):
        row = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=code,
            owner=self.user,
            project_system_user=self.user,
            is_deleted=is_deleted,
        )
        ProjectMembers.objects.create(team=self.team, project=row, attendee=self.user2)
        return row

    def task(self, project, title, **extra):
        fields = {
            "team": self.team,
            "project": project,
            "title": title,
            "status": "Open",
            "assignee": self.holder,
            "reporter": self.user,
            "project_task_number": 1,
        }
        fields.update(extra)
        return TaskMaster.objects.create(**fields)

    @property
    def people(self):
        return {
            "owner": self.user,
            "member": self.user2,
            "assignee who is not on the project": self.holder,
            "teammate": self.mate,
        }


GONE_MARKERS = (GONE_TITLE, GONE_PROJECT, "GON-1")


class ByIdTests(DeletedTaskBase):
    """The call matrix of MIP-1572, with the project gone instead of the task."""

    calls = TestEndpointsRefuseADeletedTask.calls

    def setUp(self):
        super().setUp()
        ProjectMaster.objects.filter(pk=self.project.pk).update(is_deleted=True)
        self.project.refresh_from_db()
        self.holder = User.objects.create_user(
            username="holder", email="holder@example.com", password="pw"
        )
        self.mate = User.objects.create_user(
            username="mate", email="mate@example.com", password="pw"
        )
        for person in (self.holder, self.mate):
            TeamMembers.objects.create(team=self.team, attendee=person)

    def test_every_endpoint_answers_a_task_of_a_gone_project_as_a_missing_task(self):
        for who, person in (
            ("member", self.user2),
            ("owner", self.user),
            ("assignee", self.holder),
            ("teammate", self.mate),
        ):
            gone = self.task(assignee=self.holder)
            self.authenticate(person)
            for name, call in self.calls().items():
                with self.subTest(f"{name} as {who}"):
                    self.assertEqual(_answer(call(gone.task_id)), _answer(call(MISSING)))

    def test_the_same_endpoints_still_read_a_task_of_a_live_project(self):
        ProjectMaster.objects.filter(pk=self.project.pk).update(is_deleted=False)
        self.project.refresh_from_db()
        for name, call in self.calls().items():
            with self.subTest(name):
                # A fresh task per call: the hard delete in the matrix removes the one it is given.
                self.assertNotEqual(_answer(call(self.task().task_id)), _answer(call(MISSING)))

    def test_the_writes_change_nothing_on_the_task(self):
        gone = self.task(title="before")
        self.client.put("/api/v2/task/", {"task_id": gone.task_id, "title": "after"}, format="json")
        self.client.post(
            "/api/v2/task/comment/",
            {"task_id": gone.task_id, "sender_id": str(self.user2.id), "comment_body": []},
            format="json",
        )
        self.client.delete(
            f"/api/v2/task/?team_id={self.team.team_id}&task_id={gone.task_id}&is_init_task_boolean=0"
        )
        gone.refresh_from_db()
        self.assertEqual(gone.title, "before")

    def test_a_task_cannot_be_created_in_a_gone_project_as_in_a_missing_one(self):
        def create(project_id):
            return self.client.post(
                "/api/v2/task/",
                {
                    "project": project_id,
                    "team": str(self.team.team_id),
                    "assignee": str(self.user.id),
                    "reporter": str(self.user.id),
                    "title": "new",
                    "priority": "Medium",
                    "effort_level": "Small",
                    "status": "Open",
                    "content": [],
                    "links": [],
                    "tags": [],
                    "is_init_task": False,
                },
                format="json",
            )

        self.assertEqual(_answer(create(self.project.project_id)), _answer(create(MISSING)))
        self.assertFalse(TaskMaster.objects.filter(title="new").exists())


class GuardTests(World):
    def test_can_access_task_refuses_every_kind_of_person_for_a_gone_project(self):
        for who, person in self.people.items():
            with self.subTest(who):
                self.assertFalse(can_access_task(self.gone_task.task_id, person.id))

    def test_it_still_admits_the_same_people_for_a_live_project(self):
        for who in ("owner", "member", "assignee who is not on the project"):
            with self.subTest(who):
                self.assertTrue(can_access_task(self.live_task.task_id, self.people[who].id))
        self.assertFalse(can_access_task(self.live_task.task_id, self.mate.id))

    def test_include_deleted_does_not_bring_back_a_gone_project(self):
        self.assertFalse(
            can_access_task(self.gone_task.task_id, self.user.id, include_deleted=True)
        )

    def test_the_agent_gated_caller_can_keep_the_old_answer(self):
        self.assertTrue(
            can_access_task(self.gone_task.task_id, self.user.id, include_gone_project=True)
        )

    def test_a_task_with_no_project_is_live(self):
        orphan = self.task(None, "no project")
        self.assertTrue(can_access_task(orphan.task_id, self.holder.id))

    def test_the_guard_costs_the_queries_it_did(self):
        with CaptureQueriesContext(connection) as live:
            can_access_task(self.live_task.task_id, self.user2.id)
        with CaptureQueriesContext(connection) as gone:
            can_access_task(self.gone_task.task_id, self.user2.id)
        # The refusal costs the one query that says no; a live task costs what it always did.
        self.assertEqual(len(gone), 1)
        self.assertLessEqual(len(live), 3)

    def test_the_sql_twin_of_the_guard_agrees(self):
        from origin.views.task.task_views import _openable_task_q

        for who, person in self.people.items():
            for task in (self.gone_task, self.live_task):
                with self.subTest(f"{who} {task.title}"):
                    in_sql = TaskMaster.objects.filter(
                        _openable_task_q(person.id, ""), task_id=task.task_id
                    ).exists()
                    self.assertEqual(in_sql, can_access_task(task.task_id, person.id))

    def test_the_batch_twin_agrees(self):
        pairs = task_reader_pairs(
            [p.id for p in self.people.values()], [self.gone_task.task_id, self.live_task.task_id]
        )
        for who, person in self.people.items():
            for task in (self.gone_task, self.live_task):
                with self.subTest(f"{who} {task.title}"):
                    self.assertEqual(
                        (str(person.id), task.task_id) in pairs,
                        can_access_task(task.task_id, person.id),
                    )

    def test_member_project_ids_leaves_the_gone_project_out_only_when_asked(self):
        self.assertIn(self.gone.project_id, member_project_ids(self.user2.id, self.team.team_id))
        self.assertNotIn(
            self.gone.project_id,
            member_project_ids(self.user2.id, self.team.team_id, live_only=True),
        )
        self.assertNotIn(
            self.gone.project_id, reachable_project_ids(self.user2.id, self.team.team_id)
        )
        self.assertIn(self.live.project_id, reachable_project_ids(self.user2.id, self.team.team_id))

    def test_is_project_member_live_only(self):
        for person in (self.user, self.user2):
            self.assertTrue(is_project_member(self.gone.project_id, person.id))
            self.assertFalse(is_project_member(self.gone.project_id, person.id, live_only=True))
            self.assertTrue(is_project_member(self.live.project_id, person.id, live_only=True))

    def test_a_shared_gone_project_is_not_reachable_either(self):
        from origin.models.common.team_models import ExternalGrant  # noqa: F401

        shared = {str(self.gone.project_id): None, str(self.live.project_id): None}
        with mock.patch(
            "origin.services.external_grants.external_objects_for_member", return_value=shared
        ):
            ids = reachable_project_ids(self.mate.id, self.team.team_id)
        self.assertEqual(sorted(ids), [self.live.project_id])

    def test_the_helper(self):
        self.assertEqual(live_projects.gone_project_ids(), {self.gone.project_id})
        self.assertEqual(live_projects.gone_project_ids(self.team.team_id), {self.gone.project_id})
        other = TeamMaster.objects.create(team_name="O", team_email="o@x.com", owner=self.user)
        self.assertEqual(live_projects.gone_project_ids(other.team_id), set())
        self.assertEqual(
            live_projects.live_project_ids([self.gone.project_id, self.live.project_id]),
            [self.live.project_id],
        )
        self.assertEqual(live_projects.live_project_ids([]), [])
        self.assertTrue(live_projects.project_is_gone(self.gone))
        self.assertFalse(live_projects.project_is_gone(None))
        rows = TaskMaster.objects.filter(live_projects.not_in_gone_project())
        self.assertIn(self.live_task, rows)
        self.assertNotIn(self.gone_task, rows)


class ListTests(World):
    def body(self, res):
        self.assertEqual(res.status_code, 200, res.content)
        return json.dumps(res.json())

    def assertGone(self, text, who):
        for marker in GONE_MARKERS:
            self.assertNotIn(marker, text, who)
        self.assertNotIn(f'"{self.gone_task.task_id}"', text, who)

    def team_id(self):
        return str(self.team.team_id)

    def reads(self):
        """`name -> fn(user) -> response`, the lists that carry tasks."""
        c = self.client
        team = self.team_id()

        def meta(u):
            return c.get(f"/api/v2/task/meta/?team_id={team}&user_id={u.id}")

        return {
            "getTeamTasks": lambda u: c.get(f"/api/v2/task/getTeamTasks/?team_id={team}"),
            "getTeamTasksPage": lambda u: c.get(
                f"/api/v2/task/getTeamTasksPage/?team_id={team}&limit=100"
            ),
            "task meta": meta,
            "getMyAssignedTasks": lambda u: c.get(
                f"/api/v2/task/getMyAssignedTasks/?team_id={team}"
            ),
            "search/teamTasks (all projects)": lambda u: c.get(
                f"/api/v2/search/teamTasks/?team_id={team}&project_id=-1"
                "&statuses=open,wip,blocked,pending&top_n=-1&include_all=true"
            ),
            "search/taskByDisplayId": lambda u: c.get(
                f"/api/v2/search/taskByDisplayId/?team_id={team}&display_id=GON-1"
            ),
        }

    def test_no_list_names_a_task_of_a_gone_project(self):
        for who, person in self.people.items():
            self.authenticate(person)
            for name, fn in self.reads().items():
                if name == "search/taskByDisplayId":
                    continue
                with self.subTest(f"{name} as {who}"):
                    self.assertGone(self.body(fn(person)), who)

    def test_the_display_id_lookup_answers_as_for_a_missing_id(self):
        for who, person in self.people.items():
            self.authenticate(person)
            gone = self.client.get(
                f"/api/v2/search/taskByDisplayId/?team_id={self.team_id()}&display_id=GON-1"
            )
            missing = self.client.get(
                f"/api/v2/search/taskByDisplayId/?team_id={self.team_id()}&display_id=ZZZ-9"
            )
            self.assertEqual(_answer(gone), _answer(missing), who)

    def test_the_same_lists_still_carry_the_live_projects_task(self):
        for who in ("owner", "member"):
            person = self.people[who]
            self.authenticate(person)
            for name in ("getTeamTasks", "getTeamTasksPage", "task meta"):
                with self.subTest(f"{name} as {who}"):
                    self.assertIn(LIVE_TITLE, self.body(self.reads()[name](person)))
        self.authenticate(self.user2)
        self.assertIn(
            LIVE_TITLE,
            self.body(
                self.client.get(
                    f"/api/v2/search/teamTasks/?team_id={self.team_id()}&project_id=-1"
                    "&statuses=open&top_n=-1&include_all=true"
                )
            ),
        )
        live = self.client.get(
            f"/api/v2/search/taskByDisplayId/?team_id={self.team_id()}&display_id=LIV-1"
        )
        self.assertEqual(live.status_code, 200)

    def test_the_assignees_own_list_leaves_it_out(self):
        self.authenticate(self.holder)
        res = self.client.get(f"/api/v2/task/getMyAssignedTasks/?team_id={self.team_id()}")
        self.assertGone(self.body(res), "holder")

    def test_the_project_scoped_reads_answer_a_gone_project_as_a_missing_one(self):
        c, team = self.client, self.team_id()
        for who in ("owner", "member"):
            self.authenticate(self.people[who])
            for name, url in (
                (
                    "getProjectTasks",
                    "/api/v2/task/getProjectTasks/?team_id={t}&project_id={p}",
                ),
                (
                    "childTasks",
                    "/api/v2/task/childTasks/?team_id={t}&project_id={p}&current_task_id=1",
                ),
                (
                    "search/teamTasks explicit",
                    "/api/v2/search/teamTasks/?team_id={t}&project_id={p}&statuses=open&top_n=10",
                ),
            ):
                with self.subTest(f"{name} as {who}"):
                    gone = _answer(c.get(url.format(t=team, p=self.gone.project_id)))
                    missing = _answer(c.get(url.format(t=team, p=MISSING)))
                    self.assertEqual(gone, missing)
                    self.assertEqual(gone[0], 404)

    def test_the_live_projects_scoped_reads_still_answer(self):
        self.authenticate(self.user2)
        res = self.client.get(
            f"/api/v2/task/getProjectTasks/?team_id={self.team_id()}&project_id={self.live.project_id}"
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(LIVE_TITLE, json.dumps(res.json()))

    def test_task_progress_answers_as_for_a_missing_task(self):
        for who in ("owner", "member"):
            self.authenticate(self.people[who])
            gone = self.client.get(f"/api/v2/task/progress/?task_id={self.gone_task.task_id}")
            missing = self.client.get(f"/api/v2/task/progress/?task_id={MISSING}")
            self.assertEqual(_answer(gone), _answer(missing), who)
            live = self.client.get(f"/api/v2/task/progress/?task_id={self.live_task.task_id}")
            self.assertEqual(live.status_code, 200, who)

    def test_recording_an_open_answers_as_for_a_missing_task(self):
        for who in ("owner", "member"):
            self.authenticate(self.people[who])
            gone = self.client.post(
                "/api/v2/task/recent/open/", {"task_id": self.gone_task.task_id}, format="json"
            )
            missing = self.client.post(
                "/api/v2/task/recent/open/", {"task_id": MISSING}, format="json"
            )
            self.assertEqual(_answer(gone), _answer(missing), who)
            live = self.client.post(
                "/api/v2/task/recent/open/", {"task_id": self.live_task.task_id}, format="json"
            )
            self.assertEqual(live.status_code, 200, who)

    def test_a_dependency_edge_to_a_gone_task_is_not_shown_from_the_live_end(self):
        other_live = self.task(self.live, "Other live", project_task_number=2)
        TaskDependency.objects.create(
            blocker_task=self.gone_task,
            blocked_task=other_live,
            team=self.team,
            created_by=self.user,
        )
        self.authenticate(self.user)
        res = self.client.get(f"/api/v2/task/dependency/list/?task_id={other_live.task_id}")
        self.assertEqual(res.status_code, 200)
        self.assertGone(json.dumps(res.json()), "owner")
        batch = self.client.get(
            f"/api/v2/task/dependency/list-for-tasks/?task_ids={other_live.task_id}"
        )
        self.assertGone(json.dumps(batch.json()), "owner")

    def test_the_milestone_of_a_gone_project_answers_as_a_missing_one(self):
        gone_ms = MilestoneMaster.objects.create(
            team=self.team, project=self.gone, title="M", reporter=self.user
        )
        live_ms = MilestoneMaster.objects.create(
            team=self.team, project=self.live, title="M", reporter=self.user
        )
        for who in ("owner", "member"):
            self.authenticate(self.people[who])
            gone = self.client.get(f"/api/v2/milestone/{gone_ms.milestone_id}/")
            missing = self.client.get(f"/api/v2/milestone/{MISSING}/")
            self.assertEqual(_answer(gone), _answer(missing), who)
            self.assertEqual(
                self.client.get(f"/api/v2/milestone/{live_ms.milestone_id}/").status_code, 200, who
            )
            for verb in ("patch", "delete"):
                res = getattr(self.client, verb)(f"/api/v2/milestone/{gone_ms.milestone_id}/", {})
                miss = getattr(self.client, verb)(f"/api/v2/milestone/{MISSING}/", {})
                self.assertEqual(_answer(res), _answer(miss), f"{verb} as {who}")
        gone_ms.refresh_from_db()
        self.assertFalse(gone_ms.is_deleted)


    def test_the_other_milestone_doors_answer_a_gone_project_as_a_missing_milestone(self):
        gone_ms = MilestoneMaster.objects.create(
            team=self.team, project=self.gone, title="M", reporter=self.user
        )
        converted = MilestoneMaster.objects.create(
            team=self.team,
            project=self.gone,
            title="C",
            reporter=self.user,
            is_deleted=True,
            converted_to_task=self.gone_task,
        )
        live_converted = MilestoneMaster.objects.create(
            team=self.team,
            project=self.live,
            title="C",
            reporter=self.user,
            is_deleted=True,
            converted_to_task=self.live_task,
        )
        for who in ("owner", "member"):
            self.authenticate(self.people[who])
            calls = {
                "convert to task": lambda m: self.client.post(
                    "/api/v2/milestone/convert-to-task/", {"milestone_id": m}, format="json"
                ),
                "assignee add": lambda m: self.client.post(
                    f"/api/v2/milestone/{m}/assignees/", {"user_id": str(self.user2.id)}, format="json"
                ),
                "assignee remove": lambda m: self.client.delete(
                    f"/api/v2/milestone/{m}/assignees/{self.user2.id}/"
                ),
                "the 410 of a converted milestone": lambda m: self.client.get(
                    f"/api/v2/milestone/{m}/"
                ),
            }
            for name, call in calls.items():
                with self.subTest(f"{name} as {who}"):
                    target = converted if name.startswith("the 410") else gone_ms
                    self.assertEqual(_answer(call(target.milestone_id)), _answer(call(MISSING)))
        self.authenticate(self.user2)
        self.assertEqual(
            self.client.get(f"/api/v2/milestone/{live_converted.milestone_id}/").status_code, 410
        )


class PublicApiTests(World):
    def test_the_public_list_and_its_total_leave_the_gone_project_out(self):
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user2,
            team=self.team,
            name="k",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=SCOPE_READ,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        res = self.client.get(f"/api/public/v1/tasks/?team_id={self.team.team_id}")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["total"], 1)
        self.assertEqual([t["title"] for t in res.json()["tasks"]], [LIVE_TITLE])
        named = self.client.get(
            f"/api/public/v1/tasks/?team_id={self.team.team_id}&project_id={self.gone.project_id}"
        )
        missing = self.client.get(
            f"/api/public/v1/tasks/?team_id={self.team.team_id}&project_id={MISSING}"
        )
        self.assertEqual(_answer(named), _answer(missing))


class SearchFilterTests(World):
    """The app search (and the Spotlight typeahead, which posts to it) filters in the query."""

    def build(self, **kw):
        return search_module._build_filter(
            str(self.team.team_id), str(self.user.id), None, None, None, **kw
        )

    def test_a_search_with_no_gone_project_builds_the_filter_it_always_built(self):
        self.assertEqual(self.build(), self.build(exclude_project_ids=None))
        self.assertEqual(self.build(), self.build(exclude_project_ids=[]))

    def test_the_gone_projects_are_excluded_in_the_query(self):
        clause = {"bool": {"must_not": [{"terms": {"project_id": [str(self.gone.project_id)]}}]}}
        self.assertEqual(self.build(exclude_project_ids=[self.gone.project_id])[-1], clause)
        self.assertEqual(self.build(exclude_project_ids=[self.gone.project_id])[:-1], self.build())

    def post(self, **extra):
        self.authenticate(self.user2)
        with mock.patch("origin.search_engine.views.search", return_value={"results": []}) as run:
            res = self.client.post(
                "/api/v2/search/",
                {"query": "harbour", "team_id": str(self.team.team_id), **extra},
                format="json",
            )
        self.assertEqual(res.status_code, 200, res.content)
        return run.call_args.kwargs

    def test_the_search_view_names_the_gone_projects_to_search(self):
        self.assertEqual(self.post()["exclude_project_ids"], [str(self.gone.project_id)])

    def test_the_search_view_passes_nothing_when_no_project_is_gone(self):
        ProjectMaster.objects.filter(pk=self.gone.pk).update(is_deleted=False)
        self.assertIsNone(self.post()["exclude_project_ids"])

    def test_the_typeahead_mode_is_the_same_view(self):
        self.assertEqual(
            self.post(mode="typeahead")["exclude_project_ids"], [str(self.gone.project_id)]
        )

    def test_graph_expansion_asks_for_the_same_exclusion(self):
        seen = []
        real = search_module._build_filter

        def spy(*args, **kw):
            seen.append(kw.get("exclude_project_ids"))
            return real(*args, **kw)

        client = mock.MagicMock()
        client.search.return_value = {"hits": {"hits": []}}
        grouped = [
            {
                "entity_type": "task",
                "entity_id": "task:1",
                "task_id": 1,
                "score": 1.0,
                "keyword_rank": 1,
            }
        ]
        with (
            mock.patch.object(search_module, "_build_filter", spy),
            mock.patch.object(
                search_module, "_walk_dependency_graph", return_value={5: (0.5, "downstream")}
            ),
        ):
            search_module._graph_expand(
                grouped,
                team_id=str(self.team.team_id),
                user_id=str(self.user.id),
                for_agent=False,
                client=client,
                index="x",
                exclude_project_ids=["9"],
            )
        self.assertEqual(seen, [["9"]])

    def test_search_hands_the_exclusion_to_the_filter(self):
        class Stop(Exception):
            pass

        seen = {}

        def spy(*args, **kw):
            seen.update(kw)
            raise Stop

        with (
            mock.patch.object(search_module, "_build_filter", spy),
            mock.patch.object(search_module, "get_client", return_value=mock.MagicMock()),
        ):
            with self.assertRaises(Stop):
                search_module.search(
                    query="harbour",
                    team_id=str(self.team.team_id),
                    user_id=str(self.user.id),
                    exclude_project_ids=["9"],
                )
        self.assertEqual(seen["exclude_project_ids"], ["9"])
