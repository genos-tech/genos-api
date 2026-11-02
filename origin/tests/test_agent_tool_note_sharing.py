"""`list_note_access` and `manage_note_sharing` show and change who can open a note (MIP-883).

Sharing widens who can read and edit a note, so what matters is what is refused: anyone
but the owner, a stranger from another team, a second owner, oneself. Taking a share
back narrows access, so it must also tell the search index. The page's role handlers
call the same service, and the last class pins that what they answer did not change.
"""

from __future__ import annotations

from unittest import mock
from urllib.parse import urlencode

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.list_note_access import LIST_NOTE_ACCESS
from origin.search_engine.agent.tools.manage_note_sharing import MANAGE_NOTE_SHARING
from origin.services.note_sharing import SharingRefused, share_note, unshare_note
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER, get_effective_role

PERSONAL = 1
ROLE_URL = "/api/v2/note/role/"


class SharingBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.stranger = get_user_model().objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.stranger)
        self.note = self.make_note("Plan", owner=self.user)
        patcher = mock.patch("origin.services.note_sharing.purge_note")
        self.purge_note = patcher.start()
        self.addCleanup(patcher.stop)

    def make_note(self, title, *, owner, team=None, folder=None):
        note = PersonalNoteMaster.objects.create(
            team=team or self.team,
            owner=owner,
            title=title,
            body=[],
            folder_id=folder.folder_id if folder else None,
        )
        NotePermissionMaster.objects.create(
            team=note.team, user=owner, note_id=note.note_id, note_type=PERSONAL, role_id=ROLE_OWNER
        )
        return note

    def grant(self, note, user, role):
        return NotePermissionMaster.objects.create(
            team=self.team, user=user, note_id=note.note_id, note_type=PERSONAL, role_id=role
        )

    def ctx(self, user=None):
        return ToolContext(team_id=str(self.team.team_id), user_id=str((user or self.user).id))

    def access(self, args, user=None):
        return LIST_NOTE_ACCESS.run(args, self.ctx(user))

    def manage(self, args, user=None):
        return MANAGE_NOTE_SHARING.run(args, self.ctx(user))

    def refused(self, run, args, message, user=None):
        with self.assertRaises(ToolError) as caught:
            run(args, user)
        self.assertIn(message, str(caught.exception))

    def rows(self, note=None):
        return NotePermissionMaster.objects.filter(
            note_type=PERSONAL, note_id=(note or self.note).note_id
        )

    def share_args(self, who=None, role="viewer", note=None, **extra):
        return {
            "action": "share",
            "note_type": "personal",
            "note_id": (note or self.note).note_id,
            "user_id": str((who or self.user2).id),
            "role": role,
            **extra,
        }

    def unshare_args(self, who=None, note=None):
        return {
            "action": "unshare",
            "note_type": "personal",
            "note_id": (note or self.note).note_id,
            "user_id": str((who or self.user2).id),
        }


class ListingWhoCanOpenANote(SharingBase):
    def test_the_owner_sees_the_shares_in_role_order_and_may_change_them(self):
        self.grant(self.note, self.user2, ROLE_VIEWER)
        out = self.access({"note_type": "personal", "note_id": self.note.note_id})
        self.assertEqual(
            [(p["username"], p["role"]) for p in out["people"]],
            [("testuser", "owner"), ("otheruser", "viewer")],
        )
        self.assertEqual((out["your_role"], out["you_can_change_sharing"]), ("owner", True))
        self.assertNotIn("filed_in_team_folder", out)
        self.assertIn("shared with 1 other person.", out["__summary__"])

    def test_an_editor_sees_the_list_but_may_not_change_it(self):
        self.grant(self.note, self.user2, ROLE_EDITOR)
        out = self.access({"note_type": "personal", "note_id": self.note.note_id}, user=self.user2)
        self.assertEqual((out["your_role"], out["you_can_change_sharing"]), ("editor", False))
        self.assertIn("you can't change who it is shared with", out["__summary__"])

    def test_an_author_with_no_owner_row_cannot_change_it_either(self):
        """The page asks for an explicit Owner row, which is how the author is recorded."""
        legacy = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Old", body=[]
        )
        out = self.access({"note_type": "personal", "note_id": legacy.note_id})
        self.assertEqual((out["your_role"], out["you_can_change_sharing"]), ("author", False))

    def test_a_note_in_a_team_folder_names_the_folder_when_the_caller_can_see_it(self):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user2,
            name="Handbook",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility="public",
        )
        note = self.make_note("Policy", owner=self.user2, folder=folder)
        out = self.access({"note_type": "team", "note_id": note.note_id})
        self.assertEqual(
            out["filed_in_team_folder"], {"folder_id": folder.folder_id, "name": "Handbook"}
        )
        self.assertIn('Team Notes folder "Handbook"', out["__summary__"])

    def test_a_private_team_folder_the_caller_cannot_see_is_not_named(self):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user2,
            name="Board only",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility="private",
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=self.user2, role_id=ROLE_OWNER
        )
        note = self.make_note("Minutes", owner=self.user2, folder=folder)
        self.grant(note, self.user, ROLE_VIEWER)  # shared to the caller on its own
        out = self.access({"note_type": "shared", "note_id": note.note_id})
        self.assertNotIn("filed_in_team_folder", out)

    def test_a_note_in_a_my_notes_folder_names_no_team_folder(self):
        mine = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Drafts",
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
        )
        note = self.make_note("Draft", owner=self.user, folder=mine)
        out = self.access({"note_type": "personal", "note_id": note.note_id})
        self.assertNotIn("filed_in_team_folder", out)

    def test_shares_of_another_note_family_with_the_same_id_are_not_listed(self):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=self.note.note_id,
            note_type=2,
            role_id=ROLE_EDITOR,
        )
        out = self.access({"note_type": "personal", "note_id": self.note.note_id})
        self.assertEqual([p["username"] for p in out["people"]], ["testuser"])

    def test_people_come_in_role_order_whatever_order_they_were_added(self):
        third = get_user_model().objects.create_user(
            username="third", email="third@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=third)
        self.grant(self.note, self.user2, ROLE_VIEWER)  # first in, last in the list
        self.grant(self.note, third, ROLE_EDITOR)
        out = self.access({"note_type": "personal", "note_id": self.note.note_id})
        self.assertEqual([p["role"] for p in out["people"]], ["owner", "editor", "viewer"])

    def test_a_missing_note_someone_elses_and_another_teams_read_alike(self):
        private = self.make_note("Private", owner=self.user2)
        foreign = self.make_note("Foreign", owner=self.user, team=self.other_team)
        for note_id in (private.note_id, foreign.note_id, 987654):
            with self.subTest(note_id=note_id):
                self.refused(
                    self.access, {"note_type": "personal", "note_id": note_id}, "not found"
                )

    def test_it_is_a_read_and_asks_for_no_approval(self):
        self.assertFalse(LIST_NOTE_ACCESS.requires_approval)
        self.assertTrue(MANAGE_NOTE_SHARING.requires_approval)


class SharingANote(SharingBase):
    def test_the_owner_shares_it_and_the_person_can_then_open_it(self):
        self.assertIsNone(get_effective_role(self.user2.id, PERSONAL, self.note.note_id))
        out = self.manage(self.share_args(role="viewer"))
        self.assertEqual(self.rows().get(user=self.user2).role_id, ROLE_VIEWER)
        self.assertEqual(
            get_effective_role(self.user2.id, PERSONAL, self.note.note_id), ROLE_VIEWER
        )
        self.assertEqual((out["action"], out["role"]), ("share", "viewer"))
        self.assertIn('Shared "Plan" with otheruser as a viewer.', out["__summary__"])
        self.assertIn("not notified", out["__summary__"])

    def test_sharing_again_changes_the_role_and_adds_no_row(self):
        self.manage(self.share_args(role="viewer"))
        out = self.manage(self.share_args(role="editor"))
        self.assertEqual(self.rows().filter(user=self.user2).count(), 1)
        self.assertEqual(self.rows().get(user=self.user2).role_id, ROLE_EDITOR)
        self.assertIn("as an editor", out["__summary__"])

    def test_only_the_owner_may_grant(self):
        self.grant(self.note, self.user2, ROLE_EDITOR)
        third = get_user_model().objects.create_user(
            username="third", email="third@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=third)
        self.refused(
            self.manage,
            self.share_args(who=third),
            "Only the note owner can grant roles.",
            user=self.user2,
        )
        self.assertFalse(self.rows().filter(user=third).exists())

    def test_an_author_with_no_owner_row_cannot_grant(self):
        legacy = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Old", body=[]
        )
        self.refused(
            self.manage,
            self.share_args(note=legacy),
            "Only the note owner can grant roles.",
        )
        self.assertEqual(self.rows(legacy).count(), 0)

    def test_nobody_changes_their_own_role(self):
        self.refused(
            self.manage, self.share_args(who=self.user), "You cannot change your own role."
        )

    def test_someone_outside_the_team_cannot_be_given_access(self):
        self.refused(
            self.manage, self.share_args(who=self.stranger), "Target user is not in the same team."
        )
        self.assertFalse(self.rows().filter(user=self.stranger).exists())

    def test_a_person_who_left_the_team_cannot_be_given_access(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.refused(self.manage, self.share_args(), "Target user is not in the same team.")

    def test_there_is_no_second_owner(self):
        self.refused(
            self.manage, self.share_args(role="owner"), "`role` must be 'editor' or 'viewer'"
        )
        self.assertFalse(self.rows().filter(user=self.user2).exists())

    def test_the_arguments_are_checked(self):
        self.refused(
            self.manage, {**self.share_args(), "action": "grant"}, "`action` must be one of"
        )
        self.refused(self.manage, {**self.share_args(), "user_id": ""}, "`user_id` is required")
        self.refused(self.manage, {**self.share_args(), "user_id": "sam"}, "`user_id` is required")
        self.refused(self.manage, {**self.share_args(), "role": None}, "`role` must be")
        self.refused(self.manage, {**self.share_args(), "note_id": 987654}, "not found")

    def test_a_note_the_caller_cannot_read_reads_as_missing(self):
        private = self.make_note("Private", owner=self.user2)
        self.refused(self.manage, self.share_args(note=private), "not found")

    def test_the_approval_card_names_the_person_and_the_note(self):
        card = controller._friendly_arguments(
            self.share_args(), "manage_note_sharing", ctx=self.ctx()
        )
        self.assertEqual((card["user_id"], card["note_title"]), ("otheruser", "Plan"))


class StoppingASharedNote(SharingBase):
    def setUp(self):
        super().setUp()
        self.grant(self.note, self.user2, ROLE_VIEWER)

    def test_the_owner_takes_the_share_back_and_search_is_told(self):
        out = self.manage(self.unshare_args())
        self.assertFalse(self.rows().filter(user=self.user2).exists())
        self.assertIsNone(get_effective_role(self.user2.id, PERSONAL, self.note.note_id))
        self.purge_note.assert_called_once_with("personal", self.note.note_id)
        self.assertEqual(out["__summary__"], 'Stopped sharing "Plan" with otheruser.')

    def test_the_owners_own_row_survives(self):
        self.manage(self.unshare_args())
        self.assertEqual(self.rows().get().user_id, self.user.id)

    def test_only_the_owner_may_revoke(self):
        self.refused(
            self.manage,
            self.unshare_args(who=self.user2),
            "Only the note owner can revoke roles.",
            user=self.user2,
        )
        self.assertTrue(self.rows().filter(user=self.user2).exists())
        self.purge_note.assert_not_called()

    def test_the_owner_cannot_revoke_themselves(self):
        self.refused(
            self.manage, self.unshare_args(who=self.user), "Owners cannot revoke themselves."
        )
        self.purge_note.assert_not_called()

    def test_someone_with_no_share_is_not_found_and_search_is_not_told(self):
        self.rows().filter(user=self.user2).delete()
        self.refused(self.manage, self.unshare_args(), "No matching role found.")
        self.purge_note.assert_not_called()

    def test_another_notes_share_is_left_alone(self):
        other = self.make_note("Other", owner=self.user)
        self.grant(other, self.user2, ROLE_VIEWER)
        self.manage(self.unshare_args())
        self.assertTrue(self.rows(other).filter(user=self.user2).exists())


class TheServiceChecksItself(SharingBase):
    """The tool resolves the note first, which would hide a missing check in the service."""

    def test_share_refusals_carry_the_pages_messages_and_statuses(self):
        cases = [
            (
                dict(granter_id=self.user2.id, target_user_id=self.user.id, role_id=ROLE_VIEWER),
                403,
                "Only the note owner can grant roles.",
            ),
            (
                dict(granter_id=self.user.id, target_user_id=self.user.id, role_id=ROLE_VIEWER),
                400,
                "You cannot change your own role.",
            ),
            (
                dict(granter_id=self.user.id, target_user_id=self.stranger.id, role_id=ROLE_VIEWER),
                400,
                "Target user is not in the same team.",
            ),
            (
                dict(granter_id=self.user.id, target_user_id=self.user2.id, role_id=ROLE_OWNER),
                400,
                "Owner transfer is not supported in this version.",
            ),
        ]
        for kwargs, status, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(SharingRefused) as caught:
                    share_note(
                        team_id=self.team.team_id,
                        note_type=PERSONAL,
                        note_id=self.note.note_id,
                        **kwargs,
                    )
                self.assertEqual(
                    (caught.exception.status, caught.exception.message), (status, message)
                )
        self.assertEqual(self.rows().count(), 1)

    def test_a_person_who_does_not_exist_is_refused_as_not_on_the_team(self):
        import uuid

        with self.assertRaises(SharingRefused) as caught:
            share_note(
                team_id=self.team.team_id,
                granter_id=self.user.id,
                note_type=PERSONAL,
                note_id=self.note.note_id,
                target_user_id=uuid.uuid4(),
                role_id=ROLE_VIEWER,
            )
        self.assertEqual(caught.exception.status, 400)  # not on the team, so refused first

    def test_unshare_refusals_carry_the_pages_messages_and_statuses(self):
        self.grant(self.note, self.user2, ROLE_VIEWER)
        for kwargs, status, message in [
            (
                dict(granter_id=self.user2.id, target_user_id=self.user.id),
                403,
                "Only the note owner can revoke roles.",
            ),
            (
                dict(granter_id=self.user.id, target_user_id=self.user.id),
                400,
                "Owners cannot revoke themselves.",
            ),
            (
                dict(granter_id=self.user.id, target_user_id=self.stranger.id),
                404,
                "No matching role found.",
            ),
        ]:
            with self.subTest(message=message):
                with self.assertRaises(SharingRefused) as caught:
                    unshare_note(note_type=PERSONAL, note_id=self.note.note_id, **kwargs)
                self.assertEqual(
                    (caught.exception.status, caught.exception.message), (status, message)
                )
        self.assertEqual(self.rows().count(), 2)

    def test_taking_a_share_back_leaves_the_same_ids_share_in_another_family(self):
        self.grant(self.note, self.user2, ROLE_VIEWER)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=self.note.note_id,
            note_type=2,
            role_id=ROLE_VIEWER,
        )
        unshare_note(
            granter_id=self.user.id,
            note_type=PERSONAL,
            note_id=self.note.note_id,
            target_user_id=self.user2.id,
        )
        self.assertFalse(self.rows().filter(user=self.user2).exists())
        self.assertTrue(
            NotePermissionMaster.objects.filter(
                user=self.user2, note_type=2, note_id=self.note.note_id
            ).exists()
        )

    def test_the_grant_is_filed_under_the_given_team(self):
        share_note(
            team_id=self.team.team_id,
            granter_id=self.user.id,
            note_type=PERSONAL,
            note_id=self.note.note_id,
            target_user_id=self.user2.id,
            role_id=ROLE_EDITOR,
        )
        self.assertEqual(str(self.rows().get(user=self.user2).team_id), str(self.team.team_id))


class ThePagesRoleHandlersDidNotChange(SharingBase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def revoke_url(self):
        return (
            ROLE_URL
            + "?"
            + urlencode(
                {
                    "team_id": self.team.team_id,
                    "note_type": PERSONAL,
                    "note_id": self.note.note_id,
                    "target_user_id": str(self.user2.id),
                }
            )
        )

    def body(self, who, role=ROLE_VIEWER, note=None):
        return {
            "team_id": self.team.team_id,
            "note_type": PERSONAL,
            "note_id": (note or self.note).note_id,
            "target_user_id": str(who.id),
            "role_id": role,
        }

    def test_the_page_grants_and_the_tool_sees_it(self):
        res = self.client.post(ROLE_URL, self.body(self.user2, ROLE_EDITOR), format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["roleId"], ROLE_EDITOR)
        out = self.access({"note_type": "personal", "note_id": self.note.note_id})
        self.assertIn(("otheruser", "editor"), [(p["username"], p["role"]) for p in out["people"]])

    def test_the_page_and_the_tool_refuse_alike(self):
        for who, role, message in (
            (self.user, ROLE_VIEWER, "You cannot change your own role."),
            (self.stranger, ROLE_VIEWER, "Target user is not in the same team."),
            (self.user2, ROLE_OWNER, "Owner transfer is not supported in this version."),
        ):
            with self.subTest(message=message):
                res = self.client.post(ROLE_URL, self.body(who, role), format="json")
                self.assertEqual((res.status_code, res.data["error"]), (400, message))

    def test_the_page_still_demands_the_owner(self):
        self.grant(self.note, self.user2, ROLE_EDITOR)
        self.authenticate(self.user2)
        res = self.client.post(ROLE_URL, self.body(self.user2), format="json")
        self.assertEqual(
            (res.status_code, res.data["error"]), (403, "Only the note owner can grant roles.")
        )

    def test_the_page_revoke_removes_the_share_and_now_tells_search(self):
        self.grant(self.note, self.user2, ROLE_VIEWER)
        res = self.client.delete(self.revoke_url())
        self.assertEqual(res.status_code, 204)
        self.assertFalse(self.rows().filter(user=self.user2).exists())
        self.purge_note.assert_called_once_with("personal", self.note.note_id)

    def test_the_page_revoke_of_nobody_is_still_a_404(self):
        res = self.client.delete(self.revoke_url())
        self.assertEqual((res.status_code, res.data["error"]), (404, "No matching role found."))
