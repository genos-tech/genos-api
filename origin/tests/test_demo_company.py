"""The company layer of the v2 demo workspace (MIP-1692, under MIP-1447).

A visitor's demo is one small team plus a company around it: four departments (a project tag, a
mention group, an open team folder and an open chat each), more projects than the visitor joins,
and a private folder, chat and project the visitor is not in. v1 (the eval corpus) must get none
of it, and the whole thing must be swept with the team.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectLabelAssignment,
    ProjectMaster,
    ProjectMembers,
)
from origin.models.task.task_models import TaskMaster
from origin.services import demo_company
from origin.services.demo_seeder import create_demo_environment, delete_demo_environment
from origin.views.utils.note_folder_role import get_folder_role

User = get_user_model()


def _user(name):
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", password="x", is_demo=True
    )


class DemoCompanyLayerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.en_user = _user("co_en")
        cls.en = create_demo_environment(cls.en_user, short="coen", lang="en", version="v2")
        cls.ja_user = _user("co_ja")
        cls.ja = create_demo_environment(cls.ja_user, short="coja", lang="ja", version="v2")
        cls.v1_user = _user("co_v1")
        cls.v1 = create_demo_environment(cls.v1_user, short="cov1", lang="en")

    def _projects(self, env):
        return ProjectMaster.objects.filter(team_id=env["team_id"])

    def _folders(self, env):
        return PersonalNoteFolder.objects.filter(team_id=env["team_id"], scope="team")

    # --- v1 is untouched ----------------------------------------------------------------

    def test_v1_gets_none_of_the_company_layer(self):
        team = self.v1["team_id"]

        self.assertEqual(self._projects(self.v1).count(), 2)
        self.assertFalse(ProjectLabel.objects.filter(team_id=team).exists())
        self.assertFalse(MentionGroupMaster.objects.filter(team_id=team).exists())
        self.assertFalse(self._folders(self.v1).exists())
        # The story's own chats: 3 DMs, a self chat, one group chat and 2 project chats.
        self.assertEqual(Channel.objects.filter(team_id=team).count(), 7)

    # --- the four departments -------------------------------------------------------------

    def test_each_department_is_a_tag_a_group_a_folder_and_an_open_chat(self):
        team = self.en["team_id"]
        copy = demo_company.COPY["en"]

        self.assertEqual(
            list(
                ProjectLabel.objects.filter(team_id=team)
                .order_by("label_id")
                .values_list("name", flat=True)
            ),
            copy["departments"],
        )
        self.assertEqual(
            list(
                MentionGroupMaster.objects.filter(team_id=team, is_deleted=False)
                .order_by("group_id")
                .values_list("group_name", flat=True)
            ),
            list(demo_company.GROUP_SLUGS),
        )
        for name in copy["departments"]:
            folder = self._folders(self.en).get(name=name)
            self.assertEqual(folder.visibility, "public")
            chat = Channel.objects.get(team_id=team, kind=ChannelKind.GM, title=f"{name} · general")
            self.assertFalse(chat.is_private)
            self.assertEqual(Message.objects.filter(channel=chat).count(), 2)

    def test_each_department_lead_is_the_person_whose_job_it_is(self):
        team = self.en["team_id"]
        names = ["Alice Chen", "Bob Martinez", "Carol Park", "Dan OConnor"]

        for index, name in enumerate(demo_company.COPY["en"]["departments"]):
            group = MentionGroupMaster.objects.get(
                team_id=team, group_name=demo_company.GROUP_SLUGS[index]
            )
            self.assertEqual(group.created_by.username, names[index])
            self.assertEqual(self._folders(self.en).get(name=name).owner.username, names[index])

    # --- join little --------------------------------------------------------------------------

    def test_the_visitor_is_in_four_of_seven_projects_and_two_of_four_chats(self):
        team = self.en["team_id"]
        # The story's two, the company layer's four, and the launch plan (MIP-1740).
        self.assertEqual(self._projects(self.en).count(), 7)

        joined = set(
            ProjectMembers.objects.filter(team_id=team, attendee=self.en_user).values_list(
                "project__project_name", flat=True
            )
        )
        self.assertEqual(len(joined), 4)
        self.assertIn("Launch plan", joined)
        self.assertIn("Design system", joined)

        open_chats = Channel.objects.filter(
            team_id=team, kind=ChannelKind.GM, title__endswith=" · general", is_private=False
        )
        self.assertEqual(open_chats.count(), 4)
        in_chats = ChannelMember.objects.filter(
            channel__in=open_chats, user=self.en_user, is_deleted=False
        ).count()
        self.assertEqual(in_chats, 2)

    def test_the_visitor_is_in_two_mention_groups(self):
        groups = set(
            MentionGroupMembers.objects.filter(
                team_id=self.en["team_id"], user=self.en_user
            ).values_list("group__group_name", flat=True)
        )

        self.assertEqual(groups, {"product", "design"})

    # --- what only the leads see --------------------------------------------------------------

    def test_the_private_project_has_no_visitor_in_it(self):
        private = self._projects(self.en).get(is_private=True)

        self.assertEqual(self._projects(self.en).filter(is_private=True).count(), 1)
        self.assertFalse(
            ProjectMembers.objects.filter(project=private, attendee=self.en_user).exists()
        )
        for task in TaskMaster.objects.filter(project=private):
            self.assertNotEqual(task.assignee_id, self.en_user.id)
            self.assertNotEqual(task.reporter_id, self.en_user.id)

    def test_the_private_chat_has_no_visitor_in_it(self):
        chat = Channel.objects.get(
            team_id=self.en["team_id"],
            title=demo_company.COPY["en"]["private_chat"],
            kind=ChannelKind.GM,
        )

        self.assertTrue(chat.is_private)
        self.assertFalse(ChannelMember.objects.filter(channel=chat, user=self.en_user).exists())
        self.assertEqual(Message.objects.filter(channel=chat).count(), 2)

    def test_the_visitor_can_open_the_department_folders_and_not_the_private_one(self):
        team_id = self.en["team_id"]
        for folder in self._folders(self.en).filter(visibility="public"):
            self.assertIsNotNone(
                get_folder_role(self.en_user.id, folder.folder_id, team_id), folder.name
            )
        private = self._folders(self.en).get(name=demo_company.COPY["en"]["private_folder"])
        self.assertEqual(private.visibility, "private")
        self.assertIsNone(get_folder_role(self.en_user.id, private.folder_id, team_id))

    def test_every_folder_has_a_note_its_lead_owns(self):
        notes = PersonalNoteMaster.objects.filter(
            team_id=self.en["team_id"], folder_id__in=self._folders(self.en).values("folder_id")
        )

        # Four departments, the overview in Product's folder, and the private note.
        self.assertEqual(notes.count(), 6)
        for folder in self._folders(self.en):
            owned = notes.filter(folder_id=folder.folder_id)
            self.assertTrue(owned.exists(), folder.name)
            for note in owned:
                self.assertEqual(note.owner_id, folder.owner_id)

    # --- how the projects are grouped -------------------------------------------------------------

    def test_the_two_story_projects_get_the_departments_that_own_them(self):
        team = self.en["team_id"]

        def tags_of(name_contains):
            project = ProjectMaster.objects.get(team_id=team, project_name__contains=name_contains)
            return set(
                ProjectLabelAssignment.objects.filter(project=project).values_list(
                    "label__name", flat=True
                )
            )

        self.assertEqual(tags_of("Website"), {"Design", "Engineering"})
        self.assertEqual(tags_of("Roadmap"), {"Product"})

    def test_every_extra_project_carries_its_departments_tag(self):
        team = self.en["team_id"]
        copy = demo_company.COPY["en"]
        for spec in copy["projects"]:
            project = ProjectMaster.objects.get(team_id=team, project_name=spec["name"])
            self.assertEqual(
                set(
                    ProjectLabelAssignment.objects.filter(project=project).values_list(
                        "label__name", flat=True
                    )
                ),
                {copy["departments"][spec["department"]]},
            )
            self.assertEqual(project.is_private, spec["private"])

    def test_every_extra_task_is_filed_like_the_rest(self):
        team = self.en["team_id"]
        for spec in demo_company.COPY["en"]["projects"]:
            project = ProjectMaster.objects.get(team_id=team, project_name=spec["name"])
            tasks = TaskMaster.objects.filter(project=project, is_milestone=False)
            self.assertEqual(tasks.count(), len(spec["tasks"]))
            for task in tasks:
                self.assertTrue(task.tags, task.title)
                self.assertTrue(task.effort_level and task.effort_level_code, task.title)
                self.assertNotEqual(task.assignee_id, self.en_user.id, task.title)

    # --- Japanese and parity ----------------------------------------------------------------------

    def test_the_japanese_workspace_has_the_same_shape_in_japanese(self):
        team = self.ja["team_id"]
        copy = demo_company.COPY["ja"]

        self.assertEqual(self._projects(self.ja).count(), 7)
        self.assertEqual(
            list(
                ProjectLabel.objects.filter(team_id=team)
                .order_by("label_id")
                .values_list("name", flat=True)
            ),
            copy["departments"],
        )
        self.assertTrue(
            Channel.objects.filter(
                team_id=team, title=f"{copy['departments'][0]} · general"
            ).exists()
        )
        self.assertTrue(
            self._projects(self.ja).filter(project_name=copy["projects"][0]["name"]).exists()
        )
        self.assertEqual(
            Channel.objects.filter(team_id=team).count(),
            Channel.objects.filter(team_id=self.en["team_id"]).count(),
        )

    def test_both_languages_have_the_same_structure(self):
        en, ja = demo_company.COPY["en"], demo_company.COPY["ja"]

        self.assertEqual(set(en), set(ja))
        for key in ("departments", "group_descriptions", "folder_notes", "chat_messages"):
            self.assertEqual(len(en[key]), len(ja[key]), key)
        self.assertEqual(len(en["private_chat_messages"]), len(ja["private_chat_messages"]))
        self.assertEqual(len(en["projects"]), len(ja["projects"]))
        for a, b in zip(en["projects"], ja["projects"]):
            for key in ("department", "owner", "members", "visitor", "private"):
                self.assertEqual(a[key], b[key], key)
            self.assertEqual(len(a["tasks"]), len(b["tasks"]))
            self.assertEqual(len(a["tags"]), len(b["tags"]))

    def test_the_overview_note_says_what_is_true(self):
        text = str(
            PersonalNoteMaster.objects.get(
                team_id=self.en["team_id"], title=demo_company.COPY["en"]["overview_title"]
            ).body
        )
        for slug in demo_company.GROUP_SLUGS:
            self.assertIn(f"@{slug}", text)
        self.assertIn("four of the seven projects", text)
        self.assertIn("two of the four department chats", text)

    # --- cleanup -------------------------------------------------------------------------------------

    def test_the_whole_layer_is_swept_with_the_team(self):
        user = _user("co_sweep")
        env = create_demo_environment(user, short="cosweep", lang="en", version="v2")
        team = env["team_id"]
        orphans_before = (
            MentionGroupMaster.objects.filter(team__isnull=True).count(),
            ProjectLabel.objects.filter(team__isnull=True).count(),
            PersonalNoteFolder.objects.filter(team__isnull=True, scope="team").count(),
        )

        delete_demo_environment(user)

        self.assertFalse(TeamMaster.objects.filter(team_id=team).exists())
        self.assertFalse(ProjectLabel.objects.filter(team_id=team).exists())
        self.assertFalse(MentionGroupMaster.objects.filter(team_id=team).exists())
        self.assertFalse(PersonalNoteFolder.objects.filter(team_id=team).exists())
        self.assertFalse(Channel.objects.filter(team_id=team).exists())
        self.assertFalse(ProjectMaster.objects.filter(team_id=team).exists())
        # Nothing left behind with its team link cleared (MIP-1702).
        self.assertEqual(
            (
                MentionGroupMaster.objects.filter(team__isnull=True).count(),
                ProjectLabel.objects.filter(team__isnull=True).count(),
                PersonalNoteFolder.objects.filter(team__isnull=True, scope="team").count(),
            ),
            orphans_before,
        )
