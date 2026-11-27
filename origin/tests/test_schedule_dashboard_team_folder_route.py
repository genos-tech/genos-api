"""A dashboard saved in a team folder is addressed by Team Notes (MIP-2408).

The page opens through `/workspace/notes/my/<id>` too, but the sidebar then does not show where it
is (and for a teammate "Shared with me" opens, empty). Pinned: the report's link, the chat notice's
chip and the dashboards list all say Team Notes for a note in a team folder the reader can open
(also under another note there), and say what they said before everywhere else.
"""

from __future__ import annotations

from rest_framework.test import APIClient

from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
from origin.models.common.schedule_models import MemberSchedule
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.services import schedule_dashboards
from origin.tests.test_schedule_dashboard_chat_notice import DashboardChatBase
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_VIEWER

LISTING = "/api/v2/schedules/dashboards/"


class TeamFolderRoute(DashboardChatBase):
    def folder(self, *, scope="team", members=None, owner=None):
        """A team folder only `members` (default: the project's) can open, so a project
        dashboard may be saved in it; or one of the owner's personal folders."""
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=owner or self.user,
            name=scope,
            scope=scope,
            visibility="restricted" if scope == "team" else None,
        )
        if scope == "team":
            for user in members or (self.user, self.user2):
                NoteFolderPermission.objects.create(
                    team=self.team, folder_id=folder.folder_id, user=user, role_id=1
                )
        return folder

    def board(self, channel=None, *, folder=None, save_to="me", name="Board"):
        params = {"kind": "project", "project_id": self.project.project_id, "period": "month"}
        if folder is not None:
            params.update({"save_to": save_to, "folder_id": folder.folder_id})
        return self.save(channel, job="ai_dashboard", name=name, params=params)

    def run_ok(self, sched):
        self.assertEqual(self.run_it(sched), ("ok", ""))
        sched.refresh_from_db()
        return PersonalNoteMaster.objects.get(note_id=sched.output["note_id"])

    def report_link(self):
        item = InboxItems.objects.filter(
            receiver=self.user, item_type=ITEM_TYPE_GENOS_REPORT
        ).latest("item_id")
        return item.item_body["sections"][0]["items"][0]["link"]

    def listing(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        res = client.get(f"{LISTING}?team_id={self.team.team_id}")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def test_the_report_link_of_a_team_folder_page_is_in_team_notes(self):
        note = self.run_ok(self.board(folder=self.folder(), save_to="team_folder"))
        self.assertEqual(self.report_link(), f"/workspace/notes/team/{note.note_id}")

    def test_the_report_link_elsewhere_is_unchanged(self):
        for sched in (
            self.board(name="Mine"),
            self.board(
                folder=self.folder(scope="personal"), save_to="personal_folder", name="Folder"
            ),
        ):
            note = self.run_ok(sched)
            self.assertEqual(self.report_link(), f"/workspace/notes/my/{note.note_id}")

    def test_the_chat_notice_chip_opens_team_notes(self):
        sched = self.board(self.pm, folder=self.folder(), save_to="team_folder")
        self.run_ok(sched)
        (message,) = self.posted()
        self.assertEqual(message.body[2]["content"][0]["props"]["noteKind"], "team")

    def test_the_list_says_which_pages_are_in_a_team_folder_for_owner_and_teammate(self):
        team_note = self.run_ok(self.board(folder=self.folder(), save_to="team_folder", name="T"))
        my_note = self.run_ok(self.board(name="M"))
        own = {row["note"]["noteId"]: row["note"] for row in self.listing(self.user)["own"]}
        self.assertIs(own[team_note.note_id]["inTeamFolder"], True)
        self.assertIs(own[my_note.note_id]["inTeamFolder"], False)
        (shared,) = self.listing(self.user2)["shared"]
        self.assertEqual(shared["noteId"], team_note.note_id)
        self.assertIs(shared["inTeamFolder"], True)

    def test_a_reader_without_the_folder_is_not_sent_to_team_notes(self):
        folder = self.folder(members=(self.user,))
        note = self.run_ok(self.board(folder=folder, save_to="team_folder"))
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_VIEWER,
        )
        (shared,) = self.listing(self.user2)["shared"]
        self.assertIs(shared["inTeamFolder"], False)
        self.assertIs(self.listing(self.user)["own"][0]["note"]["inTeamFolder"], True)

    def test_a_page_moved_under_a_note_of_a_team_folder_is_still_in_team_notes(self):
        sched = self.board(name="Moved")
        note = self.run_ok(sched)
        root = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Root", folder_id=self.folder().folder_id
        )
        PersonalNoteMaster.objects.filter(note_id=note.note_id).update(
            parent_note_id=root.note_id, folder_id=None
        )
        self.assertTrue(
            schedule_dashboards.in_team_space(
                str(self.user.id), str(self.team.team_id), note.note_id
            )
        )
        self.run_ok(MemberSchedule.objects.get(pk=sched.pk))
        self.assertEqual(self.report_link(), f"/workspace/notes/team/{note.note_id}")
