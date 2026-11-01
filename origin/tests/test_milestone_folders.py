"""Milestone folders (MIP-847): a named group of milestones in a project.

A folder is independent of the sprint (a milestone has both). Any project
member manages folders. A folder from another project is refused, a project
move leaves the folder behind, and deleting a folder unfiles its milestones
without deleting anything.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import (
    MilestoneAssignees,
    MilestoneFolder,
    MilestoneMaster,
)
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.get_milestone_summary import GET_MILESTONE_SUMMARY
from origin.search_engine.agent.tools.list_milestones import LIST_MILESTONES
from origin.search_engine.agent.tools.list_my_milestones import LIST_MY_MILESTONES
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

FOLDER_URL = "/api/v2/milestone/folder/"


class MilestoneFolderTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Planned", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.other_project = ProjectMaster.objects.create(
            team=self.team, project_name="Elsewhere", owner=self.user
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.other_project, attendee=self.user
        )
        self.authenticate(self.user)

    # helpers --------------------------------------------------------

    def _folder(self, name="Launch", project=None, **kw):
        return MilestoneFolder.objects.create(
            team=self.team, project=project or self.project, name=name, **kw
        )

    def _milestone(self, title="M1", project=None, folder=None, sprint=None):
        project = project or self.project
        m = MilestoneMaster.objects.create(
            team=self.team,
            project=project,
            title=title,
            reporter=self.user,
            folder=folder,
            sprint=sprint,
        )
        backing = TaskMaster.objects.create(
            team=self.team,
            project=project,
            title=title,
            status="Open",
            is_milestone=True,
            milestone=m,
        )
        m.task = backing
        m.save(update_fields=["task"])
        return m

    def _post_folder(self, **body):
        body.setdefault("project_id", self.project.project_id)
        return self.client.post(FOLDER_URL, body, format="json")

    def _patch_ms(self, milestone, **body):
        return self.client.patch(
            f"/api/v2/milestone/{milestone.milestone_id}/", body, format="json"
        )

    # folder CRUD ----------------------------------------------------

    def test_a_member_creates_and_lists_folders_in_order(self):
        a = self._post_folder(name="Launch")
        b = self._post_folder(name="Growth")
        self.assertEqual(a.status_code, 201, a.data)
        self.assertEqual(a.data["folder"]["name"], "Launch")
        self.assertLess(a.data["folder"]["sortOrder"], b.data["folder"]["sortOrder"])
        res = self.client.get(f"{FOLDER_URL}list/", {"project_id": self.project.project_id})
        self.assertEqual([f["name"] for f in res.data["folders"]], ["Launch", "Growth"])

    def test_a_non_owner_member_can_manage_folders(self):
        member = User.objects.create_user(username="mem", email="mem@x.com", password="pw")
        TeamMembers.objects.create(team=self.team, attendee=member)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        folder = self._folder()
        self.authenticate(member)
        self.assertEqual(self._post_folder(name="Theirs").status_code, 201)
        res = self.client.patch(
            f"{FOLDER_URL}{folder.folder_id}/", {"name": "Renamed"}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.delete(f"{FOLDER_URL}{folder.folder_id}/").status_code, 204)

    def test_names_are_validated(self):
        self.assertEqual(self._post_folder(name="   ").status_code, 400)
        self.assertEqual(self._post_folder(name="x" * 101).status_code, 400)
        self.assertEqual(self._post_folder().status_code, 400)
        self._post_folder(name="Launch")
        self.assertEqual(self._post_folder(name="Launch").status_code, 400)
        self.assertEqual(MilestoneFolder.objects.filter(project=self.project).count(), 1)

    def test_the_same_name_may_exist_in_another_project(self):
        self._folder("Launch", project=self.other_project)
        self.assertEqual(self._post_folder(name="Launch").status_code, 201)

    def test_rename_onto_another_folder_is_a_400_not_a_500(self):
        self._folder("Growth")
        launch = self._folder("Launch")
        res = self.client.patch(
            f"{FOLDER_URL}{launch.folder_id}/", {"name": "Growth"}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        launch.refresh_from_db()
        self.assertEqual(launch.name, "Launch")

    def test_rename_and_reorder(self):
        f = self._folder()
        res = self.client.patch(
            f"{FOLDER_URL}{f.folder_id}/",
            {"name": "Q4", "sort_order": 7},
            format="json",
        )
        self.assertEqual((res.data["folder"]["name"], res.data["folder"]["sortOrder"]), ("Q4", 7))
        bad = self.client.patch(f"{FOLDER_URL}{f.folder_id}/", {"sort_order": "x"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_deleting_a_folder_unfiles_its_milestones_and_deletes_nothing(self):
        f = self._folder()
        m = self._milestone(folder=f)
        self.assertEqual(self.client.delete(f"{FOLDER_URL}{f.folder_id}/").status_code, 204)
        m.refresh_from_db()
        self.assertIsNone(m.folder_id)
        self.assertFalse(m.is_deleted)
        self.assertTrue(TaskMaster.objects.filter(task_id=m.task_id, is_deleted=False).exists())

    def test_an_outsider_sees_and_changes_nothing(self):
        f = self._folder()
        m = self._milestone(folder=f)
        outsider = User.objects.create_user(username="out", email="out@x.com", password="pw")
        other_team = TeamMaster.objects.create(team_name="O", team_email="o@x.com", owner=outsider)
        TeamMembers.objects.create(team=other_team, attendee=outsider)
        self.authenticate(outsider)
        pid = self.project.project_id
        self.assertEqual(
            self.client.get(f"{FOLDER_URL}list/", {"project_id": pid}).status_code, 404
        )
        self.assertEqual(self._post_folder(name="Planted").status_code, 404)
        self.assertEqual(
            self.client.patch(
                f"{FOLDER_URL}{f.folder_id}/", {"name": "x"}, format="json"
            ).status_code,
            404,
        )
        self.assertEqual(self.client.delete(f"{FOLDER_URL}{f.folder_id}/").status_code, 404)
        self.assertEqual(MilestoneFolder.objects.get(pk=f.pk).name, "Launch")
        m.refresh_from_db()
        self.assertEqual(m.folder_id, f.folder_id)

    # milestone folder field ----------------------------------------

    def test_create_a_milestone_into_a_folder(self):
        f = self._folder()
        res = self.client.post(
            "/api/v2/milestone/",
            {
                "project_id": self.project.project_id,
                "title": "Ship",
                "folder_id": f.folder_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["milestone"]["folderId"], f.folder_id)
        self.assertEqual(MilestoneMaster.objects.get(title="Ship").folder_id, f.folder_id)

    def test_create_unfiled_by_default(self):
        res = self.client.post(
            "/api/v2/milestone/",
            {"project_id": self.project.project_id, "title": "Ship"},
            format="json",
        )
        self.assertIsNone(res.data["milestone"]["folderId"])

    def test_create_with_another_projects_folder_is_refused_and_writes_nothing(self):
        foreign = self._folder("Foreign", project=self.other_project)
        res = self.client.post(
            "/api/v2/milestone/",
            {
                "project_id": self.project.project_id,
                "title": "Ship",
                "folder_id": foreign.folder_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(MilestoneMaster.objects.filter(title="Ship").exists())

    def test_move_between_folders_and_out(self):
        a, b = self._folder("A"), self._folder("B")
        m = self._milestone(folder=a)
        res = self._patch_ms(m, folder_id=b.folder_id)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["milestone"]["folderId"], b.folder_id)
        res = self._patch_ms(m, folder_id=None)
        self.assertIsNone(res.data["milestone"]["folderId"])
        m.refresh_from_db()
        self.assertIsNone(m.folder_id)

    def test_a_patch_without_folder_leaves_it(self):
        f = self._folder()
        m = self._milestone(folder=f)
        self._patch_ms(m, title="Renamed")
        m.refresh_from_db()
        self.assertEqual(m.folder_id, f.folder_id)

    def test_moving_into_another_projects_folder_is_refused_and_changes_nothing(self):
        mine = self._folder("Mine")
        foreign = self._folder("Foreign", project=self.other_project)
        m = self._milestone(folder=mine)
        res = self._patch_ms(m, title="Changed", folder_id=foreign.folder_id)
        self.assertEqual(res.status_code, 400)
        m.refresh_from_db()
        self.assertEqual((m.folder_id, m.title), (mine.folder_id, "M1"))

    def test_a_nonsense_folder_id_is_a_400(self):
        m = self._milestone()
        self.assertEqual(self._patch_ms(m, folder_id="abc").status_code, 400)
        self.assertEqual(self._patch_ms(m, folder_id=999999).status_code, 400)

    def test_the_folder_and_the_sprint_are_independent(self):
        sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S1",
            sequence_number=1,
            start_date="2026-08-01",
            end_date="2026-08-14",
        )
        f = self._folder()
        m = self._milestone(folder=f, sprint=sprint)
        res = self._patch_ms(m, sprint_id=None)
        self.assertIsNone(res.data["milestone"]["sprintId"])
        self.assertEqual(res.data["milestone"]["folderId"], f.folder_id)
        res = self._patch_ms(m, sprint_id=sprint.sprint_id)
        self.assertEqual(res.data["milestone"]["folderId"], f.folder_id)

    def test_a_project_move_leaves_the_folder_behind(self):
        f = self._folder()
        m = self._milestone(folder=f)
        res = self._patch_ms(m, project_id=self.other_project.project_id)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(res.data["milestone"]["folderId"])
        m.refresh_from_db()
        self.assertIsNone(m.folder_id)

    def test_a_folder_id_riding_a_project_move_is_ignored(self):
        mine = self._folder("Mine")
        foreign = self._folder("Foreign", project=self.other_project)
        m = self._milestone(folder=mine)
        res = self._patch_ms(
            m, project_id=self.other_project.project_id, folder_id=foreign.folder_id
        )
        self.assertEqual(res.status_code, 200, res.data)
        m.refresh_from_db()
        self.assertEqual(m.project_id, self.other_project.project_id)
        self.assertIsNone(m.folder_id)

    def test_the_milestone_list_carries_the_folder(self):
        f = self._folder()
        self._milestone("Filed", folder=f)
        self._milestone("Loose")
        res = self.client.get("/api/v2/milestone/list/", {"project_id": self.project.project_id})
        by_title = {m["title"]: m["folderId"] for m in res.data["milestones"]}
        self.assertEqual(by_title, {"Filed": f.folder_id, "Loose": None})

    # agent tools ----------------------------------------------------

    def _ctx(self):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def test_list_milestones_names_the_folder(self):
        f = self._folder("Launch")
        self._milestone("Filed", folder=f)
        self._milestone("Loose")
        out = LIST_MILESTONES.run({"project_id": self.project.project_id}, self._ctx())
        by_title = {m["title"]: (m["folder_id"], m["folder_name"]) for m in out["milestones"]}
        self.assertEqual(by_title, {"Filed": (f.folder_id, "Launch"), "Loose": (None, None)})

    def test_list_my_milestones_names_the_folder(self):
        f = self._folder("Launch")
        m = self._milestone("Filed", folder=f)
        MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user)
        out = LIST_MY_MILESTONES.run({}, self._ctx())
        self.assertEqual(out["milestones"][0]["folder_name"], "Launch")

    def test_get_milestone_summary_names_the_folder(self):
        f = self._folder("Launch")
        m = self._milestone("Filed", folder=f)
        out = GET_MILESTONE_SUMMARY.run({"milestone_id": m.milestone_id}, self._ctx())
        self.assertEqual((out["folder_id"], out["folder_name"]), (f.folder_id, "Launch"))
