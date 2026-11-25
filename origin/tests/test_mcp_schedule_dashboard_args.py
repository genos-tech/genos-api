"""`create_my_schedule` and `update_my_schedule` can make dashboard jobs (MIP-2358).

Pinned: an agent can create every dashboard kind with a chat target and a note place; the
service that checks the REST form checks them (a public folder, a chat the report may not go
to, a folder without its id are refused in words); `deliver_chat_id` and `save_to` are read back;
an update can move the chat or the folder alone without losing the other parameters; both tools
still need approval.
"""

from __future__ import annotations

from unittest import mock

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.common.schedule_models import MemberSchedule
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY
from origin.services.collab_push import PushOutcome
from origin.tests.test_mcp_schedule_tools import ScheduleToolBase

TIER_PATH = "origin.search_engine.quota.get_effective_tier"
PUSH = "origin.services.html_notes.push_note_body_now"


class DashboardArgsBase(ScheduleToolBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        for patcher in (
            mock.patch(TIER_PATH, return_value="core"),
            mock.patch(PUSH, return_value=PushOutcome.SKIPPED),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        # A private chat of the project's people only (a public chat is refused as a target).
        self.chat = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Squad", owner=self.user, is_private=True
        )
        for member in (self.user, self.user2):
            ChannelMember.objects.create(channel=self.chat, user=member)
        ProjectMembers.objects.get_or_create(
            team=self.team, project=self.project, attendee=self.user2
        )
        # A teammate who is NOT on the project: a public folder shows them what the page names.
        outsider = CustomUser.objects.create_user(
            username="outsider", email="outsider@example.com", password="pw-outsider-1"
        )
        TeamMembers.objects.create(team=self.team, attendee=outsider)
        backing = TaskMaster.objects.create(
            team=self.team, project=self.project, title="M1", status="Open", is_milestone=True
        )
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, task=backing, title="M1", reporter=self.user
        )

    def folder(self, visibility):
        return PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Dashboards",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )

    def dash(self, **over):
        args = {
            "name": "Board",
            "job": "ai_dashboard",
            "frequency": "weekly",
            "weekdays": [0],
            "time_of_day": "08:00",
            "params": {"kind": "mine", "period": "month"},
        }
        args.update(over)
        return args


class TestCreate(DashboardArgsBase):
    def test_every_dashboard_kind_can_be_made_with_a_chat_notice(self):
        kinds = {
            "mine": {"kind": "mine", "period": "week"},
            "project": {"kind": "project", "project_id": self.project.project_id},
            "milestone": {"kind": "milestone", "milestone_id": self.milestone.milestone_id},
        }
        for key, params in kinds.items():
            if key == "mine":
                # my progress is about the owner alone: never into a chat other people read
                self.refused(
                    "create_my_schedule",
                    **self.dash(name=key, params=params, deliver_chat_id=str(self.chat.id)),
                )
                continue
            row = self.ok(
                "create_my_schedule",
                **self.dash(name=key, params=params, deliver_chat_id=str(self.chat.id)),
            )["schedule"]
            self.assertEqual(row["deliver_chat_id"], str(self.chat.id))
            self.assertEqual(row["job"], "ai_dashboard")
        self.assertEqual(MemberSchedule.objects.filter(deliver_channel=self.chat).count(), 2)

    def test_a_dashboard_with_no_chat_goes_to_the_inbox_and_says_nothing_of_a_chat(self):
        row = self.ok("create_my_schedule", **self.dash())["schedule"]
        self.assertNotIn("deliver_chat_id", row)

    def test_save_to_a_folder_is_kept_in_the_params_and_read_back(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE)
        row = self.ok(
            "create_my_schedule",
            **self.dash(
                params={"kind": "project", "project_id": self.project.project_id},
                save_to="team_folder",
                folder_id=folder.folder_id,
            ),
        )["schedule"]
        self.assertEqual(row["params"]["save_to"], "team_folder")
        self.assertEqual(row["params"]["folder_id"], folder.folder_id)

    def test_the_services_own_rules_refuse_what_the_form_would_refuse(self):
        public = self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC)
        base = self.dash(params={"kind": "project", "project_id": self.project.project_id})
        # a public folder shows a project's tasks to people off the project
        self.refused(
            "create_my_schedule", **base, save_to="team_folder", folder_id=public.folder_id
        )
        # a folder without its id
        self.assertIn(
            "folder_id", self.refused("create_my_schedule", **base, save_to="team_folder")
        )
        # a chat that does not exist
        self.refused("create_my_schedule", **base, deliver_chat_id="not-a-chat")
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_the_same_key_twice_with_different_values_is_refused_not_guessed(self):
        text = self.refused(
            "create_my_schedule",
            **self.dash(
                params={"kind": "mine", "save_to": "me"}, save_to="team_folder", folder_id=1
            ),
        )
        self.assertIn("save_to", text)

    def test_the_tools_still_need_approval_and_take_the_new_arguments(self):
        for name in ("create_my_schedule", "update_my_schedule"):
            tool = MCP_ONLY_REGISTRY[name]
            self.assertTrue(tool.requires_approval)
            properties = tool.parameters_schema["properties"]
            for arg in ("deliver_chat_id", "save_to", "folder_id"):
                self.assertIn(arg, properties)


class TestUpdate(DashboardArgsBase):
    def setUp(self):
        super().setUp()
        self.sched = self.ok(
            "create_my_schedule",
            **self.dash(params={"kind": "project", "project_id": self.project.project_id}),
        )["schedule"]

    def test_the_chat_can_be_set_and_cleared_alone(self):
        row = self.ok(
            "update_my_schedule",
            schedule_id=self.sched["schedule_id"],
            deliver_chat_id=str(self.chat.id),
        )["schedule"]
        self.assertEqual(row["deliver_chat_id"], str(self.chat.id))
        self.assertEqual(row["params"]["project_id"], self.project.project_id)
        row = self.ok(
            "update_my_schedule", schedule_id=self.sched["schedule_id"], deliver_chat_id=""
        )["schedule"]
        self.assertNotIn("deliver_chat_id", row)

    def test_a_folder_can_be_chosen_without_resending_the_other_parameters(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE)
        row = self.ok(
            "update_my_schedule",
            schedule_id=self.sched["schedule_id"],
            save_to="team_folder",
            folder_id=folder.folder_id,
        )["schedule"]
        self.assertEqual(row["params"]["kind"], "project")
        self.assertEqual(row["params"]["project_id"], self.project.project_id)
        self.assertEqual(row["params"]["folder_id"], folder.folder_id)

    def test_a_refused_change_leaves_the_schedule_as_it_was(self):
        public = self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC)
        self.refused(
            "update_my_schedule",
            schedule_id=self.sched["schedule_id"],
            save_to="team_folder",
            folder_id=public.folder_id,
        )
        self.assertNotIn("save_to", MemberSchedule.objects.get().params)

    def test_someone_elses_schedule_is_still_not_found_with_the_new_arguments(self):
        self.refused(
            "update_my_schedule",
            schedule_id="00000000-0000-0000-0000-000000000000",
            deliver_chat_id=str(self.chat.id),
        )
