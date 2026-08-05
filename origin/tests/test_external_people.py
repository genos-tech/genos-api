"""The other team's people: who exists, and what they may see.

Every surface that renders a person — an avatar, an assignee cell, a
member row, the profile modal — resolves through the roster the client
caches from `getTeamMembers`. Cross-team collaborators were in no team's
roster, so both sides rendered each other as blank circles with no name
and no status, in chats and projects they share.

The fix is to include them, marked, and the interesting assertions are
about the marking and the narrowing: they carry THEIR team's identity so
nothing mistakes them for staff, and a colleague who was never admitted
to the shared object is nobody here — the same rule that decides access.

The second half covers the team-wide task lists, which are what the
dashboard, the table and the board read. They filtered by the viewing
team while a shared project's tasks are filed under the host, so a guest
could open one task and see an empty board around it.
"""

from origin.models.task.task_models import TaskMaster
from origin.tests.cross_team_fixtures import CrossTeamTestCase

TEAM_MEMBERS = "/api/v2/team/getTeamMembers/"
TEAM_TASKS = "/api/v2/task/getTeamTasks/"
TASK_META = "/api/v2/task/meta/"
ASSIGNED_TASKS = "/api/v2/task/getMyAssignedTasks/"
SEARCH_TASKS = "/api/v2/search/teamTasks/"
PRESENCE_TEAMS = "/api/v2/user/presence/teams/"


class TheHostSeesTheGuestTeamsPeopleTests(CrossTeamTestCase):
    """Team A's roster, asked for by team A, with a project shared to B."""

    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()

    def _roster(self, user, team=None):
        self.authenticate(user)
        team = team or self.team_a
        res = self.client.get(
            TEAM_MEMBERS,
            {
                "team_id": str(team.team_id),
                "team_name": team.team_name,
                "user_id": str(user.id),
            },
        )
        self.assertEqual(res.status_code, 200, res.data)
        return {str(m["userId"]): m for m in res.data["data"]["members"]}

    def test_the_admitted_guest_is_in_the_hosts_roster(self):
        roster = self._roster(self.a_owner)
        self.assertIn(str(self.b_owner.id), roster)

    def test_they_are_marked_external_and_carry_their_own_team(self):
        row = self._roster(self.a_owner)[str(self.b_owner.id)]
        self.assertTrue(row["isExternal"])
        self.assertEqual(str(row["homeTeamId"]), str(self.team_b.team_id))
        self.assertEqual(row["homeTeamName"], self.team_b.team_name)
        # `teamId` is the roster this row belongs to, not where they work.
        self.assertEqual(str(row["teamId"]), str(self.team_a.team_id))

    def test_they_have_a_name_and_an_email_to_render(self):
        row = self._roster(self.a_owner)[str(self.b_owner.id)]
        self.assertEqual(row["userName"], self.b_owner.username)
        self.assertEqual(row["userEmail"], self.b_owner.email)

    def test_their_teams_picture_travels_with_them(self):
        """The badge on their avatar is their team's own picture, and a
        letter only when that team has none. A UUID column keyed a dict of
        icons that was looked up by the string form of the same id, so it
        matched nothing and every external avatar wore a letter while the
        team's NAME beside it was right — the mismatch that made it look
        like the picture simply wasn't being sent."""
        self.team_b.profile_image_file_name = "teams/team-b.png"
        self.team_b.save(update_fields=["profile_image_file_name"])
        row = self._roster(self.a_owner)[str(self.b_owner.id)]
        self.assertEqual(row["homeTeamImgPath"], "teams/team-b.png")

    def test_they_read_as_a_guest_rather_than_a_teammate(self):
        self.assertEqual(self._roster(self.a_owner)[str(self.b_owner.id)]["memberRole"], "guest")

    def test_a_colleague_of_theirs_who_was_never_admitted_is_not_there(self):
        self.assertNotIn(str(self.b_viewer.id), self._roster(self.a_owner))

    def test_the_hosts_own_people_are_not_marked_external(self):
        row = self._roster(self.a_owner)[str(self.a_editor.id)]
        self.assertFalse(row.get("isExternal", False))

    def test_a_host_member_who_is_not_in_the_shared_project_sees_nobody(self):
        """One project shared does not put the guest team in every colleague's
        directory — the roster follows the objects you are actually in."""
        self.assertNotIn(str(self.b_owner.id), self._roster(self.a_viewer))

    def test_a_stranger_team_is_unaffected(self):
        self.assertNotIn(str(self.b_owner.id), self._roster(self.c_owner, team=self.team_c))


class TheGuestSeesTheHostsPeopleTests(CrossTeamTestCase):
    """The same question from inside the guest team's own workspace.

    A guest asking for THEIR team's roster gets their colleagues plus the
    host-team people they share the object with — otherwise the host's
    names render blank in the guest's copy of the same chat.
    """

    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()
        from origin.models.project.prj_models import ProjectMembers

        ProjectMembers.objects.create(
            project=self.project, attendee=self.a_owner, team=self.team_a
        )

    def _roster(self, user):
        self.authenticate(user)
        res = self.client.get(
            TEAM_MEMBERS,
            {
                "team_id": str(self.team_b.team_id),
                "team_name": self.team_b.team_name,
                "user_id": str(user.id),
            },
        )
        self.assertEqual(res.status_code, 200, res.data)
        return {str(m["userId"]): m for m in res.data["data"]["members"]}

    def test_the_hosts_project_people_are_in_the_guest_teams_roster(self):
        row = self._roster(self.b_owner).get(str(self.a_owner.id))
        self.assertIsNotNone(row, "the host's project owner has to be nameable")
        self.assertTrue(row["isExternal"])
        self.assertEqual(str(row["homeTeamId"]), str(self.team_a.team_id))

    def test_the_hosts_other_staff_are_still_withheld(self):
        """Sharing a project discloses the people on it, never the company."""
        self.assertNotIn(str(self.a_editor.id), self._roster(self.b_owner))


class SharedWorkFillsTheTeamWideListsTests(CrossTeamTestCase):
    """The dashboard, the table and the board, from the guest's own team."""

    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()
        self.task = TaskMaster.objects.create(
            team=self.team_a,
            project=self.project,
            title="Host task",
            assignee=self.b_owner,
            reporter=self.a_owner,
            # `status` has no model default, and the search endpoint
            # filters on it — an empty one is matched by no query.
            status="Open",
        )

    def _as_b(self, user, url, **extra):
        self.authenticate(user)
        params = {"team_id": str(self.team_b.team_id)}
        params.update(extra)
        return self.client.get(url, params)

    def test_the_team_wide_task_list_includes_the_shared_project(self):
        res = self._as_b(self.b_owner, TEAM_TASKS)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn(str(self.task.task_id), [str(t["id"]) for t in res.data])

    def test_the_task_meta_tree_includes_it(self):
        res = self._as_b(self.b_owner, TASK_META, user_id=str(self.b_owner.id))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn(str(self.task.task_id), [str(t["taskId"]) for t in res.data])

    def test_a_guest_sees_a_task_assigned_to_them_in_the_shared_project(self):
        res = self._as_b(self.b_owner, ASSIGNED_TASKS)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn(str(self.task.task_id), [str(t["id"]) for t in res.data])

    def test_the_team_wide_task_search_finds_it(self):
        res = self._as_b(
            self.b_owner,
            SEARCH_TASKS,
            project_id="-1",
            statuses="open",
            top_n="20",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn(self.task.task_id, [t["taskId"] for t in res.data])

    def test_a_colleague_who_was_never_admitted_sees_none_of_it(self):
        res = self._as_b(self.b_viewer, TEAM_TASKS)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn(str(self.task.task_id), [str(t["id"]) for t in res.data])

    def test_the_hosts_own_team_wide_list_is_unchanged(self):
        self.authenticate(self.a_owner)
        from origin.models.project.prj_models import ProjectMembers

        ProjectMembers.objects.create(
            project=self.project, attendee=self.a_owner, team=self.team_a
        )
        res = self.client.get(TEAM_TASKS, {"team_id": str(self.team_a.team_id)})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn(str(self.task.task_id), [str(t["id"]) for t in res.data])

    def test_a_stranger_still_gets_nothing(self):
        self.authenticate(self.c_owner)
        res = self.client.get(TEAM_TASKS, {"team_id": str(self.team_c.team_id)})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data, [])


class TheProjectListSaysHowManyRowsToHoldTests(CrossTeamTestCase):
    """A delta answer has to be checkable, or a wrong cache is permanent.

    The client stores a watermark meaning "everything older is already
    here" and afterwards only ever asks what changed. Nothing in a later
    answer can contradict that — an unchanged row is never mentioned — so
    a client that took a watermark without the rows shows an empty table
    forever. It happened to every guest whose share predated the fix, and
    survived it. `totalCount` is the one number that lets them notice.
    """

    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()
        for title in ("Old milestone", "Old child", "Old subtask"):
            TaskMaster.objects.create(
                team=self.team_a, project=self.project, title=title, status="Open"
            )

    def _tasks(self, user, **extra):
        self.authenticate(user)
        params = {"team_id": str(self.team_b.team_id), "project_id": str(self.project.project_id)}
        params.update(extra)
        res = self.client.get("/api/v2/task/getProjectTasks/", params)
        self.assertEqual(res.status_code, 200, res.data)
        return res.data["data"]

    def test_an_answer_about_changes_says_how_many_rows_there_are(self):
        """Asked what changed since a moment when nothing has, the guest is
        told nothing changed AND that they should be holding three rows."""
        data = self._tasks(self.b_owner, since="2999-01-01T00:00:00Z")
        self.assertEqual(data["tasks"], [])
        self.assertEqual(data["totalCount"], 3)

    def test_a_full_answer_is_its_own_count(self):
        data = self._tasks(self.b_owner)
        self.assertEqual(len(data["tasks"]), 3)
        self.assertNotIn("totalCount", data)

    def test_the_count_is_the_hosts_whole_project(self):
        """Not "the rows filed under the team asking" — that is zero for a
        guest, and a zero target can never prompt anyone to ask again."""
        self.assertEqual(self._tasks(self.b_owner, since="2999-01-01T00:00:00Z")["totalCount"], 3)


class PresenceReachesTheOtherTeamTests(CrossTeamTestCase):
    """Whose rooms a heartbeat has to arrive in for a dot to be green."""

    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()
        from origin.models.project.prj_models import ProjectMembers

        ProjectMembers.objects.create(
            project=self.project, attendee=self.a_owner, team=self.team_a
        )

    def _audience(self, user):
        self.authenticate(user)
        res = self.client.get(PRESENCE_TEAMS)
        self.assertEqual(res.status_code, 200, res.data)
        return set(res.data["teamIds"])

    def test_a_host_member_publishes_into_the_guest_teams_room(self):
        audience = self._audience(self.a_owner)
        self.assertIn(str(self.team_a.team_id), audience)
        self.assertIn(str(self.team_b.team_id), audience)

    def test_a_guest_publishes_into_the_host_teams_room(self):
        audience = self._audience(self.b_owner)
        self.assertIn(str(self.team_b.team_id), audience)
        self.assertIn(str(self.team_a.team_id), audience)

    def test_someone_not_in_the_shared_object_publishes_only_to_their_own(self):
        self.assertEqual(self._audience(self.b_viewer), {str(self.team_b.team_id)})

    def test_a_stranger_publishes_only_to_their_own(self):
        self.assertEqual(self._audience(self.c_owner), {str(self.team_c.team_id)})
