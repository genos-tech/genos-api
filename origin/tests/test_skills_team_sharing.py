"""Team sharing of Genos Skills (MIP-2043): who may edit a team skill and who may only run it,
copy with attribution, moving a skill private <-> team, the team owner's hide switch, and what
happens to a team's skills when their owner leaves.

The roles, as the design (note 274 on MIP-2039, section 6) names them:

* owner (`user2` here: a member who is NOT the team owner, so the team owner is a role of its own)
* co-editor (`editor`: invited by the owner, on the team)
* team member (`reader`: reads and runs the published version only)
* team owner (`self.user`)
* guest / outsider (no team row)
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.skills.skill_models import Skill, SkillVersion
from origin.services import team_membership
from origin.services.skills import service
from origin.tests.test_skills_api import LIST, SkillBase, detail
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, get_effective_role

User = get_user_model()


class SharingBase(SkillBase):
    def setUp(self):
        super().setUp()
        # self.user = team owner, self.user2 = the skill's owner.
        self.editor = User.objects.create_user(
            username="editor", email="editor@example.com", password="x-pass-123"
        )
        self.reader = User.objects.create_user(
            username="reader", email="reader@example.com", password="x-pass-123"
        )
        for member in (self.editor, self.reader):
            TeamMembers.objects.create(team=self.team, attendee=member)
        self.guest = User.objects.create_user(
            username="guest", email="guest@example.com", password="x-pass-123"
        )
        purge = mock.patch("origin.search_engine.purge.purge_note")
        self.purge = purge.start()
        self.addCleanup(purge.stop)

    def skill(self, **over):
        """A skill owned by user2 (default: shared with the team)."""
        over.setdefault("visibility", "team")
        over.setdefault("slug", "weekly")
        return self.make_ok(user=self.user2, **over)

    def call(self, user, method, path, data=None):
        self.as_user(user)
        return getattr(self.client, method)(path, data or {}, format="json")

    def invite(self, skill, target, owner=None):
        res = self.call(
            owner or self.user2, "post", detail(skill["id"], "editors/"), {"userId": str(target.pk)}
        )
        self.assertEqual(res.status_code, 201, res.content)
        return res

    def hide(self, skill, user=None):
        return self.call(user or self.user, "post", detail(skill["id"], "hide/"))

    def team_tab(self, user):
        self.as_user(user)
        res = self.client.get(f"{LIST}?team_id={self.team_id}&tab=team")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["skills"]


class OwnerTests(SharingBase):
    def test_the_owner_is_the_owner_of_the_body_note(self):
        skill = self.skill()
        self.assertEqual(get_effective_role(self.user2.pk, NOTE_TYPE_PERSONAL, skill["noteId"]), 1)

    def test_the_owner_edits_publishes_renames_and_deletes(self):
        skill = self.skill()
        self.as_user(self.user2)
        res = self.client.patch(
            detail(skill["id"]),
            {"slug": "weekly-2", "bodyMarkdown": "# T\n\n1. New step."},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        res = self.client.post(detail(skill["id"], "publish/"), {}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.client.delete(detail(skill["id"])).status_code, 204)

    def test_the_owner_invites_and_removes_an_editor(self):
        skill = self.skill()
        res = self.invite(skill, self.editor)
        self.assertEqual([e["username"] for e in res.json()["editors"]], ["editor"])
        # Again: idempotent.
        self.invite(skill, self.editor)
        self.assertEqual(
            NotePermissionMaster.objects.filter(note_id=skill["noteId"], user=self.editor).count(),
            1,
        )
        res = self.call(self.user2, "delete", detail(skill["id"], f"editors/{self.editor.pk}/"))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["editors"], [])
        self.assertFalse(
            NotePermissionMaster.objects.filter(note_id=skill["noteId"], user=self.editor).exists()
        )
        self.purge.assert_called()  # the index stamped the editor as a reader

    def test_only_a_member_of_the_team_can_be_invited_and_not_oneself(self):
        skill = self.skill()
        for target in (self.guest, self.outsider):
            res = self.call(
                self.user2, "post", detail(skill["id"], "editors/"), {"userId": str(target.pk)}
            )
            self.assertEqual(res.status_code, 404, res.content)
        res = self.call(
            self.user2, "post", detail(skill["id"], "editors/"), {"userId": str(self.user2.pk)}
        )
        self.assertEqual(res.status_code, 400)
        res = self.call(self.user2, "post", detail(skill["id"], "editors/"), {})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(NotePermissionMaster.objects.filter(note_id=skill["noteId"]).exists())

    def test_a_skill_has_a_limited_number_of_editors(self):
        skill = self.skill()
        with mock.patch.object(service, "MAX_EDITORS", 1):
            self.invite(skill, self.editor)
            res = self.call(
                self.user2, "post", detail(skill["id"], "editors/"), {"userId": str(self.reader.pk)}
            )
            self.assertEqual(res.status_code, 400)
            self.invite(skill, self.editor)  # the one already in is not a new one

    def test_the_owner_cannot_show_a_skill_the_team_owner_hid(self):
        skill = self.skill()
        self.assertEqual(self.hide(skill).status_code, 200)
        res = self.call(self.user2, "delete", detail(skill["id"], "hide/"))
        self.assertEqual(res.status_code, 403)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).status, "hidden_by_team")

    def test_the_owner_keeps_editing_a_hidden_skill_and_it_stays_hidden(self):
        skill = self.skill()
        self.hide(skill)
        self.as_user(self.user2)
        res = self.client.get(detail(skill["id"]))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["skill"]["hiddenByTeam"])
        self.assertFalse(res.json()["skill"]["canHide"])
        res = self.client.patch(
            detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Edited."}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        res = self.client.post(detail(skill["id"], "publish/"), {}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).status, "hidden_by_team")
        self.as_user(self.reader)
        self.assertEqual(self.client.get(detail(skill["id"])).status_code, 404)

    def test_moving_private_and_back_keeps_the_editors_and_the_hide(self):
        skill = self.skill()
        self.invite(skill, self.editor)
        self.hide(skill)
        self.as_user(self.user2)
        self.client.patch(detail(skill["id"]), {"visibility": "private"}, format="json")
        self.client.patch(detail(skill["id"]), {"visibility": "team"}, format="json")
        row = Skill.objects.get(pk=skill["id"])
        self.assertEqual(row.status, "hidden_by_team")  # not a way around the team owner
        self.assertTrue(
            NotePermissionMaster.objects.filter(note_id=skill["noteId"], user=self.editor).exists()
        )

    def test_going_private_stops_the_teams_reads_and_keeps_their_copies(self):
        skill = self.skill()
        self.as_user(self.reader)
        copy = self.client.post(
            detail(skill["id"], "copy/"), {"team_id": self.team_id}, format="json"
        ).json()["skill"]
        self.call(self.user2, "patch", detail(skill["id"]), {"visibility": "private"})
        for who in (self.reader, self.user):
            self.as_user(who)
            self.assertEqual(self.client.get(detail(skill["id"])).status_code, 404)
            self.assertEqual(self.client.get(detail(skill["id"], "versions/")).status_code, 404)
            self.assertEqual(self.client.get(detail(skill["id"], "export/")).status_code, 404)
        self.as_user(self.reader)
        self.assertEqual(self.client.get(detail(copy["id"])).status_code, 200)
        self.assertEqual(self.team_tab(self.reader), [])

    def test_an_owner_who_shares_publishes_the_text_for_the_team(self):
        skill = self.skill(visibility="private")
        self.call(self.user2, "patch", detail(skill["id"]), {"visibility": "team"})
        self.as_user(self.reader)
        res = self.client.get(detail(skill["id"]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["skill"]["publishedVersionNo"], 1)


class CoEditorTests(SharingBase):
    def setUp(self):
        super().setUp()
        self.s = self.skill()
        self.invite(self.s, self.editor)

    def test_a_co_editor_reads_the_draft_and_gets_the_editors_view(self):
        self.call(self.user2, "patch", detail(self.s["id"]), {"description": "Draft words."})
        res = self.call(self.editor, "get", detail(self.s["id"]))
        self.assertEqual(res.status_code, 200)
        body = res.json()["skill"]
        self.assertEqual(body["description"], "Draft words.")  # the draft, not version 1
        self.assertEqual(body["role"], "editor")
        self.assertTrue(body["canEdit"])
        self.assertFalse(body["canManage"])
        self.assertFalse(body["mine"])
        self.assertEqual(body["noteId"], self.s["noteId"])
        self.assertEqual(body["ownerName"], "otheruser")
        self.assertEqual([e["username"] for e in body["editors"]], ["editor"])
        self.assertTrue(body["hasUnpublishedChanges"])

    def test_a_co_editor_edits_the_text_and_publishes_a_version(self):
        self.as_user(self.editor)
        res = self.client.patch(
            detail(self.s["id"]),
            {"description": "By the editor.", "bodyMarkdown": "# T\n\n1. Editor step."},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        # Readers still have version 1.
        self.as_user(self.reader)
        self.assertIn(
            "Read the open tasks",
            self.client.get(detail(self.s["id"])).json()["skill"]["bodyMarkdown"],
        )
        self.as_user(self.editor)
        res = self.client.post(
            detail(self.s["id"], "publish/"), {"changelog": "tidy"}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        version = SkillVersion.objects.get(skill_id=self.s["id"], version_no=2)
        self.assertEqual(version.created_by_id, self.editor.pk)
        self.as_user(self.reader)
        got = self.client.get(detail(self.s["id"])).json()["skill"]
        self.assertEqual(got["description"], "By the editor.")
        self.assertIn("Editor step", got["bodyMarkdown"])

    def test_a_co_editor_cannot_rename_share_delete_invite_or_hide(self):
        self.as_user(self.editor)
        self.assertEqual(
            self.client.patch(detail(self.s["id"]), {"slug": "other"}, format="json").status_code,
            403,
        )
        self.assertEqual(
            self.client.patch(
                detail(self.s["id"]), {"visibility": "private"}, format="json"
            ).status_code,
            403,
        )
        self.assertEqual(self.client.delete(detail(self.s["id"])).status_code, 403)
        res = self.client.post(
            detail(self.s["id"], "editors/"), {"userId": str(self.reader.pk)}, format="json"
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(
            self.client.delete(detail(self.s["id"], f"editors/{self.user2.pk}/")).status_code, 403
        )
        self.assertEqual(self.client.post(detail(self.s["id"], "hide/")).status_code, 403)
        row = Skill.objects.get(pk=self.s["id"])
        self.assertEqual((row.slug, row.visibility, row.status), ("weekly", "team", "active"))
        # Saving the same name and sharing is not a change and is allowed.
        res = self.client.patch(
            detail(self.s["id"]),
            {"slug": "weekly", "visibility": "team", "title": "Same"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_co_editor_reads_a_private_skill_but_cannot_copy_it(self):
        private = self.skill(slug="secret", visibility="private")
        self.invite(private, self.editor)
        res = self.call(self.editor, "get", detail(private["id"]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            sorted(s["slug"] for s in self.team_tab(self.editor)), ["secret", "weekly"]
        )
        res = self.call(
            self.editor, "post", detail(private["id"], "copy/"), {"team_id": self.team_id}
        )
        self.assertEqual(res.status_code, 403)
        self.assertFalse(Skill.objects.filter(owner=self.editor).exists())
        # A teammate who was not invited still gets the 404.
        self.assertEqual(self.call(self.reader, "get", detail(private["id"])).status_code, 404)

    def test_the_body_note_answers_to_the_skill(self):
        note = self.s["noteId"]
        role = lambda user: get_effective_role(user.pk, NOTE_TYPE_PERSONAL, note)  # noqa: E731
        self.assertEqual(role(self.user2), 1)
        self.assertEqual(role(self.editor), 2)
        for user in (self.reader, self.user, self.guest, self.outsider):
            self.assertIsNone(role(user))
        # A stray Viewer row (a share, an approved access request) opens nothing.
        NotePermissionMaster.objects.create(
            team=self.team, user=self.reader, note_type=NOTE_TYPE_PERSONAL, note_id=note, role_id=3
        )
        self.assertIsNone(role(self.reader))
        # An Editor row of someone who is not on the team opens nothing either.
        NotePermissionMaster.objects.create(
            team=self.team, user=self.guest, note_type=NOTE_TYPE_PERSONAL, note_id=note, role_id=2
        )
        self.assertIsNone(role(self.guest))

    def test_the_note_apis_cannot_delete_a_skills_body(self):
        note = self.s["noteId"]
        for user in (self.editor, self.user2):
            res = self.call(
                user, "delete", f"/api/v2/note/personal/?team_id={self.team_id}&note_id={note}"
            )
            self.assertIn(res.status_code, (409, 403, 404), res.content)
            self.assertTrue(PersonalNoteMaster.objects.filter(note_id=note).exists())
        self.assertTrue(Skill.objects.filter(pk=self.s["id"]).exists())

    def test_the_note_is_not_in_the_co_editors_shared_with_me(self):
        self.as_user(self.editor)
        res = self.client.get(
            f"/api/v2/note/personal/shared/meta/?team_id={self.team_id}&user_id={self.editor.pk}"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), [])

    def test_a_co_editor_can_leave_the_skill(self):
        res = self.call(self.editor, "delete", detail(self.s["id"], f"editors/{self.editor.pk}/"))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(
            self.call(self.editor, "patch", detail(self.s["id"]), {"title": "x"}).status_code, 403
        )

    def test_a_co_editor_who_leaves_the_team_loses_the_role_and_the_row(self):
        team_membership.remove_team_member(self.team.team_id, self.editor.pk)
        self.assertFalse(
            NotePermissionMaster.objects.filter(note_id=self.s["noteId"], user=self.editor).exists()
        )
        self.assertEqual(self.call(self.editor, "get", detail(self.s["id"])).status_code, 404)
        self.assertEqual(
            self.call(self.editor, "patch", detail(self.s["id"]), {"title": "x"}).status_code, 404
        )
        self.purge.assert_called()

    def test_a_row_that_survived_a_removal_grants_nothing(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.editor).update(is_deleted=True)
        self.assertEqual(self.call(self.editor, "get", detail(self.s["id"])).status_code, 404)
        self.assertEqual(
            self.call(self.user2, "get", detail(self.s["id"])).json()["skill"]["editors"], []
        )
        self.assertIsNone(get_effective_role(self.editor.pk, NOTE_TYPE_PERSONAL, self.s["noteId"]))

    def test_a_taken_down_skill_has_no_co_editors(self):
        Skill.objects.filter(pk=self.s["id"]).update(status="taken_down")
        self.assertEqual(self.call(self.editor, "get", detail(self.s["id"])).status_code, 404)

    def test_the_editors_list_is_for_the_owner_and_the_editors(self):
        self.assertEqual(
            self.call(self.editor, "get", detail(self.s["id"], "editors/")).status_code, 200
        )
        self.assertEqual(
            self.call(self.user2, "get", detail(self.s["id"], "editors/")).status_code, 200
        )
        self.assertEqual(
            self.call(self.reader, "get", detail(self.s["id"], "editors/")).status_code, 403
        )
        for user in (self.guest, self.outsider):
            self.assertEqual(
                self.call(user, "get", detail(self.s["id"], "editors/")).status_code, 404
            )

    def test_the_team_tab_names_the_role_of_each_row(self):
        mine_to_edit = {s["slug"]: s for s in self.team_tab(self.editor)}
        self.assertEqual(mine_to_edit["weekly"]["role"], "editor")
        self.assertTrue(mine_to_edit["weekly"]["canEdit"])
        readers = {s["slug"]: s for s in self.team_tab(self.reader)}
        self.assertEqual(readers["weekly"]["role"], "reader")
        self.assertFalse(readers["weekly"]["canEdit"])
        self.assertNotIn("noteId", readers["weekly"])


class TeamMemberTests(SharingBase):
    def test_a_team_member_reads_the_published_version_and_cannot_see_the_editors(self):
        skill = self.skill()
        self.invite(skill, self.editor)
        res = self.call(self.reader, "get", detail(skill["id"]))
        body = res.json()["skill"]
        self.assertEqual(body["role"], "reader")
        self.assertFalse(body["canEdit"])
        self.assertNotIn("editors", body)
        self.assertNotIn("noteId", body)
        self.assertEqual(
            self.call(self.reader, "get", detail(skill["id"], "editors/")).status_code, 403
        )
        for call in (
            lambda: self.call(self.reader, "patch", detail(skill["id"]), {"title": "x"}),
            lambda: self.call(self.reader, "post", detail(skill["id"], "publish/")),
            lambda: self.call(self.reader, "delete", detail(skill["id"])),
            lambda: self.call(self.reader, "post", detail(skill["id"], "hide/")),
            lambda: self.call(
                self.reader, "post", detail(skill["id"], "editors/"), {"userId": str(self.guest.pk)}
            ),
        ):
            self.assertEqual(call().status_code, 403)

    def test_a_copy_is_private_theirs_and_names_where_it_came_from(self):
        skill = self.skill()
        res = self.call(
            self.reader, "post", detail(skill["id"], "copy/"), {"team_id": self.team_id}
        )
        self.assertEqual(res.status_code, 201, res.content)
        copy = res.json()["skill"]
        self.assertEqual(copy["visibility"], "private")
        self.assertEqual(copy["role"], "owner")
        self.assertEqual(copy["forkedFromId"], skill["id"])
        got = self.call(self.reader, "get", detail(copy["id"])).json()["skill"]
        self.assertEqual(
            got["forkedFrom"],
            {
                "versionNo": 1,
                "available": True,
                "id": skill["id"],
                "title": "Weekly update",
                "ownerName": "otheruser",
            },
        )
        # No editors, no co-editor of the original, the original is untouched.
        self.assertEqual(got["editors"], [])
        self.assertEqual(Skill.objects.get(pk=skill["id"]).copy_count, 1)
        self.assertEqual(
            self.call(self.user2, "get", detail(skill["id"])).json()["skill"]["visibility"], "team"
        )

    def test_a_copy_does_not_carry_the_co_editors(self):
        skill = self.skill()
        self.invite(skill, self.editor)
        copy = self.call(
            self.reader, "post", detail(skill["id"], "copy/"), {"team_id": self.team_id}
        ).json()["skill"]
        self.assertFalse(NotePermissionMaster.objects.filter(note_id=copy["noteId"]).exists())
        self.assertEqual(self.call(self.editor, "get", detail(copy["id"])).status_code, 404)

    def test_a_copy_is_made_from_the_published_version_not_the_draft(self):
        skill = self.skill()
        self.call(
            self.user2, "patch", detail(skill["id"]), {"bodyMarkdown": "# T\n\n1. Draft only."}
        )
        copy = self.call(
            self.reader, "post", detail(skill["id"], "copy/"), {"team_id": self.team_id}
        ).json()["skill"]
        text = self.call(self.reader, "get", detail(copy["id"])).json()["skill"]["bodyMarkdown"]
        self.assertIn("Read the open tasks", text)
        self.assertNotIn("Draft only", text)

    def test_a_team_skill_is_copied_only_into_its_own_team(self):
        skill = self.skill()
        TeamMembers.objects.create(team=self.other_team, attendee=self.reader)
        res = self.call(
            self.reader,
            "post",
            detail(skill["id"], "copy/"),
            {"team_id": str(self.other_team.team_id)},
        )
        self.assertEqual(res.status_code, 403, res.content)
        self.assertFalse(Skill.objects.filter(owner=self.reader).exists())

    def test_a_copy_keeps_working_when_the_original_goes_private_and_names_nothing(self):
        skill = self.skill()
        copy = self.call(
            self.reader, "post", detail(skill["id"], "copy/"), {"team_id": self.team_id}
        ).json()["skill"]
        self.call(self.user2, "patch", detail(skill["id"]), {"visibility": "private"})
        got = self.call(self.reader, "get", detail(copy["id"])).json()["skill"]
        self.assertEqual(got["forkedFrom"], {"versionNo": 1, "available": False})
        self.assertNotIn("Weekly", str(got["forkedFrom"]))

    def test_the_original_being_deleted_leaves_a_copy_with_no_attribution(self):
        skill = self.skill()
        copy = self.call(
            self.reader, "post", detail(skill["id"], "copy/"), {"team_id": self.team_id}
        ).json()["skill"]
        self.call(self.user2, "delete", detail(skill["id"]))
        got = self.call(self.reader, "get", detail(copy["id"])).json()["skill"]
        self.assertNotIn("forkedFrom", got)

    def test_a_hidden_skill_is_a_404_everywhere_for_the_team(self):
        skill = self.skill()
        self.hide(skill)
        for suffix in ("", "versions/", "versions/1/", "export/"):
            self.assertEqual(
                self.call(self.reader, "get", detail(skill["id"], suffix)).status_code, 404, suffix
            )
        self.assertEqual(
            self.call(
                self.reader, "post", detail(skill["id"], "copy/"), {"team_id": self.team_id}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.call(self.reader, "post", detail(skill["id"], "star/")).status_code, 404
        )
        self.assertEqual(self.team_tab(self.reader), [])
        self.assertEqual(
            [
                s["slug"]
                for s in self.call(
                    self.reader, "get", f"{LIST}?team_id={self.team_id}&tab=starred"
                ).json()["skills"]
            ],
            [],
        )

    def test_a_guest_and_an_outsider_get_404_on_every_new_route(self):
        skill = self.skill()
        for user in (self.guest, self.outsider):
            for method, suffix in (
                ("get", "editors/"),
                ("post", "editors/"),
                ("post", "hide/"),
                ("delete", "hide/"),
                ("delete", f"editors/{self.editor.pk}/"),
            ):
                res = self.call(user, method, detail(skill["id"], suffix), {"userId": str(user.pk)})
                self.assertEqual(res.status_code, 404, (user.username, method, suffix))


class TeamOwnerTests(SharingBase):
    def test_the_team_owner_hides_and_shows_a_team_skill(self):
        skill = self.skill()
        res = self.hide(skill)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["skill"]["status"], "hidden_by_team")
        self.assertEqual(self.hide(skill).status_code, 200)  # idempotent
        # They still see it, marked, with the switch to show it.
        row = {s["slug"]: s for s in self.team_tab(self.user)}["weekly"]
        self.assertTrue(row["hiddenByTeam"])
        self.assertTrue(row["canHide"])
        self.assertEqual(row["role"], "reader")
        self.assertFalse(row["canEdit"])
        res = self.call(self.user, "delete", detail(skill["id"], "hide/"))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["skill"]["status"], "active")
        self.assertEqual(self.call(self.reader, "get", detail(skill["id"])).status_code, 200)

    def test_the_team_owner_reads_a_hidden_skill_but_cannot_edit_or_copy_it(self):
        skill = self.skill()
        self.hide(skill)
        self.assertEqual(self.call(self.user, "get", detail(skill["id"])).status_code, 200)
        self.assertEqual(
            self.call(self.user, "patch", detail(skill["id"]), {"title": "x"}).status_code, 403
        )
        self.assertEqual(self.call(self.user, "delete", detail(skill["id"])).status_code, 403)
        res = self.call(self.user, "post", detail(skill["id"], "copy/"), {"team_id": self.team_id})
        self.assertEqual(res.status_code, 403)

    def test_a_private_skill_cannot_be_hidden_and_is_not_even_seen(self):
        skill = self.skill(visibility="private", slug="mine-only")
        self.assertEqual(self.hide(skill).status_code, 404)
        self.assertEqual(self.call(self.user, "get", detail(skill["id"])).status_code, 404)

    def test_the_team_owner_cannot_hide_their_own_skill_or_an_operators_state(self):
        own = self.make_ok(user=self.user, slug="own", visibility="team")
        self.assertEqual(self.hide(own).status_code, 403)
        skill = self.skill()
        Skill.objects.filter(pk=skill["id"]).update(status="reported_hidden")
        self.assertEqual(
            self.hide(skill).status_code, 404
        )  # not readable to them: an operator holds it
        Skill.objects.filter(pk=skill["id"]).update(status="taken_down")
        self.assertEqual(self.hide(skill).status_code, 404)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).status, "taken_down")

    def test_only_the_team_owner_hides(self):
        skill = self.skill()
        for user in (self.reader, self.editor):
            self.assertEqual(self.hide(skill, user).status_code, 403)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).status, "active")

    def test_the_team_owner_is_not_an_editor_unless_named_one(self):
        skill = self.skill()
        self.assertIsNone(get_effective_role(self.user.pk, NOTE_TYPE_PERSONAL, skill["noteId"]))
        self.assertEqual(
            self.call(self.user, "get", detail(skill["id"])).json()["skill"]["role"], "reader"
        )
        self.invite(skill, self.user)
        self.assertEqual(
            self.call(self.user, "get", detail(skill["id"])).json()["skill"]["role"], "editor"
        )

    def test_the_hide_is_one_status_for_the_runner_to_check(self):
        # The `/name` run (MIP-2042) reads through `can_read`: a hidden skill is refused to
        # everyone but the owner and the co-editors (the team owner reads it, to show it again).
        skill = self.skill()
        self.invite(skill, self.editor)
        self.hide(skill)
        row = Skill.objects.get(pk=skill["id"])
        self.assertFalse(service.can_read(row, self.reader.pk))
        self.assertTrue(service.can_read(row, self.editor.pk))
        self.assertTrue(service.can_read(row, self.user2.pk))


class LeavingTests(SharingBase):
    def test_leaving_the_team_hands_team_skills_to_the_team_owner(self):
        shared = self.skill(slug="shared")
        private = self.skill(slug="private", visibility="private")
        self.invite(shared, self.editor)
        with self.captureOnCommitCallbacks(execute=True):
            team_membership.remove_team_member(self.team.team_id, self.user2.pk)
        row = Skill.objects.get(pk=shared["id"])
        self.assertEqual(row.owner_id, self.user.pk)
        note = PersonalNoteMaster.objects.get(note_id=shared["noteId"])
        self.assertEqual(note.owner_id, self.user.pk)
        folder = PersonalNoteFolder.objects.get(folder_id=note.folder_id)
        self.assertEqual((folder.owner_id, folder.scope), (self.user.pk, "skill"))
        # The versions, the counters and the co-editor came with it.
        self.assertEqual(SkillVersion.objects.filter(skill=row).count(), 1)
        self.assertEqual(get_effective_role(self.user.pk, NOTE_TYPE_PERSONAL, shared["noteId"]), 1)
        self.assertEqual(
            get_effective_role(self.editor.pk, NOTE_TYPE_PERSONAL, shared["noteId"]), 2
        )
        self.assertIsNone(get_effective_role(self.user2.pk, NOTE_TYPE_PERSONAL, shared["noteId"]))
        got = self.call(self.user, "get", detail(shared["id"])).json()["skill"]
        self.assertEqual((got["role"], got["canManage"]), ("owner", True))
        self.assertEqual(self.call(self.reader, "get", detail(shared["id"])).status_code, 200)
        # The private one stays with the person who left, who can only delete it.
        self.assertEqual(Skill.objects.get(pk=private["id"]).owner_id, self.user2.pk)
        self.assertEqual(self.call(self.user2, "delete", detail(private["id"])).status_code, 204)

    def test_the_new_owner_gets_a_free_name_when_theirs_is_taken(self):
        mine = self.make_ok(user=self.user, slug="weekly", visibility="team")
        theirs = self.skill(slug="weekly")
        team_membership.remove_team_member(self.team.team_id, self.user2.pk)
        self.assertEqual(Skill.objects.get(pk=theirs["id"]).slug, "weekly-2")
        self.assertEqual(Skill.objects.get(pk=mine["id"]).slug, "weekly")

    def test_a_team_owner_who_was_a_co_editor_is_the_owner_after_the_hand_over(self):
        skill = self.skill()
        self.invite(skill, self.user)
        team_membership.remove_team_member(self.team.team_id, self.user2.pk)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).owner_id, self.user.pk)
        self.assertFalse(
            NotePermissionMaster.objects.filter(note_id=skill["noteId"], user=self.user).exists()
        )

    def test_a_hand_over_that_fails_does_not_keep_the_person_on_the_team(self):
        self.skill()
        with mock.patch.object(service, "hand_over_team_skills", side_effect=RuntimeError("boom")):
            team_membership.remove_team_member(self.team.team_id, self.user2.pk)
        self.assertFalse(
            TeamMembers.objects.filter(
                team=self.team, attendee=self.user2, is_deleted=False
            ).exists()
        )

    def test_a_hidden_skill_is_shown_again_when_it_is_handed_to_the_team_owner(self):
        skill = self.skill()
        self.hide(skill)
        team_membership.remove_team_member(self.team.team_id, self.user2.pk)
        row = Skill.objects.get(pk=skill["id"])
        self.assertEqual((row.owner_id, row.status), (self.user.pk, "active"))
        self.assertEqual(self.call(self.reader, "get", detail(skill["id"])).status_code, 200)

    def test_a_closing_team_has_nobody_to_hand_over_to(self):
        self.skill()
        TeamMaster.objects.filter(pk=self.team.pk).update(is_deleted=True)
        self.assertEqual(service.hand_over_team_skills(self.team.team_id, self.user2.pk), 0)

    def test_a_skill_owned_by_the_team_owner_is_not_moved(self):
        skill = self.make_ok(user=self.user, slug="boss", visibility="team")
        self.assertEqual(service.hand_over_team_skills(self.team.team_id, self.user.pk), 0)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).owner_id, self.user.pk)

    def test_another_teams_skills_are_not_touched(self):
        other = self.make_ok(user=self.outsider, slug="theirs", visibility="team")
        self.skill()
        team_membership.remove_team_member(self.team.team_id, self.user2.pk)
        self.assertEqual(Skill.objects.get(pk=other["id"]).owner_id, self.outsider.pk)

    def test_deleting_the_account_hands_team_skills_over_and_deletes_the_rest(self):
        from origin.services.account_deletion import delete_account

        shared = self.skill(slug="shared")
        private = self.skill(slug="private", visibility="private")
        self.invite(shared, self.editor)
        with (
            mock.patch("origin.services.account_deletion.purge_notes_from_index"),
            mock.patch("origin.services.note_delete.purge_note"),
            mock.patch("origin.search_engine.purge.purge_note_folders"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            delete_account(self.user2)
        row = Skill.objects.get(pk=shared["id"])
        self.assertEqual(row.owner_id, self.user.pk)
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=shared["noteId"]).exists())
        self.assertEqual(self.call(self.reader, "get", detail(shared["id"])).status_code, 200)
        self.assertFalse(Skill.objects.filter(pk=private["id"]).exists())
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=private["noteId"]).exists())

    def test_deleting_a_co_editors_account_drops_their_rows(self):
        from origin.services.account_deletion import delete_account

        skill = self.skill()
        self.invite(skill, self.editor)
        with (
            mock.patch("origin.services.account_deletion.purge_notes_from_index"),
            mock.patch("origin.services.note_delete.purge_note"),
            mock.patch("origin.search_engine.purge.purge_note_folders"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            delete_account(self.editor)
        self.assertFalse(
            NotePermissionMaster.objects.filter(note_id=skill["noteId"], user=self.editor).exists()
        )

    def test_the_team_owner_deleting_their_account_leaves_no_heir_so_skills_go(self):
        skill = self.make_ok(user=self.user, slug="boss", visibility="team")
        with mock.patch("origin.services.note_delete.purge_note"):
            service.erase_for_user(self.user)
        self.assertFalse(Skill.objects.filter(pk=skill["id"]).exists())


class BodyNoteRulesTests(SharingBase):
    def test_a_skill_body_is_only_deleted_with_its_skill(self):
        from origin.services.note_delete import DeleteRefused, delete_note

        skill = self.skill()
        with self.assertRaises(DeleteRefused):
            delete_note(team_id=self.team.team_id, family="personal", note_id=skill["noteId"])
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=skill["noteId"]).exists())
        self.assertEqual(self.call(self.user2, "delete", detail(skill["id"])).status_code, 204)
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=skill["noteId"]).exists())

    def test_an_ordinary_note_is_unchanged(self):
        from origin.services.note_delete import delete_note

        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n", body=[]
        )
        delete_note(team_id=self.team.team_id, family="personal", note_id=note.note_id)
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())
        self.assertEqual(service.skill_note_role(self.user.pk, note.note_id), (False, None))

    def test_the_search_acl_of_the_body_is_the_owner_and_the_editors(self):
        from origin.search_engine.agent.acl import personal_note_acl_user_ids

        skill = self.skill()
        self.invite(skill, self.editor)
        note = PersonalNoteMaster.objects.get(note_id=skill["noteId"])
        acl = personal_note_acl_user_ids(owner_id=self.user2.pk, note_id=note.note_id)
        self.assertEqual(acl, {str(self.user2.pk), str(self.editor.pk)})  # never the team


class ListQueryCountTests(SharingBase):
    def test_the_roles_of_the_team_tab_cost_the_same_for_two_skills_and_six(self):
        """A co-editor reads drafts, so no row needs its published version: what is left is the
        role questions, and they must not grow with the number of rows."""

        def queries() -> int:
            self.as_user(self.editor)
            with CaptureQueriesContext(connection) as ctx:
                res = self.client.get(f"{LIST}?team_id={self.team_id}&tab=team")
            self.assertEqual(res.status_code, 200)
            return len(ctx)

        for n in range(2):
            self.invite(self.skill(slug=f"s-{n}"), self.editor)
        few = queries()
        for n in range(2, 6):
            self.invite(self.skill(slug=f"s-{n}"), self.editor)
        self.assertEqual(len(self.team_tab(self.editor)), 6)
        self.assertEqual(queries(), few)
