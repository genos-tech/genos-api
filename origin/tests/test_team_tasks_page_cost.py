"""`getTeamTasksPage` is cheap to build, not just cheap to query (MIP-966).

The page took ~1.5 s for up to 500 rows while the database accounted for 0.27 s
of it. The rest was work done per row and per request that the response never
used:

  * every row loaded the whole task body (`content`, a JSON document) and the
    page parsed it;
  * every row joined the assignee and the team and decrypted their personal
    fields (email, phone number, country, location, about-me; the team's
    email), of which the row reads one: the assignee's email;
  * a `task_tags` prefetch nobody read;
  * the team membership was checked three times (the view, `_team_task_scope`,
    and `is_guest` inside it, which asks `is_team_member` again).

What is pinned here: the response does not change by a byte (the expected value
below was captured from unmodified `main`, ids replaced by the order in which
they first appear because sequences and UUIDs differ between runs), the query
count does not grow with the number of tasks, the membership is read once, and
paging still covers every row exactly once.
"""

import json
from datetime import date, datetime, timezone

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

PAGE = "/api/v2/task/getTeamTasksPage/"
OLD = "/api/v2/task/getTeamTasks/"

FROZEN = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

# Keys whose value is a database id (integers, strings of integers, UUIDs).
ID_KEYS = (
    "id",
    "assigneeId",
    "parentTaskId",
    "rootTaskId",
    "teamId",
    "projectId",
    "milestoneId",
    "sprintId",
)


def normalise_ids(rows):
    seen: dict[str, dict[object, int]] = {}

    def token(key, value):
        ids = seen.setdefault(key, {})
        return f"{key}#{ids.setdefault(value, len(ids) + 1)}"

    def display_id(value):
        # "<code>-<n>" is stable; a task with no project falls back to "#<task id>"
        return "#" + token("displayId", value[1:]) if value.startswith("#") else value

    return [
        {
            k: (
                token(k, v)
                if k in ID_KEYS and v is not None
                else display_id(v)
                if k == "displayId"
                else v
            )
            for k, v in row.items()
        }
        for row in rows
    ]


class TeamTasksPageBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        sysuser = User.objects.create_user(
            username="ttcsys", email="ttcsys@example.com", password="pw-12345-pw"
        )
        sysuser.is_system_user = True
        sysuser.save(update_fields=["is_system_user"])
        self.project_a = ProjectMaster.objects.create(
            team=self.team,
            project_name="Alpha",
            code="AAA",
            owner=self.user,
            project_system_user=sysuser,
        )
        self.project_b = ProjectMaster.objects.create(
            team=self.team,
            project_name="Beta",
            code="BBB",
            owner=self.user,
            project_system_user=sysuser,
        )
        self.hidden = ProjectMaster.objects.create(
            team=self.team,
            project_name="Hidden",
            code="HID",
            owner=self.user2,
            project_system_user=sysuser,
        )
        for project in (self.project_a, self.project_b):
            ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
            ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
        ProjectMembers.objects.create(team=self.team, project=self.hidden, attendee=self.user2)
        self.people = []
        for i in range(6):
            person = User.objects.create_user(
                username=f"ttc{i}", email=f"ttc{i}@example.com", password="pw-12345-pw"
            )
            # the personal fields stored encrypted, which a join used to decrypt per row
            person.phone_number = f"+8190000000{i}"
            person.base_country = "Japan"
            person.current_location = f"Tokyo {i}"
            person.about_me = f"About person {i}"
            person.profile_image_file_name = f"avatar-{i}.png"
            person.save()
            self.people.append(person)
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project_a, reporter=self.user, title="M", status="Open"
        )
        self.sprint = Sprint.objects.create(
            team=self.team,
            project=self.project_a,
            name="S1",
            sequence_number=1,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 14),
        )
        self.authenticate(self.user)
        self.made = 0
        self.first_task_id = None

    # ------------------------------------------------------------------ build
    def make_tasks(self, n):
        """`n` more tasks of every shape the row builder has a branch for."""
        for _ in range(n):
            i = self.made
            self.made += 1
            project = (self.project_a, self.project_b, None)[i % 3]
            task = TaskMaster.objects.create(
                team=self.team,
                project=project,
                reporter=self.user,
                title=f"task {i:03d}",
                status=("Open", "WIP", "Closed", "Blocked")[i % 4],
                priority=("High", "Normal", None)[i % 3],
                effort_level=("Low", "Medium")[i % 2],
                assignee=self.people[i % 7] if i % 7 < 6 else None,
                tags=[{"tagName": f"tag{i % 4}", "tagId": i % 4}] if i % 5 < 3 else None,
                due_date=date(2020, 1, 1 + i % 20) if i % 5 == 0 else None,
                start_date=date(2019, 12, 1 + i % 20) if i % 6 == 0 else None,
                links=[{"id": "l", "url": "https://example.com", "title": "PR"}]
                if i % 4 == 0
                else None,
                custom_field_values={"f1": f"v{i}"} if i % 3 == 1 else None,
                thread_id=f"thread-{i}" if i % 8 == 0 else None,
                is_deleted=i % 10 == 9,
                is_milestone=i % 11 == 10,
                milestone=self.milestone if i % 9 == 4 else None,
                sprint=self.sprint if i % 6 == 5 else None,
                parent_task_id=self.first_task_id if i % 7 == 3 else None,
                # a task body: the part of the row the response never uses
                content=[
                    {"type": "paragraph", "content": f"line {j} of task {i} " + "x" * 60}
                    for j in range(40)
                ],
            )
            if self.first_task_id is None:
                self.first_task_id = task.task_id
        TaskMaster.objects.all().update(ts_created_at=FROZEN, ts_updated_at=FROZEN)
        # rows that must never be listed
        TaskMaster.objects.create(
            team=self.team,
            project=self.hidden,
            title="in a project I am not in",
            status="Open",
            reporter=self.user,
        )
        TaskMaster.objects.create(
            team=self.team,
            project=self.project_a,
            title="scaffold",
            status="Open",
            reporter=self.user,
            is_init_task=True,
        )

    # ----------------------------------------------------------------- request
    def fetch(self, url=PAGE, **params):
        res = self.client.get(url, {"team_id": str(self.team.team_id), **params})
        self.assertEqual(res.status_code, 200, res.content)
        return res

    def queries(self, **params) -> int:
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            self.fetch(**params)
        return len(ctx)

    def all_pages(self, limit):
        rows, cursor = [], None
        for _ in range(100):
            body = self.fetch(limit=limit, **({"cursor": cursor} if cursor else {})).json()
            rows += body["tasks"]
            if not body["has_more"]:
                return rows
            cursor = body["next_cursor"]
        self.fail("paging did not end")


class TeamTasksPageResponseTests(TeamTasksPageBase):
    def test_the_response_is_the_same_apart_from_soft_deleted_tasks(self):
        """The wire format is pinned against what `main` returned (MIP-966); the one
        deliberate change since is that soft-deleted tasks are no longer listed
        (MIP-984). Ids are numbered by order of appearance, so with a row gone the
        rows are compared by title, field by field, ids left out."""
        self.make_tasks(14)
        body = json.loads(self.fetch().content)

        self.assertEqual(list(body), ["tasks", "has_more", "next_cursor"])
        self.assertEqual((body["has_more"], body["next_cursor"]), (False, None))
        rows = body["tasks"]
        # not the other project's task, not the scaffold row, not the deleted one
        self.assertEqual(len(rows), 13)
        got = {r["title"]: r for r in normalise_ids(rows)}
        want = {r["title"]: r for r in EXPECTED_ROWS if r["title"] != DELETED_TITLE}
        self.assertEqual(set(got), set(want))
        skip = set(ID_KEYS) | {"displayId"}
        for title, row in want.items():
            self.assertEqual(
                {k: v for k, v in got[title].items() if k not in skip},
                {k: v for k, v in row.items() if k not in skip},
                title,
            )
        # the key order is part of the wire format too
        self.assertEqual(list(rows[0]), EXPECTED_KEY_ORDER)

    def test_soft_deleted_tasks_are_not_listed(self):
        """MIP-984: a soft-deleted task used to be sent like a live one, with
        nothing on the row to tell them apart. Neither route sends it now."""
        self.make_tasks(14)
        deleted = TaskMaster.objects.get(is_deleted=True)
        self.assertEqual(deleted.title, DELETED_TITLE)
        for url in (PAGE, OLD):
            body = self.fetch(url).json()
            rows = body["tasks"] if url == PAGE else body
            self.assertNotIn(str(deleted.task_id), {r["id"] for r in rows}, url)
            self.assertNotIn(DELETED_TITLE, {r["title"] for r in rows}, url)
        # ...and a page boundary that falls on it does not lose or repeat others
        titles = [r["title"] for r in self.all_pages(limit=3)]
        self.assertEqual(len(titles), 13)
        self.assertEqual(len(set(titles)), 13)

    def test_the_unpaged_route_gives_the_same_rows(self):
        self.make_tasks(14)
        paged = {r["id"]: r for r in self.fetch().json()["tasks"]}
        old = {r["id"]: r for r in self.fetch(OLD).json()}
        self.assertEqual(paged, old)

    def test_pages_add_up_to_the_whole_list(self):
        self.make_tasks(14)
        whole = self.fetch().json()["tasks"]
        self.assertEqual(self.all_pages(limit=3), whole)
        self.assertEqual(self.all_pages(limit=14), whole)
        self.assertEqual(self.all_pages(limit=5), whole)

    def test_a_team_member_without_a_project_sees_the_orphans_too(self):
        """The orphan clause is the part of the scope that depends on the
        membership check, so check both sides of it."""
        self.make_tasks(6)
        orphans = {str(t.task_id) for t in TaskMaster.objects.filter(project__isnull=True)}
        self.assertTrue(orphans)
        self.authenticate(self.user2)
        ids = {r["id"] for r in self.fetch().json()["tasks"]}
        self.assertTrue(orphans <= ids)

        stranger = User.objects.create_user(
            username="ttcstranger", email="ttcstranger@example.com", password="pw-12345-pw"
        )
        self.authenticate(stranger)
        res = self.client.get(PAGE, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 404)

    def test_a_project_guest_who_is_not_a_team_member_is_refused(self):
        """A person with a project but no team membership (an external guest)
        is refused by this route, as before."""
        guest = User.objects.create_user(
            username="ttcguest", email="ttcguest@example.com", password="pw-12345-pw"
        )
        ProjectMembers.objects.create(team=self.team, project=self.project_a, attendee=guest)
        self.make_tasks(6)
        self.authenticate(guest)
        res = self.client.get(PAGE, {"team_id": str(self.team.team_id)})
        # a guest is not a team member, so the route refuses them as it always did
        self.assertEqual(res.status_code, 404)


class AssignedTasksScopeTests(TeamTasksPageBase):
    """`getMyAssignedTasks` shares `_team_task_scope` but has NO membership check
    of its own, so it must keep asking: `known_member=True` is only for callers
    that just verified the caller. A task with no project belongs to the team, not
    to a project, so it may be listed only to a member of the team."""

    URL = "/api/v2/task/getMyAssignedTasks/"

    def _titles(self, user):
        self.authenticate(user)
        res = self.client.get(self.URL, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200, res.content)
        return {r["title"] for r in res.json()}

    def test_a_non_member_is_not_handed_the_teams_projectless_tasks(self):
        # (Only the non-member side is asserted: a MEMBER's own projectless task makes
        # this route answer 500 today, `t.project.project_id` on a null project. That
        # is a separate bug and not part of this change.)
        outsider = User.objects.create_user(
            username="ttcout", email="ttcout@example.com", password="pw-12345-pw"
        )
        TaskMaster.objects.create(
            team=self.team,
            project=None,
            title="orphan assigned to an outsider",
            status="Open",
            reporter=self.user,
            assignee=outsider,
        )

        self.assertEqual(self._titles(outsider), set())


class TeamTasksPageCostTests(TeamTasksPageBase):
    # Budget for one page, whatever the number of rows (the caller owns the team;
    # a member who does not pays one more per membership check):
    #   1 the caller   2 the team exists   3 the caller is a member (the view's check)
    #   4-5 the projects they belong to / own   6 the projects shared with their team
    #   7 the tasks, with assignee and project joined
    # Before: 10 (two more membership checks inside `_team_task_scope`, a `task_tags` prefetch).
    BUDGET = 7

    def test_the_query_count_does_not_grow_with_the_number_of_tasks(self):
        self.make_tasks(20)
        small = self.queries()
        self.make_tasks(20)
        large = self.queries()

        live = (
            40 - TaskMaster.objects.filter(is_deleted=True).count()
        )  # deleted ones are not listed
        self.assertEqual(len(self.fetch().json()["tasks"]), live)
        self.assertEqual(large, small, "queries grow with the number of tasks")
        self.assertEqual(small, self.BUDGET)

    def test_the_membership_is_read_once(self):
        """The view's own check, and nothing else: `_team_task_scope` used to
        ask again, and so did `is_guest` inside it."""
        self.make_tasks(3)
        self.authenticate(self.user2)  # a member who is not the owner: two reads per check
        table = TeamMembers._meta.db_table
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            self.fetch()
        reads = [q["sql"] for q in ctx.captured_queries if f'"{table}"' in q["sql"]]
        self.assertEqual(len(reads), 1, reads)

    def test_a_page_does_not_read_task_bodies_or_prefetch_tags(self):
        self.make_tasks(3)
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            self.fetch()
        sql = " ".join(q["sql"] for q in ctx.captured_queries)
        self.assertNotIn('"origin_taskmaster"."content"', sql)
        self.assertNotIn("tasktag", sql.lower())


# Captured from the endpoint on unmodified `main` for `make_tasks(14)` above.
EXPECTED_KEY_ORDER = [
    "id",
    "displayId",
    "title",
    "priority",
    "effortLevel",
    "createdDate",
    "updatedAt",
    "dueDate",
    "startDate",
    "daysLeft",
    "status",
    "assigneeId",
    "assigneeEmail",
    "assigneeName",
    "assigneeImgPath",
    "parentTaskId",
    "rootTaskId",
    "threadId",
    "tags",
    "concatTags",
    "teamId",
    "projectId",
    "isMilestone",
    "milestoneId",
    "sprintId",
    "links",
    "customFieldValues",
]
DELETED_TITLE = "task 009"  # the one soft-deleted task `make_tasks(14)` creates
EXPECTED_ROWS = [
    {
        "id": "id#1",
        "displayId": "AAA-1",
        "title": "task 000",
        "priority": "High",
        "effortLevel": "Low",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": "2020-01-01",
        "startDate": "2019-12-01",
        "daysLeft": -1,
        "status": "Open",
        "assigneeId": "assigneeId#1",
        "assigneeEmail": "ttc0@example.com",
        "assigneeName": "ttc0",
        "assigneeImgPath": "avatar-0.png",
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#1",
        "threadId": "thread-0",
        "tags": [{"tagId": 0, "tagName": "tag0"}],
        "concatTags": "/tag0/",
        "teamId": "teamId#1",
        "projectId": "projectId#1",
        "isMilestone": False,
        "milestoneId": None,
        "sprintId": None,
        "links": [{"id": "l", "url": "https://example.com", "title": "PR"}],
        "customFieldValues": {},
    },
    {
        "id": "id#2",
        "displayId": "BBB-1",
        "title": "task 001",
        "priority": "Normal",
        "effortLevel": "Medium",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": None,
        "startDate": None,
        "daysLeft": None,
        "status": "WIP",
        "assigneeId": "assigneeId#2",
        "assigneeEmail": "ttc1@example.com",
        "assigneeName": "ttc1",
        "assigneeImgPath": "avatar-1.png",
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#2",
        "threadId": None,
        "tags": [{"tagId": 1, "tagName": "tag1"}],
        "concatTags": "/tag1/",
        "teamId": "teamId#1",
        "projectId": "projectId#2",
        "isMilestone": False,
        "milestoneId": None,
        "sprintId": None,
        "links": [],
        "customFieldValues": {"f1": "v1"},
    },
    {
        "id": "id#3",
        "displayId": "#displayId#1",
        "title": "task 002",
        "priority": None,
        "effortLevel": "Low",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": None,
        "startDate": None,
        "daysLeft": None,
        "status": "Closed",
        "assigneeId": "assigneeId#3",
        "assigneeEmail": "ttc2@example.com",
        "assigneeName": "ttc2",
        "assigneeImgPath": "avatar-2.png",
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#3",
        "threadId": None,
        "tags": [{"tagId": 2, "tagName": "tag2"}],
        "concatTags": "/tag2/",
        "teamId": "teamId#1",
        "projectId": None,
        "isMilestone": False,
        "milestoneId": None,
        "sprintId": None,
        "links": [],
        "customFieldValues": {},
    },
    {
        "id": "id#4",
        "displayId": "AAA-2",
        "title": "task 003",
        "priority": "High",
        "effortLevel": "Medium",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": None,
        "startDate": None,
        "daysLeft": None,
        "status": "Blocked",
        "assigneeId": "assigneeId#4",
        "assigneeEmail": "ttc3@example.com",
        "assigneeName": "ttc3",
        "assigneeImgPath": "avatar-3.png",
        "parentTaskId": "parentTaskId#1",
        "rootTaskId": "rootTaskId#1",
        "threadId": None,
        "tags": [],
        "concatTags": None,
        "teamId": "teamId#1",
        "projectId": "projectId#1",
        "isMilestone": False,
        "milestoneId": None,
        "sprintId": None,
        "links": [],
        "customFieldValues": {},
    },
    {
        "id": "id#5",
        "displayId": "BBB-2",
        "title": "task 004",
        "priority": "Normal",
        "effortLevel": "Low",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": None,
        "startDate": None,
        "daysLeft": None,
        "status": "Open",
        "assigneeId": "assigneeId#5",
        "assigneeEmail": "ttc4@example.com",
        "assigneeName": "ttc4",
        "assigneeImgPath": "avatar-4.png",
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#4",
        "threadId": None,
        "tags": [],
        "concatTags": None,
        "teamId": "teamId#1",
        "projectId": "projectId#2",
        "isMilestone": False,
        "milestoneId": "milestoneId#1",
        "sprintId": None,
        "links": [{"id": "l", "url": "https://example.com", "title": "PR"}],
        "customFieldValues": {"f1": "v4"},
    },
    {
        "id": "id#6",
        "displayId": "#displayId#2",
        "title": "task 005",
        "priority": None,
        "effortLevel": "Medium",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": "2020-01-06",
        "startDate": None,
        "daysLeft": -1,
        "status": "WIP",
        "assigneeId": "assigneeId#6",
        "assigneeEmail": "ttc5@example.com",
        "assigneeName": "ttc5",
        "assigneeImgPath": "avatar-5.png",
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#5",
        "threadId": None,
        "tags": [{"tagId": 1, "tagName": "tag1"}],
        "concatTags": "/tag1/",
        "teamId": "teamId#1",
        "projectId": None,
        "isMilestone": False,
        "milestoneId": None,
        "sprintId": "sprintId#1",
        "links": [],
        "customFieldValues": {},
    },
    {
        "id": "id#7",
        "displayId": "AAA-3",
        "title": "task 006",
        "priority": "High",
        "effortLevel": "Low",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": None,
        "startDate": "2019-12-07",
        "daysLeft": None,
        "status": "Closed",
        "assigneeId": None,
        "assigneeEmail": None,
        "assigneeName": None,
        "assigneeImgPath": None,
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#6",
        "threadId": None,
        "tags": [{"tagId": 2, "tagName": "tag2"}],
        "concatTags": "/tag2/",
        "teamId": "teamId#1",
        "projectId": "projectId#1",
        "isMilestone": False,
        "milestoneId": None,
        "sprintId": None,
        "links": [],
        "customFieldValues": {},
    },
    {
        "id": "id#8",
        "displayId": "BBB-3",
        "title": "task 007",
        "priority": "Normal",
        "effortLevel": "Medium",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": None,
        "startDate": None,
        "daysLeft": None,
        "status": "Blocked",
        "assigneeId": "assigneeId#1",
        "assigneeEmail": "ttc0@example.com",
        "assigneeName": "ttc0",
        "assigneeImgPath": "avatar-0.png",
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#7",
        "threadId": None,
        "tags": [{"tagId": 3, "tagName": "tag3"}],
        "concatTags": "/tag3/",
        "teamId": "teamId#1",
        "projectId": "projectId#2",
        "isMilestone": False,
        "milestoneId": None,
        "sprintId": None,
        "links": [],
        "customFieldValues": {"f1": "v7"},
    },
    {
        "id": "id#9",
        "displayId": "#displayId#3",
        "title": "task 008",
        "priority": None,
        "effortLevel": "Low",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": None,
        "startDate": None,
        "daysLeft": None,
        "status": "Open",
        "assigneeId": "assigneeId#2",
        "assigneeEmail": "ttc1@example.com",
        "assigneeName": "ttc1",
        "assigneeImgPath": "avatar-1.png",
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#8",
        "threadId": "thread-8",
        "tags": [],
        "concatTags": None,
        "teamId": "teamId#1",
        "projectId": None,
        "isMilestone": False,
        "milestoneId": None,
        "sprintId": None,
        "links": [{"id": "l", "url": "https://example.com", "title": "PR"}],
        "customFieldValues": {},
    },
    {
        "id": "id#10",
        "displayId": "AAA-4",
        "title": "task 009",
        "priority": "High",
        "effortLevel": "Medium",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": None,
        "startDate": None,
        "daysLeft": None,
        "status": "WIP",
        "assigneeId": "assigneeId#3",
        "assigneeEmail": "ttc2@example.com",
        "assigneeName": "ttc2",
        "assigneeImgPath": "avatar-2.png",
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#9",
        "threadId": None,
        "tags": [],
        "concatTags": None,
        "teamId": "teamId#1",
        "projectId": "projectId#1",
        "isMilestone": False,
        "milestoneId": None,
        "sprintId": None,
        "links": [],
        "customFieldValues": {},
    },
    {
        "id": "id#11",
        "displayId": "BBB-4",
        "title": "task 010",
        "priority": "Normal",
        "effortLevel": "Low",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": "2020-01-11",
        "startDate": None,
        "daysLeft": -1,
        "status": "Closed",
        "assigneeId": "assigneeId#4",
        "assigneeEmail": "ttc3@example.com",
        "assigneeName": "ttc3",
        "assigneeImgPath": "avatar-3.png",
        "parentTaskId": "parentTaskId#1",
        "rootTaskId": "rootTaskId#1",
        "threadId": None,
        "tags": [{"tagId": 2, "tagName": "tag2"}],
        "concatTags": "/tag2/",
        "teamId": "teamId#1",
        "projectId": "projectId#2",
        "isMilestone": True,
        "milestoneId": None,
        "sprintId": None,
        "links": [],
        "customFieldValues": {"f1": "v10"},
    },
    {
        "id": "id#12",
        "displayId": "#displayId#4",
        "title": "task 011",
        "priority": None,
        "effortLevel": "Medium",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": None,
        "startDate": None,
        "daysLeft": None,
        "status": "Blocked",
        "assigneeId": "assigneeId#5",
        "assigneeEmail": "ttc4@example.com",
        "assigneeName": "ttc4",
        "assigneeImgPath": "avatar-4.png",
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#10",
        "threadId": None,
        "tags": [{"tagId": 3, "tagName": "tag3"}],
        "concatTags": "/tag3/",
        "teamId": "teamId#1",
        "projectId": None,
        "isMilestone": False,
        "milestoneId": None,
        "sprintId": "sprintId#1",
        "links": [],
        "customFieldValues": {},
    },
    {
        "id": "id#13",
        "displayId": "AAA-5",
        "title": "task 012",
        "priority": "High",
        "effortLevel": "Low",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": None,
        "startDate": "2019-12-13",
        "daysLeft": None,
        "status": "Open",
        "assigneeId": "assigneeId#6",
        "assigneeEmail": "ttc5@example.com",
        "assigneeName": "ttc5",
        "assigneeImgPath": "avatar-5.png",
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#11",
        "threadId": None,
        "tags": [{"tagId": 0, "tagName": "tag0"}],
        "concatTags": "/tag0/",
        "teamId": "teamId#1",
        "projectId": "projectId#1",
        "isMilestone": False,
        "milestoneId": None,
        "sprintId": None,
        "links": [{"id": "l", "url": "https://example.com", "title": "PR"}],
        "customFieldValues": {},
    },
    {
        "id": "id#14",
        "displayId": "BBB-5",
        "title": "task 013",
        "priority": "Normal",
        "effortLevel": "Medium",
        "createdDate": "2026-01-01",
        "updatedAt": "2026-01-01 12:00:00+00:00",
        "dueDate": None,
        "startDate": None,
        "daysLeft": None,
        "status": "WIP",
        "assigneeId": None,
        "assigneeEmail": None,
        "assigneeName": None,
        "assigneeImgPath": None,
        "parentTaskId": None,
        "rootTaskId": "rootTaskId#12",
        "threadId": None,
        "tags": [],
        "concatTags": None,
        "teamId": "teamId#1",
        "projectId": "projectId#2",
        "isMilestone": False,
        "milestoneId": "milestoneId#1",
        "sprintId": None,
        "links": [],
        "customFieldValues": {"f1": "v13"},
    },
]
