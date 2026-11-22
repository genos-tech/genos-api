"""The example department a new team gets with the sample data (MIP-1690, under MIP-1447).

What matters: every piece of the decided model is there and is what it claims to be (a public
group chat, a private one, a private project, a private team folder, a project tag on the
projects, a mention group the owner is in, team notes the owner can open); it is the owner's
alone; it is in the account's language; and a failure in it never costs the team or the rest of
the starter.
"""

from unittest import mock

from rest_framework import status

from origin.models.chat.todo_models import ToDoItem
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectLabelAssignment,
    ProjectMaster,
)
from origin.models.task.task_models import TaskMaster
from origin.services.organisation_examples import COPY, GROUP_NAME, example_locale
from origin.services.starter_seeder import JAPANESE_STARTER_BLUEPRINT, STARTER_BLUEPRINT
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_folder_role import get_folder_role


class OrganisationExamplesTests(BaseAPITestCase):
    URL = "/api/v2/team/create/"

    def _create_team(self, with_starter=True, name="Acme"):
        self.authenticate()
        payload = {
            "team_name": name,
            "team_email": f"{name.lower()}@example.com",
            "owner_id": str(self.user.id),
        }
        if with_starter is not None:
            payload["with_starter"] = with_starter
        with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
            resp = self.client.post(self.URL, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return TeamMaster.objects.get(team_id=resp.data["teamDetails"]["teamId"])

    def _teammate(self, team):
        other = CustomUser.objects.create_user(
            username="teammate", email="teammate@example.com", password="x"
        )
        TeamMembers.objects.create(team=team, attendee=other)
        return other

    # --- the English example department ---------------------------------------------------

    def test_a_project_tag_groups_both_example_projects(self):
        team = self._create_team()
        copy = COPY["en"]

        label = ProjectLabel.objects.get(team=team, name=copy["label"])
        assigned = set(
            ProjectLabelAssignment.objects.filter(label=label).values_list(
                "project__project_name", flat=True
            )
        )
        self.assertEqual(assigned, {copy["project"], copy["private_project"]})

    def test_one_project_is_open_and_one_is_private(self):
        team = self._create_team()
        copy = COPY["en"]

        self.assertFalse(
            ProjectMaster.objects.get(team=team, project_name=copy["project"]).is_private
        )
        private = ProjectMaster.objects.get(team=team, project_name=copy["private_project"])
        self.assertTrue(private.is_private)
        self.assertEqual(str(private.owner_id), str(self.user.id))

    def test_the_example_projects_have_a_milestone_and_tasks_for_the_owner(self):
        team = self._create_team()
        copy = COPY["en"]

        for name, task_specs in (
            (copy["project"], copy["project_tasks"]),
            (copy["private_project"], copy["private_project_tasks"]),
        ):
            project = ProjectMaster.objects.get(team=team, project_name=name)
            tasks = TaskMaster.objects.filter(team=team, project=project)
            # The specs, plus the milestone's own backing task.
            self.assertEqual(tasks.count(), len(task_specs) + 1)
            for task in tasks:
                self.assertEqual(str(task.assignee_id), str(self.user.id))

    def test_the_mention_group_has_the_owner_in_it(self):
        team = self._create_team()

        group = MentionGroupMaster.objects.get(team=team, group_name=GROUP_NAME, is_deleted=False)
        self.assertEqual(
            list(MentionGroupMembers.objects.filter(group=group).values_list("user_id", flat=True)),
            [self.user.id],
        )

    def test_the_open_group_chat_is_public_and_the_other_is_private(self):
        team = self._create_team()
        copy = COPY["en"]

        public = Channel.objects.get(team=team, kind=ChannelKind.GM, title=copy["chat_general"])
        private = Channel.objects.get(team=team, kind=ChannelKind.GM, title=copy["chat_private"])
        self.assertFalse(public.is_private)
        self.assertTrue(private.is_private)
        for channel in (public, private):
            self.assertEqual(str(channel.owner_id), str(self.user.id))
            members = ChannelMember.objects.filter(channel=channel)
            self.assertEqual([(m.user_id, m.role) for m in members], [(self.user.id, "owner")])
            # A chat that is not empty, so it shows in the list and says what it is for.
            self.assertEqual(Message.objects.filter(channel=channel).count(), 1)

    def test_the_team_folders_are_one_open_with_a_subfolder_and_one_private(self):
        team = self._create_team()
        copy = COPY["en"]

        open_folder = PersonalNoteFolder.objects.get(team=team, scope="team", name=copy["folder"])
        private_folder = PersonalNoteFolder.objects.get(
            team=team, scope="team", name=copy["private_folder"]
        )
        subfolder = PersonalNoteFolder.objects.get(team=team, scope="team", name=copy["subfolder"])
        self.assertEqual(open_folder.visibility, "public")
        self.assertEqual(private_folder.visibility, "private")
        self.assertEqual(subfolder.parent_folder_id, open_folder.folder_id)

    def test_a_teammate_can_open_the_open_folder_and_not_the_private_one(self):
        team = self._create_team()
        other = self._teammate(team)
        copy = COPY["en"]

        open_folder = PersonalNoteFolder.objects.get(team=team, name=copy["folder"], scope="team")
        private_folder = PersonalNoteFolder.objects.get(
            team=team, name=copy["private_folder"], scope="team"
        )
        self.assertIsNotNone(get_folder_role(other.id, open_folder.folder_id, team.team_id))
        self.assertIsNone(get_folder_role(other.id, private_folder.folder_id, team.team_id))
        self.assertIsNotNone(get_folder_role(self.user.id, private_folder.folder_id, team.team_id))

    def test_each_team_note_is_the_owners_with_a_role_row(self):
        team = self._create_team()
        copy = COPY["en"]

        titles = {copy["overview_title"], copy["meeting_title"], copy["private_title"]}
        notes = PersonalNoteMaster.objects.filter(team=team, title__in=titles)
        self.assertEqual({n.title for n in notes}, titles)
        for note in notes:
            self.assertEqual(str(note.owner_id), str(self.user.id))
            self.assertTrue(
                NotePermissionMaster.objects.filter(
                    team=team, user=self.user, note_id=note.note_id, note_type=1, role_id=1
                ).exists()
            )
            self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=note.folder_id).exists())

    def test_the_overview_note_names_what_the_example_made(self):
        """The how-it-works note must quote the real names, or it misleads."""
        team = self._create_team()
        copy = COPY["en"]

        text = str(PersonalNoteMaster.objects.get(team=team, title=copy["overview_title"]).body)
        for name in (copy["label"], f"@{GROUP_NAME}", copy["chat_general"]):
            self.assertIn(name, text)

    def test_one_to_do_points_at_the_samples(self):
        team = self._create_team()

        self.assertTrue(
            ToDoItem.objects.filter(
                group__team=team, group__user=self.user, title=COPY["en"]["todo_title"]
            ).exists()
        )

    def test_nothing_is_made_for_anyone_else_and_nothing_is_a_demo(self):
        users_before = CustomUser.objects.count()
        team = self._create_team()

        self.assertEqual(CustomUser.objects.count(), users_before)
        self.assertFalse(team.is_demo)
        self.assertEqual(
            set(TeamMembers.objects.filter(team=team).values_list("attendee_id", flat=True)),
            {self.user.id},
        )

    # --- language ---------------------------------------------------------------------------

    def test_a_japanese_account_gets_the_japanese_example_only(self):
        self.user.language = "ja"
        self.user.save(update_fields=["language"])
        team = self._create_team()
        copy = COPY["ja"]

        names = set(ProjectMaster.objects.filter(team=team).values_list("project_name", flat=True))
        self.assertEqual(
            names,
            {
                STARTER_BLUEPRINT["name"],
                JAPANESE_STARTER_BLUEPRINT["name"],
                copy["project"],
                copy["private_project"],
            },
        )
        self.assertTrue(ProjectLabel.objects.filter(team=team, name=copy["label"]).exists())
        self.assertFalse(ProjectLabel.objects.filter(team=team, name=COPY["en"]["label"]).exists())

    def test_the_language_is_the_accounts_and_english_when_it_is_not_japanese(self):
        for language, expected in (
            ("ja", "ja"),
            ("ja-JP", "ja"),
            ("en", "en"),
            ("fr", "en"),
            ("", "en"),
            (None, "en"),
        ):
            self.user.language = language
            self.assertEqual(example_locale(self.user), expected, language)

    def test_both_languages_cover_the_same_keys(self):
        self.assertEqual(set(COPY["en"]), set(COPY["ja"]))
        for key in ("project_tasks", "private_project_tasks"):
            self.assertEqual(len(COPY["en"][key]), len(COPY["ja"][key]), key)

    # --- opt-in and failure ----------------------------------------------------------------

    def test_without_the_sample_data_box_none_of_it_is_made(self):
        team = self._create_team(with_starter=None)

        self.assertFalse(ProjectLabel.objects.filter(team=team).exists())
        self.assertFalse(MentionGroupMaster.objects.filter(team=team).exists())
        # Only the default "general" chat every new team gets (MIP-1691), none of the samples.
        self.assertEqual(
            list(
                Channel.objects.filter(team=team, kind=ChannelKind.GM).values_list(
                    "title", flat=True
                )
            ),
            ["general"],
        )
        self.assertFalse(PersonalNoteFolder.objects.filter(team=team, scope="team").exists())

    def test_a_failure_in_the_example_keeps_the_team_and_the_rest_of_the_starter(self):
        with mock.patch(
            "origin.services.starter_seeder.create_organisation_examples",
            side_effect=RuntimeError("boom"),
        ):
            team = self._create_team()

        names = set(ProjectMaster.objects.filter(team=team).values_list("project_name", flat=True))
        self.assertEqual(names, {STARTER_BLUEPRINT["name"], JAPANESE_STARTER_BLUEPRINT["name"]})
        self.assertTrue(PersonalNoteMaster.objects.filter(team=team).exists())
        self.assertFalse(ProjectLabel.objects.filter(team=team).exists())

    def test_a_half_made_example_leaves_nothing_behind(self):
        """The example is one unit: if it dies in the middle, the label made first is gone too."""
        with mock.patch(
            "origin.services.organisation_examples._create_group_chat",
            side_effect=RuntimeError("boom"),
        ):
            team = self._create_team()

        self.assertFalse(ProjectLabel.objects.filter(team=team).exists())
        self.assertFalse(MentionGroupMaster.objects.filter(team=team).exists())
        self.assertFalse(PersonalNoteFolder.objects.filter(team=team, scope="team").exists())
        names = set(ProjectMaster.objects.filter(team=team).values_list("project_name", flat=True))
        self.assertEqual(names, {STARTER_BLUEPRINT["name"], JAPANESE_STARTER_BLUEPRINT["name"]})


class JapaneseCopyMIP1712Tests(BaseAPITestCase):
    """MIP-1712: the Japanese sample milestone and bodies use Japanese headings, and the invite
    path named in the Japanese guide is the real one."""

    def _headings(self, node):
        found = []
        if isinstance(node, dict):
            if node.get("type") == "heading":
                found.append("".join(c.get("text", "") for c in node.get("content", [])))
            for v in node.values():
                found += self._headings(v)
        elif isinstance(node, list):
            for v in node:
                found += self._headings(v)
        return found

    def test_japanese_sample_milestones_have_no_english_headings(self):
        ja = COPY["ja"]
        for key in ("project_milestone", "private_project_milestone"):
            for h in self._headings(ja[key]["body"]):
                self.assertFalse(h.split(" ", 1)[-1].isascii(), h)

    def test_invite_path_names_the_real_button(self):
        import json

        from origin.services.guide_company_notes import _SETUP_JA

        text = json.dumps(_SETUP_JA, ensure_ascii=False)
        self.assertIn("メンバーを招待する", text)
        self.assertNotIn("「招待」を選び", text)
