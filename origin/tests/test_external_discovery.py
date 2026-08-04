"""Finding a shared object from the guest team's OWN workspace.

The feature shipped able to share a project, a chat and a note folder, and
unable to show any of them to the team they were shared with. Two reasons,
and the second hid the first:

1. Accepting the offer admitted NOBODY — not even the person who clicked
   Approve. The roster that grants access lives inside the shared object's
   own profile, which you need access to open, so the only way to use a
   share was to already be using it.

2. Every list filtered on the team that OWNS the object, so a shared
   project could only be seen by noticing the host company in the team
   switcher and changing teams. Nothing said so, and it is the wrong
   place to look: people read their chats in their own sidebar.

These tests pin the outcome — "the team I am in shows me the work shared
with me" — and, just as importantly, that it shows me nothing else. Every
widening here is per-PERSON: the grant names a team, but a colleague who
was never admitted must still see nothing.
"""

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.common.team_models import ExternalGrant, ShareStatus
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.services.external_grants import (
    add_external_participants,
    offer_grant,
    respond_to_grant,
    revoke_grant,
)
from origin.tests.cross_team_fixtures import CrossTeamTestCase

PROJECTS = "/api/v2/project/projects/"
CHANNELS = "/api/v3/channels/"
TEAM_FOLDERS = "/api/v2/note/team/folder/"
TEAM_FOLDER_MEMBERS = "/api/v2/note/team/folder/member/"
TEAM_NOTE_META = "/api/v2/note/team/meta/"


class AcceptAdmitsTheApproverTests(CrossTeamTestCase):
    """Approve has to be the moment access starts, not a promise of it."""

    def test_accepting_a_project_share_admits_the_approver(self):
        grant = self.active_project_grant()
        self.assertIn(str(self.b_owner.id), _project_member_ids(self.project))
        self.assertEqual(grant.status, ShareStatus.ACTIVE)

    def test_accepting_a_chat_share_admits_the_approver(self):
        channel, _ = self.shared_chat()
        self.assertTrue(
            channel.members.filter(user=self.b_owner, is_deleted=False).exists(),
        )

    def test_accepting_a_folder_share_admits_the_approver(self):
        self.active_folder_grant()
        self.authenticate(self.b_owner)
        res = self.client.get(TEAM_FOLDERS, self._team_b_params())
        self.assertEqual(res.status_code, 200)
        self.assertIn(str(self.folder.folder_id), [str(f["folderId"]) for f in res.data])

    def test_declining_admits_nobody(self):
        self.connect_a_and_b()
        grant = offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.PROJECT,
            object_id=self.project.project_id,
            role_ceiling="editor",
            actor=self.a_owner,
        )
        respond_to_grant(grant, self.b_owner, accept=False)
        self.assertEqual(_project_member_ids(self.project), set())

    def test_the_approver_is_admitted_at_the_ceiling_not_above_it(self):
        """A viewer-ceiling share does not make its approver an editor."""
        channel, _ = self.shared_chat(role_ceiling="viewer")
        member = channel.members.get(user=self.b_owner)
        self.assertEqual(member.role, "member")

    def _team_b_params(self):
        return {"team_id": str(self.team_b.team_id), "user_id": str(self.b_owner.id)}


class SharedProjectInOwnWorkspaceTests(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()
        self.b_project = ProjectMaster.objects.create(
            team=self.team_b,
            project_name="Our Own Project",
            owner=self.b_owner,
            project_system_user=self.b_owner,
        )

    def _list_as(self, user):
        self.authenticate(user)
        return self.client.get(
            PROJECTS, {"team_id": str(self.team_b.team_id), "attendee_id": str(user.id)}
        )

    def test_the_approver_sees_it_beside_their_own_projects(self):
        res = self._list_as(self.b_owner)
        self.assertEqual(res.status_code, 200)
        by_id = {p["projectId"]: p for p in res.data}
        self.assertIn(self.project.project_id, by_id)
        self.assertIn(self.b_project.project_id, by_id)

    def test_it_is_labelled_with_the_team_that_owns_it(self):
        row = self._row(self._list_as(self.b_owner).data, self.project.project_id)
        self.assertTrue(row["isExternal"])
        self.assertEqual(row["hostTeamId"], str(self.team_a.team_id))
        self.assertEqual(row["hostTeamName"], self.team_a.team_name)
        self.assertTrue(row["isJoined"])

    def test_an_ordinary_project_is_not_labelled_at_all(self):
        """Absent, not false — see `_external_fields`."""
        row = self._row(self._list_as(self.b_owner).data, self.b_project.project_id)
        self.assertNotIn("isExternal", row)
        self.assertNotIn("hostTeamName", row)

    def test_a_colleague_who_was_never_admitted_does_not_see_it(self):
        """The share names team B; reaching it is still per-person."""
        ids = [p["projectId"] for p in self._list_as(self.b_editor).data]
        self.assertNotIn(self.project.project_id, ids)
        self.assertIn(self.b_project.project_id, ids)

    def test_an_admitted_colleague_does(self):
        add_external_participants(self.grant, [self.b_editor.id], self.b_owner)
        ids = [p["projectId"] for p in self._list_as(self.b_editor).data]
        self.assertIn(self.project.project_id, ids)

    def test_the_host_team_is_not_otherwise_exposed(self):
        """Only the shared project — never the rest of the host's work."""
        internal = ProjectMaster.objects.create(
            team=self.team_a,
            project_name="Host Internal",
            owner=self.a_owner,
            project_system_user=self.a_owner,
        )
        ids = [p["projectId"] for p in self._list_as(self.b_owner).data]
        self.assertNotIn(internal.project_id, ids)

    def test_revoking_takes_it_out_of_the_list(self):
        revoke_grant(self.grant, self.a_owner)
        ids = [p["projectId"] for p in self._list_as(self.b_owner).data]
        self.assertNotIn(self.project.project_id, ids)

    def test_a_stranger_team_sees_nothing_of_it(self):
        self.authenticate(self.c_owner)
        res = self.client.get(
            PROJECTS, {"team_id": str(self.team_c.team_id), "attendee_id": str(self.c_owner.id)}
        )
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(self.project.project_id, [p["projectId"] for p in res.data])

    def _row(self, payload, project_id):
        for row in payload:
            if row["projectId"] == project_id:
                return row
        self.fail(f"project {project_id} missing from {[p['projectId'] for p in payload]}")


class SharedChatInOwnWorkspaceTests(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.channel, self.grant = self.shared_chat()

    def _list_as(self, user):
        self.authenticate(user)
        return self.client.get(CHANNELS, {"team_id": str(self.team_b.team_id)})

    def test_the_approver_sees_the_chat_in_their_own_sidebar(self):
        res = self._list_as(self.b_owner)
        self.assertEqual(res.status_code, 200)
        self.assertIn(str(self.channel.id), [c["id"] for c in res.data["channels"]])

    def test_it_names_the_team_whose_room_it_is(self):
        row = next(
            c
            for c in self._list_as(self.b_owner).data["channels"]
            if c["id"] == str(self.channel.id)
        )
        self.assertTrue(row["isExternal"])
        self.assertEqual(row["teamId"], str(self.team_a.team_id))
        self.assertEqual(row["hostTeamName"], self.team_a.team_name)

    def test_a_colleague_who_was_never_admitted_does_not(self):
        ids = [c["id"] for c in self._list_as(self.b_editor).data["channels"]]
        self.assertNotIn(str(self.channel.id), ids)

    def test_an_internal_chat_of_the_host_stays_invisible(self):
        internal = Channel.objects.create(
            kind=ChannelKind.GM,
            team=self.team_a,
            title="Host only",
            owner=self.a_owner,
        )
        ids = [c["id"] for c in self._list_as(self.b_owner).data["channels"]]
        self.assertNotIn(str(internal.id), ids)

    def test_revoking_takes_it_out_of_the_sidebar(self):
        revoke_grant(self.grant, self.a_owner)
        ids = [c["id"] for c in self._list_as(self.b_owner).data["channels"]]
        self.assertNotIn(str(self.channel.id), ids)

    def test_the_host_still_sees_it_under_their_own_team(self):
        """Widening the guest's list must not disturb the owner's."""
        self.authenticate(self.a_owner)
        res = self.client.get(CHANNELS, {"team_id": str(self.team_a.team_id)})
        self.assertIn(str(self.channel.id), [c["id"] for c in res.data["channels"]])

    def test_the_host_row_is_not_labelled_as_somebody_elses(self):
        """Both sides are external; only the guest's side is "theirs"."""
        self.authenticate(self.a_owner)
        res = self.client.get(CHANNELS, {"team_id": str(self.team_a.team_id)})
        row = next(c for c in res.data["channels"] if c["id"] == str(self.channel.id))
        self.assertTrue(row["isExternal"])
        self.assertIsNone(row["hostTeamName"])


class SharedFolderInOwnWorkspaceTests(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.grant = self.active_folder_grant()
        self.child = PersonalNoteFolder.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            name="Inside The Share",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            parent_folder_id=self.folder.folder_id,
        )
        self.host_private = PersonalNoteFolder.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            name="Host Private",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE,
        )
        self.host_public = PersonalNoteFolder.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            name="Host Handbook",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )

    def _list_as(self, user):
        self.authenticate(user)
        return self.client.get(
            TEAM_FOLDERS, {"team_id": str(self.team_b.team_id), "user_id": str(user.id)}
        )

    def test_the_shared_folder_appears_in_the_guests_own_team_notes(self):
        rows = self._list_as(self.b_owner).data
        self.assertIn(str(self.folder.folder_id), [str(f["folderId"]) for f in rows])

    def test_it_is_re_rooted_because_its_real_parent_is_invisible(self):
        row = self._row(self._list_as(self.b_owner).data, self.folder.folder_id)
        self.assertIsNone(row["parentFolderId"])
        self.assertTrue(row["isExternal"])
        self.assertEqual(row["hostTeamName"], self.team_a.team_name)

    def test_a_subfolder_of_the_share_comes_with_it_and_keeps_its_parent(self):
        row = self._row(self._list_as(self.b_owner).data, self.child.folder_id)
        self.assertEqual(str(row["parentFolderId"]), str(self.folder.folder_id))

    def test_the_hosts_other_folders_do_not_come_with_it(self):
        """Including the PUBLIC one: public means the host's team, not ours."""
        ids = [str(f["folderId"]) for f in self._list_as(self.b_owner).data]
        self.assertNotIn(str(self.host_private.folder_id), ids)
        self.assertNotIn(str(self.host_public.folder_id), ids)

    def test_a_colleague_who_was_never_admitted_sees_none_of_it(self):
        ids = [str(f["folderId"]) for f in self._list_as(self.b_editor).data]
        self.assertNotIn(str(self.folder.folder_id), ids)

    def test_notes_in_the_shared_folder_are_listed_too(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            title="Kickoff",
            folder_id=self.folder.folder_id,
        )
        self.authenticate(self.b_owner)
        res = self.client.get(
            TEAM_NOTE_META,
            {"team_id": str(self.team_b.team_id), "user_id": str(self.b_owner.id)},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(str(note.note_id), [str(n["noteId"]) for n in res.data])

    def test_revoking_takes_the_folder_away(self):
        revoke_grant(self.grant, self.a_owner)
        ids = [str(f["folderId"]) for f in self._list_as(self.b_owner).data]
        self.assertNotIn(str(self.folder.folder_id), ids)

    def _row(self, payload, folder_id):
        for row in payload:
            if str(row["folderId"]) == str(folder_id):
                return row
        self.fail(f"folder {folder_id} missing from {[str(f['folderId']) for f in payload]}")


class GuestWritesInsideTheShareTests(CrossTeamTestCase):
    """The parity the product asked for: work in it, don't administer it."""

    def setUp(self):
        super().setUp()
        self.grant = self.active_folder_grant()

    def test_an_editor_guest_can_create_a_subfolder(self):
        self.authenticate(self.b_owner)
        res = self.client.post(
            TEAM_FOLDERS,
            {
                "team_id": str(self.team_b.team_id),
                "user_id": str(self.b_owner.id),
                "name": "Our Workstream",
                "parent_folder_id": str(self.folder.folder_id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        created = PersonalNoteFolder.objects.get(folder_id=res.data["folderId"])
        # Filed under the HOST's team, or it would sit in a tree neither
        # side of the share can see.
        self.assertEqual(str(created.team_id), str(self.team_a.team_id))
        self.assertEqual(str(created.parent_folder_id), str(self.folder.folder_id))

    def test_their_subfolder_inherits_and_cannot_be_made_public(self):
        """Public would mean "every member of a company they don't work for"."""
        self.authenticate(self.b_owner)
        res = self.client.post(
            TEAM_FOLDERS,
            {
                "team_id": str(self.team_b.team_id),
                "user_id": str(self.b_owner.id),
                "name": "Not Yours To Publish",
                "parent_folder_id": str(self.folder.folder_id),
                "visibility": PersonalNoteFolder.VISIBILITY_PUBLIC,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertIsNone(PersonalNoteFolder.objects.get(folder_id=res.data["folderId"]).visibility)

    def test_they_cannot_invite_a_third_party_into_the_hosts_data(self):
        self.authenticate(self.b_owner)
        res = self.client.post(
            TEAM_FOLDERS,
            {
                "team_id": str(self.team_b.team_id),
                "user_id": str(self.b_owner.id),
                "name": "Onward Share Attempt",
                "parent_folder_id": str(self.folder.folder_id),
                "user_ids": [str(self.c_owner.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        from origin.models.note.common_note_models import NoteFolderPermission

        self.assertFalse(
            NoteFolderPermission.objects.filter(
                folder_id=res.data["folderId"], user=self.c_owner
            ).exists()
        )

    def test_a_viewer_guest_cannot_create_a_subfolder(self):
        revoke_grant(self.grant, self.a_owner)
        self.active_folder_grant(role_ceiling="viewer")
        self.authenticate(self.b_owner)
        res = self.client.post(
            TEAM_FOLDERS,
            {
                "team_id": str(self.team_b.team_id),
                "user_id": str(self.b_owner.id),
                "name": "Read Only",
                "parent_folder_id": str(self.folder.folder_id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_a_guest_still_cannot_create_a_root_folder_in_the_host_team(self):
        self.authenticate(self.b_owner)
        res = self.client.post(
            TEAM_FOLDERS,
            {
                "team_id": str(self.team_a.team_id),
                "user_id": str(self.b_owner.id),
                "name": "Top Level",
                "visibility": PersonalNoteFolder.VISIBILITY_PRIVATE,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_a_note_written_in_the_share_is_filed_under_the_host_team(self):
        self.authenticate(self.b_owner)
        res = self.client.post(
            "/api/v2/note/personal/",
            {
                "team_id": str(self.team_b.team_id),
                "user_id": str(self.b_owner.id),
                "title": "Guest Note",
                "body": [],
                "folder_id": str(self.folder.folder_id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        note = PersonalNoteMaster.objects.get(note_id=res.data["noteId"])
        self.assertEqual(str(note.team_id), str(self.team_a.team_id))


class GuestSubfolderLifecycleTests(CrossTeamTestCase):
    """Make it, fix its name, throw it away — from the guest's own list.

    The guest works in their own Team Notes, so every request names the
    team they are VIEWING while the folder belongs to the host. Scoping
    these handlers by the team in the request answered "not found" for all
    three verbs, which turned the subfolder they were just allowed to
    create into something they could neither rename nor delete.
    """

    def setUp(self):
        super().setUp()
        self.grant = self.active_folder_grant()
        self.subfolder = self._create_subfolder("Our Workstream")

    def _params(self, **extra):
        params = {"team_id": str(self.team_b.team_id), "user_id": str(self.b_owner.id)}
        params.update(extra)
        return params

    def _create_subfolder(self, name):
        self.authenticate(self.b_owner)
        res = self.client.post(
            TEAM_FOLDERS,
            self._params(name=name, parent_folder_id=str(self.folder.folder_id)),
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        return PersonalNoteFolder.objects.get(folder_id=res.data["folderId"])

    def test_the_guest_can_rename_the_subfolder_they_made(self):
        self.authenticate(self.b_owner)
        res = self.client.put(
            TEAM_FOLDERS,
            self._params(folder_id=str(self.subfolder.folder_id), name="Renamed By Us"),
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.subfolder.refresh_from_db()
        self.assertEqual(self.subfolder.name, "Renamed By Us")

    def test_the_guest_cannot_rename_the_shared_folder_itself(self):
        """It is the host's, and it is the thing the grant names."""
        self.authenticate(self.b_owner)
        res = self.client.put(
            TEAM_FOLDERS,
            self._params(folder_id=str(self.folder.folder_id), name="Ours Now"),
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.folder.refresh_from_db()
        self.assertNotEqual(self.folder.name, "Ours Now")

    def test_the_guest_cannot_move_their_subfolder_out_of_the_share(self):
        self.authenticate(self.b_owner)
        res = self.client.put(
            TEAM_FOLDERS,
            self._params(folder_id=str(self.subfolder.folder_id), parent_folder_id=None),
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.subfolder.refresh_from_db()
        self.assertEqual(str(self.subfolder.parent_folder_id), str(self.folder.folder_id))

    def test_the_guest_cannot_re_scope_their_subfolder(self):
        """Public here would mean the whole host company."""
        self.authenticate(self.b_owner)
        res = self.client.put(
            TEAM_FOLDERS,
            self._params(
                folder_id=str(self.subfolder.folder_id),
                visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
            ),
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.subfolder.refresh_from_db()
        self.assertIsNone(self.subfolder.visibility)

    def test_the_guest_can_delete_their_own_subfolder(self):
        self.authenticate(self.b_owner)
        res = self.client.delete(
            f"{TEAM_FOLDERS}?team_id={self.team_b.team_id}"
            f"&user_id={self.b_owner.id}&folder_id={self.subfolder.folder_id}"
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(
            PersonalNoteFolder.objects.filter(folder_id=self.subfolder.folder_id).exists()
        )

    def test_the_guest_cannot_delete_the_shared_folder(self):
        self.authenticate(self.b_owner)
        res = self.client.delete(
            f"{TEAM_FOLDERS}?team_id={self.team_b.team_id}"
            f"&user_id={self.b_owner.id}&folder_id={self.folder.folder_id}"
        )
        self.assertEqual(res.status_code, 403)
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=self.folder.folder_id).exists())

    def test_deleting_still_refuses_when_it_holds_the_hosts_content(self):
        PersonalNoteMaster.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            title="Host wrote this here",
            folder_id=self.subfolder.folder_id,
        )
        self.authenticate(self.b_owner)
        res = self.client.delete(
            f"{TEAM_FOLDERS}?team_id={self.team_b.team_id}"
            f"&user_id={self.b_owner.id}&folder_id={self.subfolder.folder_id}"
        )
        self.assertEqual(res.status_code, 409)

    def test_the_guest_can_read_the_roster_of_the_shared_folder(self):
        """Who else is in here is the first thing you ask about a share."""
        self.authenticate(self.b_owner)
        res = self.client.get(
            TEAM_FOLDER_MEMBERS,
            {"team_id": str(self.team_b.team_id), "folder_id": str(self.folder.folder_id)},
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn(str(self.b_owner.id), [str(r["userId"]) for r in res.data])

    def test_the_guest_cannot_add_anyone_to_the_hosts_folder(self):
        """Their own colleagues come in through the grant, not this roster."""
        self.authenticate(self.b_owner)
        res = self.client.post(
            TEAM_FOLDER_MEMBERS,
            {
                "team_id": str(self.team_b.team_id),
                "folder_id": str(self.folder.folder_id),
                "user_ids": [str(self.b_editor.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_a_stranger_team_cannot_reach_the_folder_at_all(self):
        """Resolving by id must not become a way to name someone's folder."""
        self.authenticate(self.c_owner)
        res = self.client.put(
            TEAM_FOLDERS,
            {
                "team_id": str(self.team_c.team_id),
                "user_id": str(self.c_owner.id),
                "folder_id": str(self.folder.folder_id),
                "name": "Hijacked",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)


def _project_member_ids(project) -> set:
    from origin.models.project.prj_models import ProjectMembers

    return {
        str(uid)
        for uid in ProjectMembers.objects.filter(project=project).values_list(
            "attendee_id", flat=True
        )
    }
