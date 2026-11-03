"""`GET /api/v2/project/projects/`: a constant number of queries (MIP-966).

The sidebar loads this list on every boot and every team switch, so its query
count is paid by everyone. It used to grow with the data:

  * one query per tag row (`project_tag.project` loaded the project that the
    row already names by id);
  * one query per project (`project.project_system_user.id` loaded the user to
    read an id the project row already holds);

and it spent about seven queries on a cache HIT, because the two membership
checks (one of them run twice) and the lookup of projects shared from other
teams all ran before the 60-second cache was consulted.

Three kinds of test:

  * query counts: N projects against 2N, for a miss and for a hit, with the
    budget pinned so a creeping extra query is a visible failure;
  * the payload: the exact JSON for a fixed fixture. The expected value is
    written independently of the view and was checked against the unmodified
    view first, so a change that merely makes the list cheaper cannot change
    what the sidebar receives;
  * the order of the guards: the cache must stay BELOW the membership check
    (a removed member must not read the cached list), and the lookup of
    shared projects must sit BELOW the cache.
"""

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.common.team_models import ExternalGrant, TeamMembers
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectLabelAssignment,
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
)
from origin.services.external_grants import add_external_participants
from origin.services.member_roles import EDITOR
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

PROJECTS = "/api/v2/project/projects/"

# Query budgets for a plain team member who is not the owner, counted through
# the test client the way production `db_n` counts (authentication included).
#
#   miss  = 1 auth + 2 membership (owner check, member check) + 1 team-not-deleted
#           + 1 shared-project grants + 1 projects + 1 tags + 1 labels
#   hit   = 1 auth + 2 membership + 1 team-not-deleted
MISS_BUDGET = 8
HIT_BUDGET = 4


def _stamp(model, pk_field, pk, when):
    """`.update()` skips `auto_now`: how a test fixes the list's sort order."""
    model.objects.filter(**{pk_field: pk}).update(ts_updated_at=when)


class ProjectListQueryCountTests(BaseAPITestCase):
    """N projects with tags, labels and their own system users, against 2N."""

    N = 4

    def setUp(self):
        super().setUp()
        self.authenticate(self.user2)  # a plain member: the common caller
        self.label = ProjectLabel.objects.create(
            team=self.team, name="Client", color="#111111", text_color="#ffffff"
        )
        self.made = 0

    def _add_projects(self, count):
        for _ in range(count):
            self.made += 1
            system_user = User.objects.create_user(
                username=f"sys{self.made}", email=f"sys{self.made}@example.com", password="pw"
            )
            project = ProjectMaster.objects.create(
                team=self.team,
                project_name=f"Project {self.made}",
                owner=self.user,
                project_system_user=system_user,
            )
            ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
            for n in (1, 2):
                ProjectTags.objects.create(
                    team=self.team,
                    project=project,
                    tag_id=n,
                    tag_name=f"tag{n}",
                    tag_color="#ff0000",
                    tag_text_color="#ffffff",
                )
            ProjectLabelAssignment.objects.create(label=self.label, project=project)

    def _queries(self, *, warm):
        """The queries one request costs. `warm` measures a cache HIT."""
        cache.clear()
        params = {"team_id": str(self.team.team_id), "attendee_id": str(self.user2.id)}
        if warm:
            self.assertEqual(self.client.get(PROJECTS, params).status_code, 200)
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.get(PROJECTS, params)
        self.assertEqual(res.status_code, 200, res.data)
        return len(ctx), [q["sql"] for q in ctx.captured_queries], len(res.data)

    def _show(self, sqls):
        return "\n".join(f"  {i + 1}. {sql[:140]}" for i, sql in enumerate(sqls))

    def test_a_miss_costs_the_same_for_n_and_2n_projects(self):
        self._add_projects(self.N)
        small, small_sql, small_rows = self._queries(warm=False)
        self._add_projects(self.N)
        large, large_sql, large_rows = self._queries(warm=False)

        self.assertEqual((small_rows, large_rows), (self.N, 2 * self.N))
        self.assertEqual(small, large, f"grew with the data:\n{self._show(large_sql)}")
        self.assertEqual(large, MISS_BUDGET, f"miss budget:\n{self._show(large_sql)}")

    def test_a_hit_costs_the_same_for_n_and_2n_projects(self):
        self._add_projects(self.N)
        small, small_sql, _ = self._queries(warm=True)
        self._add_projects(self.N)
        large, large_sql, _ = self._queries(warm=True)

        self.assertEqual(small, large, f"grew with the data:\n{self._show(large_sql)}")
        self.assertEqual(large, HIT_BUDGET, f"hit budget:\n{self._show(large_sql)}")

    def test_a_hit_does_not_read_the_projects_tags_or_labels(self):
        self._add_projects(self.N)
        _, sqls, _ = self._queries(warm=True)
        joined = "\n".join(sqls)
        for table in ("origin_projectmaster", "origin_projecttags", "origin_projectlabel"):
            self.assertNotIn(table, joined, f"a hit read {table}:\n{self._show(sqls)}")

    def test_a_project_whose_system_user_is_gone_lists_with_a_null_id(self):
        """`project_system_user` is nullable (SET_NULL when the user is deleted).

        The old `project.project_system_user.id` raised on such a row and took
        the whole list down with it; the row now reports `systemUserId: null`.
        """
        self._add_projects(1)
        ProjectMaster.objects.update(project_system_user=None)
        cache.clear()

        res = self.client.get(
            PROJECTS, {"team_id": str(self.team.team_id), "attendee_id": str(self.user2.id)}
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual([p["systemUserId"] for p in res.data], [None])


class ProjectListSharedQueryCountTests(CrossTeamTestCase):
    """Projects shared in from another team add no queries per project."""

    shared = 0

    def _share_more(self, count):
        for _ in range(count):
            self.shared += 1
            project = ProjectMaster.objects.create(
                team=self.team_a,
                project_name=f"Shared {self.shared}",
                owner=self.a_owner,
                project_system_user=self.a_owner,
            )
            grant = self._active_grant(ExternalGrant.ObjectType.PROJECT, project.project_id, EDITOR)
            add_external_participants(grant, [self.b_owner.id], self.b_owner)

    def _miss_queries(self):
        cache.clear()
        self.authenticate(self.b_owner)
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.get(
                PROJECTS,
                {"team_id": str(self.team_b.team_id), "attendee_id": str(self.b_owner.id)},
            )
        self.assertEqual(res.status_code, 200, res.data)
        return len(ctx), len(res.data), "\n".join(q["sql"][:120] for q in ctx.captured_queries)

    def test_the_count_does_not_grow_with_the_number_of_shared_projects(self):
        self._share_more(2)
        small, small_rows, _ = self._miss_queries()
        self._share_more(3)
        large, large_rows, sqls = self._miss_queries()

        self.assertEqual((small_rows, large_rows), (2, 5))
        self.assertEqual(small, large, sqls)


class ProjectListPayloadTests(CrossTeamTestCase):
    """The exact response, for a fixed fixture, miss and hit alike."""

    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()
        add_external_participants(self.grant, [self.b_owner.id], self.b_owner)

        # Team B's own projects. The first carries tags and a label so every
        # key has content; the second is private and nobody has joined it.
        self.b_label = ProjectLabel.objects.create(
            team=self.team_b, name="Client", color="#111111", text_color="#ffffff"
        )
        self.b_system = self._user("b_system")
        self.alpha = ProjectMaster.objects.create(
            team=self.team_b,
            project_name="Alpha",
            owner=self.b_owner,
            project_system_user=self.b_system,
        )
        self.beta = ProjectMaster.objects.create(
            team=self.team_b,
            project_name="Beta",
            owner=self.b_owner,
            project_system_user=self.b_owner,
            is_private=True,
        )
        ProjectMembers.objects.create(team=self.team_b, project=self.alpha, attendee=self.b_owner)
        ProjectMembers.objects.create(team=self.team_b, project=self.alpha, attendee=self.b_editor)
        ProjectLabelAssignment.objects.create(label=self.b_label, project=self.alpha)
        self.tags = [
            ProjectTags.objects.create(
                team=self.team_b,
                project=self.alpha,
                tag_id=n,
                tag_name=name,
                tag_color=color,
                tag_text_color="#ffffff",
            )
            for n, (name, color) in enumerate((("Bug", "#ff0000"), ("Idea", "#00ff00")), start=1)
        ]

        # A fixed order: the list is newest first, tags newest first.
        now = timezone.now()
        _stamp(ProjectMaster, "project_id", self.project.project_id, now - timedelta(minutes=1))
        _stamp(ProjectMaster, "project_id", self.alpha.project_id, now - timedelta(minutes=2))
        _stamp(ProjectMaster, "project_id", self.beta.project_id, now - timedelta(minutes=3))
        _stamp(ProjectTags, "id", self.tags[0].id, now - timedelta(minutes=5))
        _stamp(ProjectTags, "id", self.tags[1].id, now - timedelta(minutes=4))

    def _row(self, project, *, joined, system_user, tags=(), labels=(), external=False):
        row = {
            "projectId": project.project_id,
            "projectName": project.project_name,
            "projectTags": list(tags),
            "projectLabels": list(labels),
            "isPrivate": project.is_private,
            "isJoined": joined,
            "systemUserId": str(system_user.id),
        }
        if external:
            row.update(
                {
                    "isExternal": True,
                    "hostTeamId": str(self.team_a.team_id),
                    "hostTeamName": self.team_a.team_name,
                }
            )
        return row

    def _wire(self, user, team):
        cache.clear()
        self.authenticate(user)
        params = {"team_id": str(team.team_id), "attendee_id": str(user.id)}
        miss = self.client.get(PROJECTS, params)
        hit = self.client.get(PROJECTS, params)
        self.assertEqual((miss.status_code, hit.status_code), (200, 200))
        return json.loads(miss.content), json.loads(hit.content)

    ALPHA_TAGS = (
        {"tagName": "Idea", "tagColor": "#00ff00", "tagTextColor": "#ffffff"},
        {"tagName": "Bug", "tagColor": "#ff0000", "tagTextColor": "#ffffff"},
    )

    def _alpha(self, joined):
        labels = [
            {
                "labelId": self.b_label.label_id,
                "name": "Client",
                "color": "#111111",
                "textColor": "#ffffff",
            }
        ]
        return self._row(
            self.alpha,
            joined=joined,
            system_user=self.b_system,
            tags=self.ALPHA_TAGS,
            labels=labels,
        )

    def test_the_owner_sees_their_projects_and_the_shared_one_exactly(self):
        miss, hit = self._wire(self.b_owner, self.team_b)

        expected = [
            self._row(self.project, joined=True, system_user=self.a_owner, external=True),
            self._alpha(joined=True),
            self._row(self.beta, joined=False, system_user=self.b_owner),
        ]
        self.assertEqual(miss, expected)
        self.assertEqual([list(r) for r in miss], [list(r) for r in expected])  # key order
        self.assertEqual(hit, miss)

    def test_a_member_who_was_not_admitted_does_not_get_the_shared_project(self):
        miss, hit = self._wire(self.b_editor, self.team_b)

        expected = [
            self._alpha(joined=True),
            self._row(self.beta, joined=False, system_user=self.b_owner),
        ]
        self.assertEqual(miss, expected)
        self.assertEqual([list(r) for r in miss], [list(r) for r in expected])
        self.assertEqual(hit, miss)

    def test_a_guest_is_narrowed_to_the_project_they_were_admitted_to(self):
        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)

        miss, hit = self._wire(self.b_viewer, self.team_a)

        expected = [self._row(self.project, joined=True, system_user=self.a_owner)]
        self.assertEqual(miss, expected)
        self.assertEqual(hit, miss)

    def test_a_stranger_still_gets_a_404(self):
        self.authenticate(self.c_owner)
        res = self.client.get(
            PROJECTS, {"team_id": str(self.team_b.team_id), "attendee_id": str(self.c_owner.id)}
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data, {"error": "Team not found."})


class ProjectListGuardOrderTests(CrossTeamTestCase):
    """What the cheaper hit must not give up, and what it must stop doing."""

    SENTINEL = [{"projectId": 0, "projectName": "from the cache"}]

    def _key(self, user, team):
        return f"project:list:{team.team_id}:{user.id}"

    def _get(self, user, team):
        self.authenticate(user)
        return self.client.get(
            PROJECTS, {"team_id": str(team.team_id), "attendee_id": str(user.id)}
        )

    def test_a_removed_member_does_not_read_the_cached_list(self):
        cache.set(self._key(self.b_editor, self.team_b), self.SENTINEL, timeout=60)
        self.assertEqual(self._get(self.b_editor, self.team_b).data, self.SENTINEL)

        TeamMembers.objects.filter(team=self.team_b, attendee=self.b_editor).update(is_deleted=True)
        # The removal may or may not clear the key; the guard must not rely on it.
        cache.set(self._key(self.b_editor, self.team_b), self.SENTINEL, timeout=60)
        res = self._get(self.b_editor, self.team_b)

        self.assertEqual(res.status_code, 404)
        self.assertNotEqual(res.data, self.SENTINEL)

    def test_a_member_of_a_deleted_team_gets_a_404_not_the_cached_list(self):
        cache.set(self._key(self.b_editor, self.team_b), self.SENTINEL, timeout=60)
        type(self.team_b).objects.filter(pk=self.team_b.pk).update(is_deleted=True)

        res = self._get(self.b_editor, self.team_b)

        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data, {"error": "Team not found."})

    def test_a_guest_is_checked_before_the_cache_too(self):
        add_external_participants(self.active_project_grant(), [self.b_viewer.id], self.b_owner)
        cache.set(self._key(self.b_viewer, self.team_a), self.SENTINEL, timeout=60)
        self.assertEqual(self._get(self.b_viewer, self.team_a).data, self.SENTINEL)

        ProjectMembers.objects.filter(attendee=self.b_viewer).delete()
        cache.set(self._key(self.b_viewer, self.team_a), self.SENTINEL, timeout=60)
        res = self._get(self.b_viewer, self.team_a)

        self.assertEqual(res.status_code, 404)

    def test_a_hit_does_not_look_up_shared_projects(self):
        """Only a miss needs the grants: the cached list already holds them."""
        self.active_project_grant()
        cache.set(self._key(self.b_editor, self.team_b), self.SENTINEL, timeout=60)

        with CaptureQueriesContext(connection) as ctx:
            res = self._get(self.b_editor, self.team_b)

        self.assertEqual(res.data, self.SENTINEL)
        grants = ExternalGrant._meta.db_table
        touched = [q["sql"][:120] for q in ctx.captured_queries if grants in q["sql"]]
        self.assertEqual(touched, [])
