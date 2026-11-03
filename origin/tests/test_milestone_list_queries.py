"""`milestone/list` costs the same number of queries for 4 milestones or 400 (MIP-966).

The list view paid about four queries per milestone: the assignees (the view
prefetched them, then the serializer asked again with a new queryset, so the
prefetch was never read), the project of the backing task (only the task was
joined, and `displayId` reads `task.project.code`), and two COUNTs for the
progress bar. With ~16 ms per round trip in production that is what made the
page slow: 119 queries for 28 milestones.

Three things are pinned here:

  * `milestone_work_counts` gives, for many milestones at once, exactly the
    numbers `milestone_work_q` gives one milestone at a time (the agent tools
    still use `milestone_work_q`, so the two definitions must not drift);
  * the query count of the endpoint does not grow with the number of milestones;
  * the JSON the endpoint returns is the JSON it returned before (the expected
    value below was captured from unmodified `main`, ids replaced by the order
    in which they first appear because sequences differ between runs).
"""

import json
import random
from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.renderers import JSONRenderer

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services.milestone_rollup import milestone_work_counts, milestone_work_q
from origin.services.milestone_service import ensure_backing_task
from origin.views.task.milestone_views import _serialize_milestone

from .test_base import BaseAPITestCase

User = get_user_model()

FROZEN = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

# Keys whose value is a database id (integers, and UUIDs for users). Sequences
# are not reset between tests and UUIDs are random, so a literal id would differ
# from run to run; the order of first appearance does not (and still shows two
# rows swapping ids, or one id turning into another).
ID_KEYS = (
    "milestoneId",
    "taskId",
    "projectId",
    "teamId",
    "sprintId",
    "folderId",
    "reporterId",
    "userId",
)


def normalise_ids(payload):
    seen: dict[str, dict[object, int]] = {}

    def token(key, value):
        ids = seen.setdefault(key, {})
        return f"{key}#{ids.setdefault(value, len(ids) + 1)}"

    def walk(node):
        if isinstance(node, dict):
            return {
                k: (token(k, v) if k in ID_KEYS and v is not None else walk(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(payload)


class MilestoneListBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Genos",
            code="GEN",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.people = [
            User.objects.create_user(
                username=f"person{i}",
                email=f"person{i}@example.com",
                password="pw-12345-pw",
            )
            for i in range(6)
        ]
        self.authenticate()

    # ------------------------------------------------------------------ build
    def _task(self, title, *, milestone=None, parent=None, status="Open", **flags):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title=title,
            status=status,
            milestone=milestone,
            parent_task_id=parent.task_id if parent is not None else None,
            priority="Normal",
            effort_level="Low",
            **flags,
        )

    def _milestone(self, title, *, backing=True, reporter="default", **kw):
        m = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.people[0] if reporter == "default" else reporter,
            title=title,
            status="Open",
            **kw,
        )
        if backing:
            ensure_backing_task(m)
        return m

    def _freeze(self, milestone, minutes):
        """Fixed timestamps (the list is ordered by `-ts_updated_at`)."""
        MilestoneMaster.objects.filter(pk=milestone.pk).update(
            ts_created_at=FROZEN, ts_updated_at=FROZEN + timedelta(minutes=minutes)
        )

    def _full_milestone(self, title, minutes):
        """A milestone with everything that used to cost a query of its own."""
        m = self._milestone(title)
        for person in self.people[:2]:
            MilestoneAssignees.objects.create(milestone=m, user=person, team=self.team)
        m.task.collaborators.add(self.people[2], self.people[3])
        self._task(f"{title}-fk-open", milestone=m)
        self._task(f"{title}-fk-closed", milestone=m, status="Closed")
        self._task(f"{title}-child-open", parent=m.task)
        self._task(f"{title}-child-closed", parent=m.task, status="Closed")
        self._freeze(m, minutes)
        return m

    # ----------------------------------------------------------------- request
    def _list(self):
        res = self.client.get(
            reverse("project_milestones"), {"project_id": self.project.project_id}
        )
        self.assertEqual(res.status_code, 200, res.content)
        return res

    def _queries(self) -> int:
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            self._list()
        return len(ctx)


# --------------------------------------------------------------------------
# 1. the batched counts are the per-milestone counts
# --------------------------------------------------------------------------


def _per_milestone_counts(m):
    """What `_serialize_milestone` computed before: two COUNTs per milestone."""
    tasks = TaskMaster.objects.filter(milestone_work_q(m)).distinct()
    return tasks.count(), tasks.filter(status="Closed").count()


class MilestoneWorkCountsTests(MilestoneListBase):
    def _assert_equivalent(self):
        milestones = list(MilestoneMaster.objects.filter(project=self.project))
        got = milestone_work_counts(milestones)
        self.assertEqual(set(got), {m.milestone_id for m in milestones})
        for m in milestones:
            with self.subTest(milestone=m.title):
                self.assertEqual(tuple(got[m.milestone_id]), _per_milestone_counts(m))
        return got

    def test_every_way_a_task_can_or_cannot_belong(self):
        a, b, c = (self._milestone(t) for t in "ABC")
        empty = self._milestone("empty")
        legacy = self._milestone("legacy", backing=False)  # no backing row: FK path only

        self._task("fk-open", milestone=a)
        self._task("fk-closed", milestone=a, status="Closed")
        self._task("parent-only", parent=a.task, status="Closed")
        self._task("both-ways", milestone=a, parent=a.task)  # counts once
        # one task, two milestones: A through its FK, B through its parent
        self._task("fk-a-parent-b", milestone=a, parent=b.task, status="Closed")
        self._task("parent-b-fk-c", milestone=c, parent=b.task, status="Closed")
        self._task("parent-a-fk-b", milestone=b, parent=a.task)
        # left out
        self._task("soft-deleted", milestone=a, is_deleted=True)
        self._task("status-deleted", milestone=a, status="Deleted")
        self._task("status-deleted-child", parent=a.task, status="Deleted")
        self._task("scaffold", milestone=a, is_init_task=True)
        self._task("scaffold-child", parent=a.task, is_init_task=True)
        self._task("soft-deleted-child", parent=a.task, is_deleted=True)
        # the parent path needs is_milestone=False ...
        self._task("milestone-child", parent=a.task, is_milestone=True)
        # ... the FK path does not
        self._task("milestone-by-fk", milestone=a, is_milestone=True, status="Closed")
        # a milestone nested under another one's backing row: not work of the parent
        TaskMaster.objects.filter(pk=c.task_id).update(parent_task_id=a.task_id)
        self._task("loose")
        self._task("legacy-fk", milestone=legacy)
        self._task("legacy-fk-closed", milestone=legacy, status="Closed")
        self._task("legacy-deleted", milestone=legacy, is_deleted=True)

        got = self._assert_equivalent()
        # Guard against a vacuous pass: the interesting milestones really count something,
        # the empty one really counts nothing, and the cross-over task is in both.
        self.assertEqual(tuple(got[empty.milestone_id]), (0, 0))
        self.assertEqual(tuple(got[legacy.milestone_id]), (2, 1))
        self.assertGreater(got[a.milestone_id][0], 4)
        self.assertGreater(got[b.milestone_id][0], 1)

    def test_many_random_tasks_agree_with_the_per_milestone_count(self):
        rng = random.Random(966)
        milestones = [self._milestone(f"M{i}") for i in range(5)]
        milestones.append(self._milestone("legacy", backing=False))
        for i in range(160):
            fk = rng.choice([None, *milestones])
            parent = rng.choice([None, *[m for m in milestones if m.task_id]])
            self._task(
                f"t{i}",
                milestone=fk,
                parent=parent,
                status=rng.choice(["Open", "Closed", "Closed", "WIP", "Deleted"]),
                is_deleted=rng.random() < 0.12,
                is_init_task=rng.random() < 0.08,
                is_milestone=rng.random() < 0.1,
            )
        got = self._assert_equivalent()
        self.assertGreater(sum(v[0] for v in got.values()), 40)
        self.assertGreater(sum(v[1] for v in got.values()), 10)

    def test_it_is_one_query_whatever_the_number_of_milestones(self):
        for i in range(7):
            self._full_milestone(f"M{i}", i)
        milestones = list(MilestoneMaster.objects.filter(project=self.project))
        with self.assertNumQueries(1):
            milestone_work_counts(milestones)
        with self.assertNumQueries(0):
            self.assertEqual(milestone_work_counts([]), {})


# --------------------------------------------------------------------------
# 2. the number of queries does not grow with the number of milestones
# --------------------------------------------------------------------------


class MilestoneListQueryCountTests(MilestoneListBase):
    # Budget for one list request, whatever the number of milestones (the caller
    # owns the project; a member who does not pays one more for the membership check):
    #   1 the caller              2 the project              3 the membership check
    #   4 the milestones, with their backing task, its project and the reporter joined
    #   5 the assignees, with their user joined (a Prefetch, ordered by id)
    #   6 the backing tasks' collaborators
    #   7 the progress of all of them (`milestone_work_counts`)
    # Before: 7 + 4 per milestone.
    BUDGET = 7

    def test_the_count_does_not_grow_with_the_number_of_milestones(self):
        for i in range(4):
            self._full_milestone(f"M{i}", i)
        small = self._queries()
        for i in range(4, 8):
            self._full_milestone(f"M{i}", i)
        large = self._queries()

        self.assertEqual(len(self._list().json()["milestones"]), 8)
        self.assertEqual(large, small, "queries grow with the number of milestones")
        self.assertEqual(small, self.BUDGET)

    def test_a_milestone_with_no_people_and_no_tasks_costs_nothing_extra(self):
        self._full_milestone("full", 0)
        before = self._queries()
        for i in range(5):
            self._milestone(f"bare{i}")
        self.assertEqual(self._queries(), before)


# --------------------------------------------------------------------------
# 3. the JSON is the JSON it was
# --------------------------------------------------------------------------


class MilestoneListPayloadTests(MilestoneListBase):
    def build_fixture(self):
        """Four milestones, each shaped differently; every timestamp fixed."""
        # Alpha: 3 assignees, one collaborator, FK + child tasks of every kind. The
        # assignees are added in the order unmodified `main` happened to return them
        # (its read was unordered; the new one orders by row id, by decision).
        alpha = self._milestone("Alpha", reporter=self.user)
        for person in (self.people[0], self.people[1], self.people[2]):
            MilestoneAssignees.objects.create(milestone=alpha, user=person, team=self.team)
        alpha.task.collaborators.add(self.people[3])
        self._task("a-fk-open", milestone=alpha)
        self._task("a-fk-closed", milestone=alpha, status="Closed")
        self._task("a-child-open", parent=alpha.task, status="WIP")
        self._task("a-child-closed", parent=alpha.task, status="Closed")
        self._task("a-soft-deleted", milestone=alpha, is_deleted=True)
        self._task("a-status-deleted", parent=alpha.task, status="Deleted")

        # Beta: no assignees, no tasks; every optional field filled in; chat origin
        beta = self._milestone(
            "Beta",
            description=[{"type": "paragraph", "content": "Why this exists"}],
            priority="High",
            priority_code=3,
            effort_level="Medium",
            effort_level_code=2,
            start_date=FROZEN.date(),
            due_date=(FROZEN + timedelta(days=30)).date(),
            tags=["backend", "perf"],
            links=[{"id": "l1", "url": "https://example.com/spec", "title": "Spec"}],
        )
        TaskMaster.objects.filter(pk=beta.task_id).update(
            chat_type=2,
            chat_id="chat-1",
            thread_id="thread-1",
            custom_field_values={"f1": "x"},
        )

        # Gamma: no reporter; one task that is its own work by FK and by parent,
        # one that is Gamma's by FK and Alpha's by parent
        gamma = self._milestone("Gamma", reporter=None)
        MilestoneAssignees.objects.create(milestone=gamma, user=self.people[4], team=self.team)
        self._task("g-both-ways", milestone=gamma, parent=gamma.task, status="Closed")
        self._task("g-fk-and-alpha-parent", milestone=gamma, parent=alpha.task)

        # Delta: one collaborator, one assignee, a nested child of a child
        delta = self._milestone("Delta", reporter=self.people[5])
        MilestoneAssignees.objects.create(milestone=delta, user=self.user, team=self.team)
        delta.task.collaborators.add(self.people[1])
        child = self._task("d-child", parent=delta.task, status="Closed")
        self._task("d-grandchild", parent=child)

        for minutes, m in enumerate((delta, gamma, beta, alpha)):
            self._freeze(m, minutes)
        return alpha, beta, gamma, delta

    def test_the_response_is_unchanged(self):
        self.build_fixture()
        payload = json.loads(self._list().content)
        self.assertEqual(list(payload), ["milestones"])
        self.assertEqual(normalise_ids(payload["milestones"]), EXPECTED_MILESTONES)
        # the key order is part of the wire format too
        self.assertEqual(list(payload["milestones"][0]), EXPECTED_KEY_ORDER)

    def test_every_item_is_what_the_single_milestone_serializer_gives(self):
        """The per-row path stays (the detail / PATCH / assignee endpoints use it)
        and agrees with the batched one."""
        self.build_fixture()
        legacy = self._milestone("Legacy", backing=False)
        self._task("legacy-fk", milestone=legacy)
        self._task("legacy-fk-closed", milestone=legacy, status="Closed")
        items = {i["milestoneId"]: i for i in self._list().json()["milestones"]}
        self.assertEqual(len(items), 5)
        for milestone_id, item in items.items():
            m = MilestoneMaster.objects.select_related("task", "reporter").get(pk=milestone_id)
            single = json.loads(JSONRenderer().render(_serialize_milestone(m)))
            with self.subTest(title=item["title"]):
                self.assertEqual(item, single)
        # The legacy milestone got its backing task on the way, and its progress ignores it.
        item = items[legacy.milestone_id]
        self.assertIsNotNone(item["taskId"])
        self.assertTrue(item["displayId"].startswith("GEN-"))
        self.assertEqual((item["tasksTotal"], item["tasksClosed"]), (2, 1))

    def test_assignees_come_in_the_order_they_were_added(self):
        """Ordered by the join row's id. Rewriting the first row's indexed
        column moves it to the end of the table's physical order (and of the
        index's), which an unordered read would show."""
        m = self._milestone("Ordered")
        rows = [
            MilestoneAssignees.objects.create(milestone=m, user=p, team=self.team)
            for p in (self.people[3], self.people[1], self.people[4])
        ]
        MilestoneAssignees.objects.filter(pk=rows[0].pk).update(team=None)
        MilestoneAssignees.objects.filter(pk=rows[0].pk).update(team=self.team)
        (item,) = self._list().json()["milestones"]
        self.assertEqual(
            [a["userId"] for a in item["assignees"]],
            [str(self.people[3].id), str(self.people[1].id), str(self.people[4].id)],
        )


# Captured from the endpoint on unmodified `main` for `build_fixture` above.
EXPECTED_KEY_ORDER = [
    "milestoneId",
    "taskId",
    "displayId",
    "chatType",
    "chatId",
    "threadId",
    "projectId",
    "teamId",
    "sprintId",
    "folderId",
    "reporterId",
    "reporter",
    "title",
    "description",
    "status",
    "statusCode",
    "priority",
    "priorityCode",
    "effortLevel",
    "effortLevelCode",
    "startDate",
    "dueDate",
    "tags",
    "links",
    "customFieldValues",
    "collaborators",
    "isDeleted",
    "tsCreatedAt",
    "tsUpdatedAt",
    "assignees",
    "tasksTotal",
    "tasksClosed",
]
EXPECTED_MILESTONES = [
    {
        "milestoneId": "milestoneId#1",
        "taskId": "taskId#1",
        "displayId": "GEN-1",
        "chatType": None,
        "chatId": None,
        "threadId": None,
        "projectId": "projectId#1",
        "teamId": "teamId#1",
        "sprintId": None,
        "folderId": None,
        "reporterId": "reporterId#1",
        "reporter": {
            "userId": "userId#1",
            "username": "testuser",
            "firstName": None,
            "lastName": None,
            "email": "test@example.com",
            "profileImageUrl": None,
        },
        "title": "Alpha",
        "description": None,
        "status": "Open",
        "statusCode": None,
        "priority": None,
        "priorityCode": None,
        "effortLevel": None,
        "effortLevelCode": None,
        "startDate": None,
        "dueDate": None,
        "tags": None,
        "links": None,
        "customFieldValues": {},
        "collaborators": [
            {
                "userId": "userId#2",
                "username": "person3",
                "firstName": None,
                "lastName": None,
                "email": "person3@example.com",
                "profileImageUrl": None,
            }
        ],
        "isDeleted": False,
        "tsCreatedAt": "2026-01-01T12:00:00Z",
        "tsUpdatedAt": "2026-01-01T12:03:00Z",
        "assignees": [
            {
                "userId": "userId#3",
                "username": "person0",
                "firstName": None,
                "lastName": None,
                "email": "person0@example.com",
                "profileImageUrl": None,
            },
            {
                "userId": "userId#4",
                "username": "person1",
                "firstName": None,
                "lastName": None,
                "email": "person1@example.com",
                "profileImageUrl": None,
            },
            {
                "userId": "userId#5",
                "username": "person2",
                "firstName": None,
                "lastName": None,
                "email": "person2@example.com",
                "profileImageUrl": None,
            },
        ],
        "tasksTotal": 5,
        "tasksClosed": 2,
    },
    {
        "milestoneId": "milestoneId#2",
        "taskId": "taskId#2",
        "displayId": "GEN-8",
        "chatType": 2,
        "chatId": "chat-1",
        "threadId": "thread-1",
        "projectId": "projectId#1",
        "teamId": "teamId#1",
        "sprintId": None,
        "folderId": None,
        "reporterId": "reporterId#2",
        "reporter": {
            "userId": "userId#3",
            "username": "person0",
            "firstName": None,
            "lastName": None,
            "email": "person0@example.com",
            "profileImageUrl": None,
        },
        "title": "Beta",
        "description": [{"type": "paragraph", "content": "Why this exists"}],
        "status": "Open",
        "statusCode": None,
        "priority": "High",
        "priorityCode": 3,
        "effortLevel": "Medium",
        "effortLevelCode": 2,
        "startDate": "2026-01-01",
        "dueDate": "2026-01-31",
        "tags": ["backend", "perf"],
        "links": [{"id": "l1", "url": "https://example.com/spec", "title": "Spec"}],
        "customFieldValues": {"f1": "x"},
        "collaborators": [],
        "isDeleted": False,
        "tsCreatedAt": "2026-01-01T12:00:00Z",
        "tsUpdatedAt": "2026-01-01T12:02:00Z",
        "assignees": [],
        "tasksTotal": 0,
        "tasksClosed": 0,
    },
    {
        "milestoneId": "milestoneId#3",
        "taskId": "taskId#3",
        "displayId": "GEN-9",
        "chatType": None,
        "chatId": None,
        "threadId": None,
        "projectId": "projectId#1",
        "teamId": "teamId#1",
        "sprintId": None,
        "folderId": None,
        "reporterId": None,
        "reporter": None,
        "title": "Gamma",
        "description": None,
        "status": "Open",
        "statusCode": None,
        "priority": None,
        "priorityCode": None,
        "effortLevel": None,
        "effortLevelCode": None,
        "startDate": None,
        "dueDate": None,
        "tags": None,
        "links": None,
        "customFieldValues": {},
        "collaborators": [],
        "isDeleted": False,
        "tsCreatedAt": "2026-01-01T12:00:00Z",
        "tsUpdatedAt": "2026-01-01T12:01:00Z",
        "assignees": [
            {
                "userId": "userId#6",
                "username": "person4",
                "firstName": None,
                "lastName": None,
                "email": "person4@example.com",
                "profileImageUrl": None,
            }
        ],
        "tasksTotal": 2,
        "tasksClosed": 1,
    },
    {
        "milestoneId": "milestoneId#4",
        "taskId": "taskId#4",
        "displayId": "GEN-12",
        "chatType": None,
        "chatId": None,
        "threadId": None,
        "projectId": "projectId#1",
        "teamId": "teamId#1",
        "sprintId": None,
        "folderId": None,
        "reporterId": "reporterId#3",
        "reporter": {
            "userId": "userId#7",
            "username": "person5",
            "firstName": None,
            "lastName": None,
            "email": "person5@example.com",
            "profileImageUrl": None,
        },
        "title": "Delta",
        "description": None,
        "status": "Open",
        "statusCode": None,
        "priority": None,
        "priorityCode": None,
        "effortLevel": None,
        "effortLevelCode": None,
        "startDate": None,
        "dueDate": None,
        "tags": None,
        "links": None,
        "customFieldValues": {},
        "collaborators": [
            {
                "userId": "userId#4",
                "username": "person1",
                "firstName": None,
                "lastName": None,
                "email": "person1@example.com",
                "profileImageUrl": None,
            }
        ],
        "isDeleted": False,
        "tsCreatedAt": "2026-01-01T12:00:00Z",
        "tsUpdatedAt": "2026-01-01T12:00:00Z",
        "assignees": [
            {
                "userId": "userId#1",
                "username": "testuser",
                "firstName": None,
                "lastName": None,
                "email": "test@example.com",
                "profileImageUrl": None,
            }
        ],
        "tasksTotal": 1,
        "tasksClosed": 1,
    },
]
