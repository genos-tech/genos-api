"""`manage_note_folder` creates and renames note folders by the pages' rules (MIP-883).

Two spaces share one table. A My Notes folder is one person's; a Team Notes folder
carries the access rules for every note filed in it, so what the tool may do there is
the rule that matters: a new top-level team folder is private to its creator, a
subfolder needs Editor on its parent, and a rename needs Editor on the folder and
membership of the team. The pages' own handlers call the same service, and the last
class pins that the answers they give did not change.
"""

from __future__ import annotations

from unittest import mock
from urllib.parse import urlencode

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.manage_note_folder import MANAGE_NOTE_FOLDER
from origin.services.note_folder_manage import (
    FolderRefused,
    create_personal_folder,
    create_team_folder,
    rename_personal_folder,
    rename_team_folder,
)
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER

PERSONAL_FOLDER_URL = "/api/v2/note/personal/folder/"
TEAM_FOLDER_URL = "/api/v2/note/team/folder/"


class FolderBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.stranger = get_user_model().objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.stranger)

    def ctx(self, user=None, team=None):
        return ToolContext(
            team_id=str((team or self.team).team_id), user_id=str((user or self.user).id)
        )

    def run_tool(self, args, user=None, team=None):
        return MANAGE_NOTE_FOLDER.run(args, self.ctx(user, team))

    def refused(self, args, message, user=None, team=None):
        with self.assertRaises(ToolError) as caught:
            self.run_tool(args, user, team)
        self.assertIn(message, str(caught.exception))
        return str(caught.exception)

    def personal_folder(self, name="Mine", owner=None, parent=None, team=None):
        return PersonalNoteFolder.objects.create(
            team=team or self.team,
            owner=owner or self.user,
            name=name,
            parent_folder_id=parent,
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
        )

    def team_folder(self, name="Handbook", *, visibility="private", parent=None, team=None):
        folder = PersonalNoteFolder.objects.create(
            team=team or self.team,
            owner=self.user,
            name=name,
            parent_folder_id=parent,
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )
        NoteFolderPermission.objects.create(
            team=team or self.team, folder=folder, user=self.user, role_id=ROLE_OWNER
        )
        return folder

    def grant(self, folder, user, role):
        NoteFolderPermission.objects.create(
            team=folder.team, folder=folder, user=user, role_id=role
        )

    def count(self):
        return PersonalNoteFolder.objects.count()


class CreatingAMyNotesFolder(FolderBase):
    def test_a_top_level_folder_is_the_callers_own(self):
        out = self.run_tool({"action": "create", "name": "  Recipes  "})
        folder = PersonalNoteFolder.objects.get(folder_id=out["folder_id"])
        self.assertEqual(
            (folder.name, folder.scope, folder.owner_id, str(folder.team_id)),
            ("Recipes", "personal", self.user.id, str(self.team.team_id)),
        )
        self.assertIsNone(folder.parent_folder_id)
        self.assertIsNone(folder.visibility)
        self.assertEqual(out["space"], "personal")
        self.assertIn("My Notes", out["__summary__"])

    def test_a_folder_inside_one_of_the_callers_own_follows_it(self):
        parent = self.personal_folder("Work")
        out = self.run_tool(
            {"action": "create", "name": "Q4", "parent_folder_id": parent.folder_id}
        )
        self.assertEqual(out["parent_folder_id"], parent.folder_id)
        self.assertEqual(out["space"], "personal")

    def test_someone_elses_my_notes_folder_is_not_a_parent(self):
        theirs = self.personal_folder("Theirs", owner=self.user2)
        before = self.count()
        self.refused(
            {"action": "create", "name": "Sneak", "parent_folder_id": theirs.folder_id},
            f"Folder {theirs.folder_id} not found",
        )
        self.assertEqual(self.count(), before)

    def test_another_teams_folder_is_not_a_parent(self):
        foreign = self.personal_folder("Foreign", owner=self.stranger, team=self.other_team)
        self.refused(
            {"action": "create", "name": "x", "parent_folder_id": foreign.folder_id},
            f"Folder {foreign.folder_id} not found. Call list_note_folders",
        )

    def test_a_team_parent_cannot_be_given_a_personal_space(self):
        parent = self.team_folder()
        self.refused(
            {
                "action": "create",
                "name": "x",
                "space": "personal",
                "parent_folder_id": parent.folder_id,
            },
            "is in team notes",
        )

    def test_a_name_is_one_to_255_characters(self):
        for bad in ("", "   ", None, "x" * 256):
            with self.subTest(name=bad):
                before = self.count()
                self.refused({"action": "create", "name": bad}, "Folder name must be 1-255")
                self.assertEqual(self.count(), before)
        self.run_tool({"action": "create", "name": "y" * 255})

    def test_the_arguments_are_checked(self):
        self.refused({"action": "delete", "name": "x"}, "`action` must be one of")
        self.refused({"action": "create", "name": "x", "space": "shared"}, "`space` must be one of")
        self.refused(
            {"action": "create", "name": "x", "parent_folder_id": "abc"},
            "`parent_folder_id` must be an integer",
        )


class CreatingATeamNotesFolder(FolderBase):
    def test_a_top_level_team_folder_is_private_to_its_creator(self):
        out = self.run_tool({"action": "create", "name": "Handbook", "space": "team"})
        folder = PersonalNoteFolder.objects.get(folder_id=out["folder_id"])
        self.assertEqual((folder.scope, folder.visibility), ("team", "private"))
        self.assertEqual(out["visibility"], "private")
        self.assertEqual(
            NoteFolderPermission.objects.get(folder=folder, user=self.user).role_id, ROLE_OWNER
        )
        self.assertIsNone(
            get_effective_role_for_folder(self.user2, folder), "a teammate sees nothing yet"
        )
        self.assertIn("private to you", out["__summary__"])
        self.assertIn("Team Notes", out["__summary__"])

    def test_the_tool_cannot_make_a_top_level_folder_public(self):
        """There is no `visibility` argument to send, and a stray one changes nothing."""
        out = self.run_tool(
            {"action": "create", "name": "Open", "space": "team", "visibility": "public"}
        )
        self.assertEqual(out["visibility"], "private")
        self.assertEqual(
            PersonalNoteFolder.objects.get(folder_id=out["folder_id"]).visibility, "private"
        )

    def test_a_subfolder_inherits_from_its_parent(self):
        parent = self.team_folder("Everyone", visibility="public")
        out = self.run_tool(
            {"action": "create", "name": "Minutes", "parent_folder_id": parent.folder_id}
        )
        sub = PersonalNoteFolder.objects.get(folder_id=out["folder_id"])
        self.assertEqual(
            (sub.scope, sub.visibility, sub.parent_folder_id), ("team", None, parent.folder_id)
        )
        self.assertEqual(out["visibility"], "inherits")
        self.assertNotIn("private to you", out["__summary__"])
        self.assertEqual(
            get_effective_role_for_folder(self.user2, sub), ROLE_EDITOR, "public flows down"
        )

    def test_an_editor_may_add_a_subfolder_and_a_viewer_may_not(self):
        parent = self.team_folder("Shared", visibility="private")
        self.grant(parent, self.user2, ROLE_VIEWER)
        before = self.count()
        self.refused(
            {"action": "create", "name": "x", "parent_folder_id": parent.folder_id},
            "You do not have permission to add folders here.",
            user=self.user2,
        )
        self.assertEqual(self.count(), before)

        NoteFolderPermission.objects.filter(folder=parent, user=self.user2).update(
            role_id=ROLE_EDITOR
        )
        self.run_tool(
            {"action": "create", "name": "ok", "parent_folder_id": parent.folder_id},
            user=self.user2,
        )
        self.assertEqual(self.count(), before + 1)

    def test_a_teammate_with_no_role_may_not_add_a_subfolder(self):
        parent = self.team_folder("Closed", visibility="private")
        self.refused(
            {"action": "create", "name": "x", "parent_folder_id": parent.folder_id},
            "You do not have permission to add folders here.",
            user=self.user2,
        )

    def test_a_top_level_team_folder_needs_membership(self):
        before = self.count()
        self.refused(
            {"action": "create", "name": "x", "space": "team"},
            "You are not a member of this team.",
            user=self.stranger,
        )
        self.assertEqual(self.count(), before)

    def test_an_outside_editor_adds_a_subfolder_that_grants_nobody(self):
        parent = self.team_folder("Shared out", visibility="private")
        self.grant(parent, self.stranger, ROLE_EDITOR)
        out = self.run_tool(
            {"action": "create", "name": "Theirs", "parent_folder_id": parent.folder_id},
            user=self.stranger,
        )
        self.assertIsNone(PersonalNoteFolder.objects.get(folder_id=out["folder_id"]).visibility)

    def test_a_refusal_after_the_name_check_creates_nothing(self):
        parent = self.team_folder("Closed", visibility="private")
        before = (self.count(), NoteFolderPermission.objects.count())
        self.refused(
            {"action": "create", "name": "x", "parent_folder_id": parent.folder_id},
            "permission",
            user=self.user2,
        )
        self.assertEqual((self.count(), NoteFolderPermission.objects.count()), before)


class RenamingAFolder(FolderBase):
    def test_the_callers_own_folder_is_renamed(self):
        folder = self.personal_folder("Old")
        stamp = folder.ts_updated_at
        out = self.run_tool({"action": "rename", "folder_id": folder.folder_id, "name": " New "})
        folder.refresh_from_db()
        self.assertEqual(folder.name, "New")
        self.assertEqual(out["previous_name"], "Old")
        self.assertEqual(out["name"], "New")
        self.assertGreater(folder.ts_updated_at, stamp, "the edit is stamped")
        self.assertIn('"Old" to "New"', out["__summary__"])

    def test_someone_elses_my_notes_folder_reads_as_missing(self):
        theirs = self.personal_folder("Theirs", owner=self.user2)
        self.refused(
            {"action": "rename", "folder_id": theirs.folder_id, "name": "Mine now"}, "not found"
        )
        theirs.refresh_from_db()
        self.assertEqual(theirs.name, "Theirs")

    def test_another_teams_folder_reads_as_missing(self):
        foreign = self.team_folder("Foreign", team=self.other_team)
        self.refused(
            {"action": "rename", "folder_id": foreign.folder_id, "name": "x"},
            f"Folder {foreign.folder_id} not found. Call list_note_folders",
        )
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, "Foreign")

    def test_a_team_folder_is_renamed_by_its_owner_and_by_an_editor(self):
        folder = self.team_folder("Docs", visibility="public")
        self.run_tool({"action": "rename", "folder_id": folder.folder_id, "name": "Docs v2"})
        # Public gives every teammate Editor.
        self.run_tool(
            {"action": "rename", "folder_id": folder.folder_id, "name": "Docs v3"}, user=self.user2
        )
        folder.refresh_from_db()
        self.assertEqual(folder.name, "Docs v3")

    def test_a_viewer_and_a_teammate_with_no_role_may_not(self):
        folder = self.team_folder("Docs", visibility="private")
        self.refused(
            {"action": "rename", "folder_id": folder.folder_id, "name": "x"},
            "You do not have permission to modify this folder.",
            user=self.user2,
        )
        self.grant(folder, self.user2, ROLE_VIEWER)
        self.refused(
            {"action": "rename", "folder_id": folder.folder_id, "name": "x"},
            "You do not have permission to modify this folder.",
            user=self.user2,
        )
        folder.refresh_from_db()
        self.assertEqual(folder.name, "Docs")

    def test_an_editor_from_outside_the_team_may_not(self):
        folder = self.team_folder("Docs", visibility="private")
        self.grant(folder, self.stranger, ROLE_EDITOR)
        self.refused(
            {"action": "rename", "folder_id": folder.folder_id, "name": "x"},
            "Only members of the owning team can manage this folder.",
            user=self.stranger,
        )

    def test_a_bad_name_changes_nothing(self):
        folder = self.personal_folder("Keep")
        team = self.team_folder("Keep too")
        for target in (folder, team):
            for bad in ("", "  ", "z" * 256):
                with self.subTest(folder=target.name, name=bad):
                    self.refused(
                        {"action": "rename", "folder_id": target.folder_id, "name": bad},
                        "Folder name must be 1-255",
                    )
            target.refresh_from_db()
            self.assertTrue(target.name.startswith("Keep"))

    def test_the_folder_must_exist_and_be_a_number(self):
        self.refused({"action": "rename", "folder_id": 987654, "name": "x"}, "not found")
        self.refused({"action": "rename", "folder_id": "abc", "name": "x"}, "`folder_id` must be")
        self.refused({"action": "rename", "name": "x"}, "`folder_id` must be")

    def test_renaming_leaves_everything_else_about_the_folder_alone(self):
        parent = self.team_folder("Parent", visibility="public")
        child = self.team_folder("Child", visibility=None, parent=parent.folder_id)
        self.run_tool({"action": "rename", "folder_id": child.folder_id, "name": "Kid"})
        child.refresh_from_db()
        self.assertEqual(
            (child.parent_folder_id, child.visibility, child.scope, child.owner_id),
            (parent.folder_id, None, "team", self.user.id),
        )


class TheServiceChecksItself(FolderBase):
    """The tool looks things up before it calls the service, which would hide a missing
    check in the service. The pages call it with whatever the request carried."""

    def test_a_team_folder_is_not_a_parent_for_a_my_notes_folder(self):
        team_parent = self.team_folder("Team")
        with self.assertRaises(FolderRefused) as caught:
            create_personal_folder(
                team_id=self.team.team_id,
                owner_id=self.user.id,
                name="x",
                parent_folder_id=team_parent.folder_id,
            )
        self.assertEqual(
            (caught.exception.message, caught.exception.status), ("Parent folder not found.", 400)
        )

    def test_a_my_notes_folder_is_renamed_only_by_its_owner_in_its_own_team(self):
        theirs = self.personal_folder("Theirs", owner=self.user2)
        mine = self.personal_folder("Mine")
        for args in (
            dict(team_id=self.team.team_id, owner_id=self.user.id, folder_id=theirs.folder_id),
            dict(team_id=self.other_team.team_id, owner_id=self.user.id, folder_id=mine.folder_id),
        ):
            with self.subTest(args=args):
                with self.assertRaises(FolderRefused) as caught:
                    rename_personal_folder(name="Taken", **args)
                self.assertEqual(
                    (caught.exception.message, caught.exception.status), ("Folder not found.", 404)
                )
        theirs.refresh_from_db()
        mine.refresh_from_db()
        self.assertEqual((theirs.name, mine.name), ("Theirs", "Mine"))

    def test_a_subfolder_belongs_to_its_parents_team_whatever_team_the_caller_is_viewing(self):
        parent = self.team_folder("Shared out", visibility="private")
        self.grant(parent, self.stranger, ROLE_EDITOR)
        folder, owning_team_id = create_team_folder(
            team_id=self.other_team.team_id,  # the team the outsider is viewing
            user_id=self.stranger.id,
            name="Theirs",
            parent_folder_id=parent.folder_id,
        )
        self.assertEqual(owning_team_id, str(self.team.team_id))
        self.assertEqual(str(folder.team_id), str(self.team.team_id))
        self.assertEqual(
            str(NoteFolderPermission.objects.get(folder=folder, user=self.stranger).team_id),
            str(self.team.team_id),
        )

    def test_an_outsider_cannot_choose_a_visibility_for_their_subfolder(self):
        parent = self.team_folder("Shared out", visibility="private")
        self.grant(parent, self.stranger, ROLE_EDITOR)
        for asked in ("public", "private"):
            with self.subTest(visibility=asked):
                folder, _ = create_team_folder(
                    team_id=self.team.team_id,
                    user_id=self.stranger.id,
                    name=f"Asked {asked}",
                    parent_folder_id=parent.folder_id,
                    visibility=asked,
                )
                self.assertIsNone(folder.visibility)

    def test_a_team_member_may_choose_a_visibility_for_a_subfolder(self):
        parent = self.team_folder("Open", visibility="public")
        folder, _ = create_team_folder(
            team_id=self.team.team_id,
            user_id=self.user.id,
            name="Narrower",
            parent_folder_id=parent.folder_id,
            visibility="private",
        )
        self.assertEqual(folder.visibility, "private")

    def test_a_team_folder_is_renamed_only_inside_its_own_team(self):
        folder = self.team_folder("Docs")
        with self.assertRaises(FolderRefused) as caught:
            rename_team_folder(
                team_id=self.other_team.team_id,
                user_id=self.user.id,
                folder_id=folder.folder_id,
                name="Taken",
            )
        self.assertEqual(
            (caught.exception.message, caught.exception.status), ("Folder not found.", 404)
        )
        folder.refresh_from_db()
        self.assertEqual(folder.name, "Docs")

    def test_a_team_rename_is_stamped(self):
        folder = self.team_folder("Docs")
        stamp = folder.ts_updated_at
        rename_team_folder(
            team_id=self.team.team_id,
            user_id=self.user.id,
            folder_id=folder.folder_id,
            name="Docs 2",
        )
        folder.refresh_from_db()
        self.assertGreater(folder.ts_updated_at, stamp)


class TheServiceBehindThePagesDidNotChange(FolderBase):
    """The pages call the same service; what they answer is what they answered."""

    def setUp(self):
        super().setUp()
        self.authenticate()

    def params(self, user=None):
        return {"team_id": self.team.team_id, "user_id": str((user or self.user).id)}

    def test_the_personal_page_and_the_tool_refuse_a_bad_name_alike(self):
        res = self.client.post(PERSONAL_FOLDER_URL, {**self.params(), "name": "  "}, format="json")
        self.assertEqual(
            (res.status_code, res.data["error"]), (400, "Folder name must be 1-255 characters.")
        )
        self.refused({"action": "create", "name": "  "}, res.data["error"])

    def test_the_personal_page_and_the_tool_refuse_a_missing_parent_alike(self):
        res = self.client.post(
            PERSONAL_FOLDER_URL,
            {**self.params(), "name": "x", "parent_folder_id": 987654},
            format="json",
        )
        self.assertEqual((res.status_code, res.data["error"]), (400, "Parent folder not found."))

    def test_a_folder_the_page_makes_is_the_same_kind_of_row_the_tool_makes(self):
        page = self.client.post(
            PERSONAL_FOLDER_URL, {**self.params(), "name": "Page"}, format="json"
        )
        self.assertEqual(page.status_code, 201)
        tool = self.run_tool({"action": "create", "name": "Tool"})
        made = [
            PersonalNoteFolder.objects.get(folder_id=page.data["folderId"]),
            PersonalNoteFolder.objects.get(folder_id=tool["folder_id"]),
        ]
        self.assertEqual(
            {(f.scope, f.owner_id, f.team_id, f.parent_folder_id, f.visibility) for f in made},
            {("personal", self.user.id, self.team.team_id, None, None)},
        )

    def test_the_team_page_and_the_tool_refuse_a_viewer_alike(self):
        parent = self.team_folder("Shared", visibility="private")
        self.grant(parent, self.user2, ROLE_VIEWER)
        self.authenticate(self.user2)
        res = self.client.post(
            TEAM_FOLDER_URL,
            {**self.params(self.user2), "name": "x", "parent_folder_id": parent.folder_id},
            format="json",
        )
        self.assertEqual(
            (res.status_code, res.data["error"]),
            (403, "You do not have permission to add folders here."),
        )
        self.refused(
            {"action": "create", "name": "x", "parent_folder_id": parent.folder_id},
            res.data["error"],
            user=self.user2,
        )

    def test_the_team_page_still_makes_a_public_top_level_folder_and_a_roster(self):
        """What the tool leaves out, the page keeps: visibility and the roster."""
        res = self.client.post(
            TEAM_FOLDER_URL,
            {
                **self.params(),
                "name": "Everyone",
                "visibility": "public",
                "user_ids": [str(self.user2.id)],
                "role_id": ROLE_VIEWER,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        folder = PersonalNoteFolder.objects.get(folder_id=res.data["folderId"])
        self.assertEqual(folder.visibility, "public")
        self.assertEqual(
            NoteFolderPermission.objects.get(folder=folder, user=self.user2).role_id, ROLE_VIEWER
        )
        self.assertEqual(
            NoteFolderPermission.objects.get(folder=folder, user=self.user).role_id, ROLE_OWNER
        )

    def test_the_team_page_still_demands_a_visibility_for_a_top_level_folder(self):
        res = self.client.post(TEAM_FOLDER_URL, {**self.params(), "name": "x"}, format="json")
        self.assertEqual(
            (res.status_code, res.data["error"]),
            (400, 'A top-level folder must be "public" or "private".'),
        )

    def test_a_failure_after_the_folder_leaves_no_folder_behind(self):
        """What the page adds runs in the folder's transaction, as before."""
        before = (self.count(), NoteFolderPermission.objects.count())
        with mock.patch(
            "origin.views.note.team_note_folder_views.sync_folder_tags",
            side_effect=RuntimeError("tags"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    TEAM_FOLDER_URL,
                    {**self.params(), "name": "Half", "visibility": "private"},
                    format="json",
                )
        self.assertEqual((self.count(), NoteFolderPermission.objects.count()), before)

    def test_a_team_rename_by_a_viewer_gets_the_same_answer_from_the_page(self):
        folder = self.team_folder("Docs", visibility="private")
        self.grant(folder, self.user2, ROLE_VIEWER)
        self.authenticate(self.user2)
        res = self.client.put(
            TEAM_FOLDER_URL,
            {**self.params(self.user2), "folder_id": folder.folder_id, "name": "x"},
            format="json",
        )
        self.assertEqual(
            (res.status_code, res.data["error"]),
            (403, "You do not have permission to modify this folder."),
        )
        self.refused(
            {"action": "rename", "folder_id": folder.folder_id, "name": "x"},
            res.data["error"],
            user=self.user2,
        )

    def test_a_page_folder_listing_shows_a_tool_made_folder(self):
        out = self.run_tool({"action": "create", "name": "Seen"})
        res = self.client.get(f"{PERSONAL_FOLDER_URL}?{urlencode(self.params())}")
        self.assertIn(out["folder_id"], [f["folderId"] for f in res.data])


def get_effective_role_for_folder(user, folder):
    """The user's role on a team folder (None for no access)."""
    from origin.views.utils.note_folder_role import get_folder_role  # noqa: PLC0415

    return get_folder_role(user.id, folder.folder_id, folder.team_id)
