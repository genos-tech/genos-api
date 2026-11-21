"""The default chat and the departments a new team gets (MIP-1691, under MIP-1447).

What matters: every new team has one open "general" chat; each department the owner names is
the four things that make a department (a project tag, a mention group with the owner in it, an
open team folder, an open group chat); a bad list is refused BEFORE a team exists; and a failure
in the setup never costs the team or leaves a department half-made.
"""

from unittest import mock

from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.models.project.prj_models import ProjectLabel
from origin.services.team_setup import (
    GENERAL_TITLE,
    MAX_DEPARTMENTS,
    MAX_NAME_LENGTH,
    DepartmentsRefused,
    department_slug,
    parse_departments,
)
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_folder_role import get_folder_role


class TeamSetupTests(BaseAPITestCase):
    URL = "/api/v2/team/create/"

    def _create(self, **extra):
        self.authenticate()
        name = extra.pop("team_name", "Acme")
        payload = {
            "team_name": name,
            "team_email": f"{name.lower().replace(' ', '')}@example.com",
            "owner_id": str(self.user.id),
            **extra,
        }
        with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
            return self.client.post(self.URL, payload, format="json")

    def _team(self, response):
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return TeamMaster.objects.get(team_id=response.data["teamDetails"]["teamId"])

    def _chats(self, team):
        return Channel.objects.filter(team=team, kind=ChannelKind.GM)

    # --- the default chat -----------------------------------------------------------------

    def test_every_new_team_gets_one_open_general_chat(self):
        team = self._team(self._create())

        chat = self._chats(team).get(title=GENERAL_TITLE)
        self.assertFalse(chat.is_private)
        self.assertEqual(str(chat.owner_id), str(self.user.id))
        self.assertEqual(
            [(m.user_id, m.role) for m in ChannelMember.objects.filter(channel=chat)],
            [(self.user.id, "owner")],
        )
        # Not empty, so it shows in the list and says what it is for.
        self.assertEqual(Message.objects.filter(channel=chat).count(), 1)
        self.assertEqual(self._chats(team).count(), 1)

    def test_the_owner_is_a_member_so_the_follow_up_join_is_a_no_op(self):
        team = self._team(self._create())

        self.assertEqual(TeamMembers.objects.filter(team=team, attendee=self.user).count(), 1)

    def test_with_the_sample_data_there_is_still_exactly_one_general_chat(self):
        team = self._team(self._create(with_starter=True))

        self.assertEqual(self._chats(team).filter(title=GENERAL_TITLE).count(), 1)

    def test_a_japanese_account_gets_a_japanese_welcome_and_the_same_title(self):
        self.user.language = "ja"
        self.user.save(update_fields=["language"])
        team = self._team(self._create())

        chat = self._chats(team).get(title=GENERAL_TITLE)
        self.assertIn("チーム全体", Message.objects.get(channel=chat).body_text)

    # --- departments ----------------------------------------------------------------------

    def test_a_department_is_a_tag_a_group_a_folder_and_an_open_chat(self):
        team = self._team(self._create(departments=["Payments", "Customer Support"]))

        for name, slug in (("Payments", "payments"), ("Customer Support", "customer-support")):
            self.assertTrue(ProjectLabel.objects.filter(team=team, name=name).exists())

            group = MentionGroupMaster.objects.get(team=team, group_name=slug, is_deleted=False)
            self.assertEqual(
                list(
                    MentionGroupMembers.objects.filter(group=group).values_list(
                        "user_id", flat=True
                    )
                ),
                [self.user.id],
            )

            folder = PersonalNoteFolder.objects.get(team=team, scope="team", name=name)
            self.assertEqual(folder.visibility, "public")

            chat = self._chats(team).get(title=f"{slug} · general")
            self.assertFalse(chat.is_private)
            self.assertEqual(str(chat.owner_id), str(self.user.id))
            self.assertIn(name, Message.objects.get(channel=chat).body_text)

    def test_the_reply_says_how_many_departments_were_made(self):
        response = self._create(departments=["Payments", "Billing"])

        self.assertEqual(response.data["departments"], {"requested": 2, "created": 2})

    def test_no_departments_means_no_departments_and_no_summary(self):
        for extra in ({}, {"departments": []}, {"departments": ["", "   "]}, {"departments": None}):
            response = self._create(team_name=f"Acme{len(str(extra))}", **extra)
            team = self._team(response)

            self.assertNotIn("departments", response.data, extra)
            self.assertFalse(ProjectLabel.objects.filter(team=team).exists(), extra)
            self.assertFalse(MentionGroupMaster.objects.filter(team=team).exists(), extra)
            self.assertEqual(self._chats(team).count(), 1, extra)  # only "general"

    def test_a_name_twice_is_made_once_whatever_its_case(self):
        team = self._team(self._create(departments=["Sales", "sales", "  SALES  "]))

        self.assertEqual(ProjectLabel.objects.filter(team=team).count(), 1)
        self.assertEqual(MentionGroupMaster.objects.filter(team=team).count(), 1)

    def test_names_in_any_language_work(self):
        team = self._team(self._create(departments=["営業"]))

        self.assertTrue(MentionGroupMaster.objects.filter(team=team, group_name="営業").exists())
        self.assertTrue(self._chats(team).filter(title="営業 · general").exists())

    def test_a_teammate_can_open_a_department_folder(self):
        team = self._team(self._create(departments=["Payments"]))
        other = CustomUser.objects.create_user(
            username="teammate", email="teammate@example.com", password="x"
        )
        TeamMembers.objects.create(team=team, attendee=other)

        folder = PersonalNoteFolder.objects.get(team=team, scope="team", name="Payments")
        self.assertIsNotNone(get_folder_role(other.id, folder.folder_id, team.team_id))

    def test_the_boundary_is_twenty_departments(self):
        team = self._team(self._create(departments=[f"Dept {i}" for i in range(MAX_DEPARTMENTS)]))
        self.assertEqual(ProjectLabel.objects.filter(team=team).count(), MAX_DEPARTMENTS)

        before = TeamMaster.objects.count()
        response = self._create(
            team_name="TooMany", departments=[f"Dept {i}" for i in range(MAX_DEPARTMENTS + 1)]
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(TeamMaster.objects.count(), before)

    def test_a_bad_list_is_refused_before_any_team_exists(self):
        bad = {
            "not a list": "Payments",
            "not text": ["Payments", 5],
            "too long": ["x" * (MAX_NAME_LENGTH + 1)],
            "no letter or digit": ["###"],
            "too many": [f"Dept {i}" for i in range(MAX_DEPARTMENTS + 1)],
        }
        codes = {
            "not a list": "departments_not_a_list",
            "not text": "departments_not_a_list",
            "too long": "department_name_too_long",
            "no letter or digit": "department_name_invalid",
            "too many": "too_many_departments",
        }
        for label, departments in bad.items():
            before = TeamMaster.objects.count()
            response = self._create(team_name="Refused", departments=departments)

            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, label)
            self.assertIn("error", response.data, label)
            self.assertEqual(response.data["field"], "departments", label)
            self.assertEqual(response.data["code"], codes[label], label)
            self.assertEqual(TeamMaster.objects.count(), before, label)

    def test_a_name_at_the_length_limit_is_accepted(self):
        team = self._team(self._create(departments=["x" * MAX_NAME_LENGTH]))

        self.assertTrue(ProjectLabel.objects.filter(team=team, name="x" * MAX_NAME_LENGTH).exists())

    def test_with_the_sample_data_a_department_that_already_exists_is_skipped(self):
        """The sample data makes a "Sample dept" tag and an @sample-dept group; naming the same
        department must not fail the other departments or make a second one."""
        response = self._create(with_starter=True, departments=["Sample dept", "Payments"])
        team = self._team(response)

        self.assertEqual(response.data["departments"], {"requested": 2, "created": 1})
        self.assertEqual(
            ProjectLabel.objects.filter(team=team, name__iexact="sample dept").count(), 1
        )
        self.assertTrue(ProjectLabel.objects.filter(team=team, name="Payments").exists())

    # --- failure ------------------------------------------------------------------------------

    def test_a_failing_department_setup_keeps_the_team_and_the_general_chat(self):
        with mock.patch(
            "origin.services.team_setup.create_departments", side_effect=RuntimeError("boom")
        ):
            response = self._create(departments=["Payments"])
        team = self._team(response)

        self.assertEqual(response.data["departments"], {"requested": 1, "created": 0})
        self.assertTrue(self._chats(team).filter(title=GENERAL_TITLE).exists())
        self.assertFalse(ProjectLabel.objects.filter(team=team).exists())

    def test_a_setup_that_fails_outright_still_keeps_the_team(self):
        with mock.patch(
            "origin.views.common.team_views.set_up_new_team", side_effect=RuntimeError("boom")
        ):
            response = self._create(departments=["Payments"])
        team = self._team(response)

        self.assertTrue(TeamMaster.objects.filter(team_id=team.team_id).exists())
        self.assertEqual(response.data["departments"], {"requested": 1, "created": 0})

    def test_a_failing_general_chat_does_not_stop_the_departments(self):
        with mock.patch(
            "origin.services.team_setup.create_general_chat", side_effect=RuntimeError("boom")
        ):
            team = self._team(self._create(departments=["Payments"]))

        self.assertFalse(self._chats(team).filter(title=GENERAL_TITLE).exists())
        self.assertTrue(ProjectLabel.objects.filter(team=team, name="Payments").exists())

    def test_a_department_that_fails_halfway_leaves_none_of_them(self):
        real = __import__("origin.services.team_setup", fromlist=["x"])._create_group_chat
        calls = {"n": 0}

        def second_chat_fails(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 3:  # 1 = general, 2 = the first department, 3 = the second
                raise RuntimeError("boom")
            return real(*args, **kwargs)

        with mock.patch("origin.services.team_setup._create_group_chat", second_chat_fails):
            team = self._team(self._create(departments=["Payments", "Billing"]))

        self.assertFalse(ProjectLabel.objects.filter(team=team).exists())
        self.assertFalse(MentionGroupMaster.objects.filter(team=team).exists())
        self.assertFalse(PersonalNoteFolder.objects.filter(team=team, scope="team").exists())
        self.assertEqual(self._chats(team).count(), 1)  # only "general"


class ParseDepartmentsTests(BaseAPITestCase):
    def test_trims_drops_blanks_and_keeps_the_first_of_a_repeat(self):
        self.assertEqual(
            parse_departments(["  Payments ", "", "payments", "Billing"]), ["Payments", "Billing"]
        )

    def test_none_and_empty_mean_none(self):
        self.assertEqual(parse_departments(None), [])
        self.assertEqual(parse_departments([]), [])

    def test_refuses_what_it_cannot_use(self):
        for raw in ("Payments", {"a": 1}, ["ok", 3], ["x" * 31], ["!!!"]):
            with self.assertRaises(DepartmentsRefused):
                parse_departments(raw)

    def test_the_slug_is_a_mention_group_name(self):
        for name, slug in (
            ("Payments", "payments"),
            ("Customer Support", "customer-support"),
            ("R&D", "rd"),
            ("営業 部", "営業-部"),
            ("  a  /  b  ", "a-b"),
            ("snake_case", "snake_case"),
        ):
            self.assertEqual(department_slug(name), slug, name)
