"""Working inside a share, from the guest team's own workspace.

Discovery shipped first: a shared project, chat and note folder now appear
in the guest team's own lists. Opening one was still empty. Every handler
underneath filters by the `team_id` in the request — the team the caller
is VIEWING — while the project, its tasks and its notes belong to the
host, so the queries matched nothing. No error said so: the task list came
back `[]`, the note came back 404, and the client's next call with the
half-loaded state came back 400.

`views/utils/foreign_team_scope` fixes that in one place by pointing
`team_id` at the team that owns the object named in the request. These
tests are the contract for it, and they are as much about what it must NOT
do: a stranger naming the same ids, and a colleague on the guest team who
was never admitted, have to see exactly what they saw before — nothing.
"""

from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.tests.cross_team_fixtures import CrossTeamTestCase

CHANNELS = "/api/v3/channels/"
PROJECT_TASKS = "/api/v2/task/getProjectTasks/"
SINGLE_TASK = "/api/v2/task/getTask/"
MILESTONES = "/api/v2/milestone/list/"
SINGLE_NOTE = "/api/v2/note/personal/single/"
CREATE_TASK = "/api/v2/task/"


class GuestReadsTheSharedProjectTests(CrossTeamTestCase):
    """The tasks in a shared project, asked for from the guest's own team."""

    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()
        self.task = TaskMaster.objects.create(
            team=self.team_a,
            project=self.project,
            title="Host task",
            assignee=self.a_owner,
            reporter=self.a_owner,
        )

    def _as_b(self, user, **extra):
        params = {"team_id": str(self.team_b.team_id), "project_id": str(self.project.project_id)}
        params.update(extra)
        self.authenticate(user)
        return params

    def test_the_project_task_list_is_not_empty_for_the_guest(self):
        params = self._as_b(self.b_owner)
        res = self.client.get(PROJECT_TASKS, params)
        self.assertEqual(res.status_code, 200, res.data)
        ids = [str(t["id"]) for t in res.data["data"]["tasks"]]
        self.assertIn(str(self.task.task_id), ids)

    def test_the_guest_can_open_one_task(self):
        params = self._as_b(self.b_owner, task_id=str(self.task.task_id))
        res = self.client.get(SINGLE_TASK, params)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data[0]["id"], self.task.task_id)

    def test_the_guest_can_list_milestones(self):
        params = self._as_b(self.b_owner)
        res = self.client.get(MILESTONES, params)
        self.assertEqual(res.status_code, 200, res.data)

    def test_a_task_the_guest_creates_is_filed_under_the_host_team(self):
        """Otherwise it lands in team B, in team A's project — invisible to both."""
        self._as_b(self.b_owner)
        res = self.client.post(
            CREATE_TASK,
            {
                "team": str(self.team_b.team_id),
                "project": str(self.project.project_id),
                "assignee": str(self.b_owner.id),
                "reporter": str(self.b_owner.id),
                "title": "Guest task",
                "priority": "medium",
                "effort_level": "medium",
                "status": "open",
                "content": None,
                "due_date": None,
                "links": [],
                "tags": [],
                "is_init_task": False,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        created = TaskMaster.objects.get(title="Guest task")
        self.assertEqual(str(created.team_id), str(self.team_a.team_id))
        self.assertEqual(created.project_id, self.project.project_id)

    def test_a_colleague_who_was_never_admitted_still_sees_nothing(self):
        """The grant names team B; access is per person inside it."""
        params = self._as_b(self.b_viewer)
        res = self.client.get(PROJECT_TASKS, params)
        self.assertEqual(res.status_code, 404, res.data)

    def test_a_stranger_naming_the_same_ids_is_refused(self):
        self.authenticate(self.c_owner)
        res = self.client.get(
            PROJECT_TASKS,
            {"team_id": str(self.team_c.team_id), "project_id": str(self.project.project_id)},
        )
        self.assertEqual(res.status_code, 404, res.data)

    def test_a_stranger_cannot_open_the_task_either(self):
        self.authenticate(self.c_owner)
        res = self.client.get(
            SINGLE_TASK,
            {
                "team_id": str(self.team_c.team_id),
                "project_id": str(self.project.project_id),
                "task_id": str(self.task.task_id),
            },
        )
        self.assertEqual(res.status_code, 404, res.data)

    def test_the_guests_own_project_is_untouched_by_any_of_this(self):
        """The rewrite must be a no-op whenever the two already agree."""
        own = ProjectMaster.objects.create(
            team=self.team_b,
            project_name="Our Own Project",
            owner=self.b_owner,
            project_system_user=self.b_owner,
        )
        from origin.models.project.prj_models import ProjectMembers

        ProjectMembers.objects.create(project=own, attendee=self.b_owner, team=self.team_b)
        mine = TaskMaster.objects.create(
            team=self.team_b,
            project=own,
            title="Our task",
            assignee=self.b_owner,
            reporter=self.b_owner,
        )
        self.authenticate(self.b_owner)
        res = self.client.get(
            PROJECT_TASKS,
            {"team_id": str(self.team_b.team_id), "project_id": str(own.project_id)},
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn(str(mine.task_id), [str(t["id"]) for t in res.data["data"]["tasks"]])


class SharedProjectChatReachesItsGuestsTests(CrossTeamTestCase):
    """A project's PM chat is where its icon and its profile live.

    Sharing a project admits its guests to that channel (the
    `ProjectMembers` -> `ChannelMember` mirror does it), but the chat list
    narrowed by owning team, so the row never arrived. The visible cost
    was not a missing conversation: with no PM chat in hand the client
    renders the shared project with no icon and has no route into the
    project profile at all.
    """

    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()
        # The host's own roster row, which the create endpoint writes and
        # the fixture's ORM insert does not. Without it the host is not in
        # their own project's chat, and the last assertion below has
        # nothing to read.
        from origin.models.project.prj_models import ProjectMembers

        ProjectMembers.objects.create(project=self.project, attendee=self.a_owner, team=self.team_a)

    def _pm_channel(self):
        from origin.models.chat.unified_models import Channel, ChannelKind

        return Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)

    def _list_as(self, user):
        self.authenticate(user)
        return self.client.get(CHANNELS, {"team_id": str(self.team_b.team_id)})

    def test_the_guest_sees_the_shared_projects_chat_in_their_own_sidebar(self):
        res = self._list_as(self.b_owner)
        self.assertEqual(res.status_code, 200, res.data)
        ids = [c["id"] for c in res.data["channels"]]
        self.assertIn(str(self._pm_channel().id), ids)

    def test_the_row_names_the_team_that_owns_it(self):
        res = self._list_as(self.b_owner)
        row = next(c for c in res.data["channels"] if c["id"] == str(self._pm_channel().id))
        self.assertEqual(row["hostTeamName"], self.team_a.team_name)
        self.assertEqual(str(row["projectId"]), str(self.project.project_id))

    def test_a_colleague_who_was_never_admitted_does_not_see_it(self):
        res = self._list_as(self.b_viewer)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn(str(self._pm_channel().id), [c["id"] for c in res.data["channels"]])

    def test_the_host_row_is_not_labelled_as_somebody_elses(self):
        self.authenticate(self.a_owner)
        res = self.client.get(CHANNELS, {"team_id": str(self.team_a.team_id)})
        row = next(c for c in res.data["channels"] if c["id"] == str(self._pm_channel().id))
        self.assertIsNone(row["hostTeamName"])


class RaisingTheCeilingTests(CrossTeamTestCase):
    """Every share was read-only, and raising the ceiling did nothing.

    The offer endpoint defaulted to `viewer` and no UI could send anything
    else, so a guest team accepted folders they could not write in. Fixing
    the default is half of it; the other half is that a host who raises
    the ceiling on an existing share expects the people already in it to
    be able to edit.
    """

    def test_offering_defaults_to_editor(self):
        self.connect_a_and_b()
        self.authenticate(self.a_owner)
        res = self.client.post(
            "/api/v2/team/share/",
            {
                "team_id": str(self.team_a.team_id),
                "guest_team_id": str(self.team_b.team_id),
                "object_type": "project",
                "object_id": str(self.project.project_id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["roleCeiling"], "editor")

    def test_raising_the_ceiling_promotes_the_people_already_in(self):
        from origin.models.note.common_note_models import NoteFolderPermission
        from origin.services.external_grants import add_external_participants, set_role_ceiling
        from origin.views.utils.note_role import ROLE_EDITOR, ROLE_VIEWER

        grant = self.active_folder_grant(role_ceiling="viewer")
        add_external_participants(grant, [self.b_editor.id], self.b_owner)
        self.assertEqual(self._folder_role(self.b_editor), ROLE_VIEWER)

        set_role_ceiling(grant, "editor", self.a_owner)
        self.assertEqual(self._folder_role(self.b_owner), ROLE_EDITOR)
        self.assertEqual(self._folder_role(self.b_editor), ROLE_EDITOR)
        self.assertFalse(
            NoteFolderPermission.objects.filter(
                folder_id=self.folder.folder_id, user=self.a_owner
            ).exists(),
            "the host's own people are not participants and must not be rewritten",
        )

    def test_lowering_the_ceiling_leaves_them_alone(self):
        """Silently taking write access away mid-edit is the worse surprise."""
        from origin.services.external_grants import set_role_ceiling
        from origin.views.utils.note_role import ROLE_EDITOR

        grant = self.active_folder_grant(role_ceiling="editor")
        set_role_ceiling(grant, "viewer", self.a_owner)
        self.assertEqual(self._folder_role(self.b_owner), ROLE_EDITOR)

    def _folder_role(self, user):
        from origin.models.note.common_note_models import NoteFolderPermission

        return (
            NoteFolderPermission.objects.filter(folder_id=self.folder.folder_id, user=user)
            .values_list("role_id", flat=True)
            .first()
        )


class GuestReadsTheSharedNoteTests(CrossTeamTestCase):
    """A note in a shared folder — the 404 that made the folder pointless."""

    def setUp(self):
        super().setUp()
        self.grant = self.active_folder_grant()
        self.note = PersonalNoteMaster.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            title="Host note",
            folder_id=self.folder.folder_id,
        )

    def _params(self, user, **extra):
        params = {
            "team_id": str(self.team_b.team_id),
            "user_id": str(user.id),
            "note_id": str(self.note.note_id),
        }
        params.update(extra)
        self.authenticate(user)
        return params

    def test_the_guest_can_open_a_note_in_the_shared_folder(self):
        res = self.client.get(SINGLE_NOTE, self._params(self.b_owner))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(str(res.data["noteId"]), str(self.note.note_id))

    def test_the_note_reports_the_team_that_owns_it(self):
        res = self.client.get(SINGLE_NOTE, self._params(self.b_owner))
        self.assertEqual(str(res.data["teamId"]), str(self.team_a.team_id))

    def test_a_colleague_who_was_never_admitted_cannot_open_it(self):
        self.authenticate(self.b_viewer)
        res = self.client.get(
            SINGLE_NOTE,
            {
                "team_id": str(self.team_b.team_id),
                "user_id": str(self.b_viewer.id),
                "note_id": str(self.note.note_id),
            },
        )
        self.assertEqual(res.status_code, 403, res.data)

    def test_a_stranger_cannot_open_it(self):
        self.authenticate(self.c_owner)
        res = self.client.get(
            SINGLE_NOTE,
            {
                "team_id": str(self.team_c.team_id),
                "user_id": str(self.c_owner.id),
                "note_id": str(self.note.note_id),
            },
        )
        self.assertEqual(res.status_code, 403, res.data)
