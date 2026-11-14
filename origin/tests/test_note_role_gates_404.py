"""MIP-1512 (b)/(c): what the REST note gates say to a caller with no role.

`require_write_role` (edit, delete, move, attach, restore a version) answered 403 for a note the
caller has no role on at all and 404 for an id that is not there, so a signed-in user could walk
the id space. A caller with NO role now gets the 404 a missing note gets (same body). A caller who
can READ the note but not write it still gets 403: they already know it exists, and the access
panel may ask for edit rights.

`require_read_role` keeps its 403 on purpose: the frontend's access-request panel (`NoteOpenFailure`
via `loadSpecificNote`, and `chatListItemForActivity`) reads a 403 on the single-note readers as "the
note exists and you have no role on it". That 403 is reachable only for a note in a team the caller
belongs to (that is the person the panel is for); a note in a team the caller is not in answers 404
like a missing one, whatever team id the request names.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.tests.test_note_role_probes_404 import MISSING, Probes
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    ROLE_EDITOR,
    ROLE_VIEWER,
    require_read_role,
    require_write_role,
)

User = get_user_model()
FAMILIES = (
    ("personal", NOTE_TYPE_PERSONAL),
    ("task", NOTE_TYPE_TASK),
    ("chat", NOTE_TYPE_CHAT),
)


class Gates(Probes):
    def setUp(self):
        super().setUp()
        self.notes = {
            NOTE_TYPE_PERSONAL: self.personal,
            NOTE_TYPE_TASK: self.task_note,
            NOTE_TYPE_CHAT: self.chat_note,
        }

    def grant(self, user, code, role_id):
        NotePermissionMaster.objects.update_or_create(
            user=user,
            note_type=code,
            note_id=self.notes[code].note_id,
            defaults={"team": self.team, "role_id": role_id},
        )

    def put(self, user, family, note_id):
        self.authenticate(user)
        return self.client.put(
            f"/api/v2/note/{family}/",
            {"note_id": note_id, "title": "changed", "body": []},
            format="json",
        )

    def single(self, user, family, note_id, team_id=None):
        self.authenticate(user)
        return self.client.get(
            f"/api/v2/note/{family}/single/",
            {"team_id": str(team_id or self.team.team_id), "note_id": note_id},
        )


class RequireWriteRole(Gates):
    def test_no_role_at_all_answers_like_a_missing_note(self):
        for family, code in FAMILIES:
            for who in ("teammate", "outsider"):
                user = self.user2 if who == "teammate" else self.outsider
                with self.subTest(family=family, who=who):
                    said = require_write_role(user.id, code, self.notes[code].note_id)
                    missing = require_write_role(user.id, code, MISSING)
                    self.assertEqual(said.status_code, 404)
                    self.assertEqual(
                        (said.status_code, said.data), (missing.status_code, missing.data)
                    )

    def test_a_reader_who_may_not_write_still_gets_403(self):
        for family, code in FAMILIES:
            self.grant(self.user2, code, ROLE_VIEWER)
            with self.subTest(family=family):
                res = require_write_role(self.user2.id, code, self.notes[code].note_id)
                self.assertEqual(res.status_code, 403)
                self.assertEqual(
                    res.data, {"error": "You do not have permission to modify this note."}
                )

    def test_a_writer_passes(self):
        for family, code in FAMILIES:
            self.grant(self.user2, code, ROLE_EDITOR)
            with self.subTest(family=family):
                self.assertIsNone(require_write_role(self.user2.id, code, self.notes[code].note_id))

    def test_the_edit_route_answers_the_same_404_for_no_role_and_for_missing(self):
        for family, code in FAMILIES:
            for user in (self.user2, self.outsider):
                with self.subTest(family=family, user=user.username):
                    said = self.put(user, family, self.notes[code].note_id)
                    missing = self.put(user, family, MISSING)
                    self.assertEqual(said.status_code, 404, said.content)
                    self.assertEqual(said.json(), missing.json())

    def test_the_edit_route_says_403_to_a_viewer_and_changes_nothing(self):
        self.grant(self.user2, NOTE_TYPE_PERSONAL, ROLE_VIEWER)
        res = self.put(self.user2, "personal", self.personal.note_id)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(PersonalNoteMaster.objects.get(pk=self.personal.pk).title, "mine")

    def test_the_delete_route_answers_404_to_no_role_and_keeps_the_note(self):
        self.authenticate(self.user2)
        res = self.client.delete(
            f"/api/v2/note/personal/?team_id={self.team.team_id}&note_id={self.personal.note_id}"
        )
        self.assertEqual(res.status_code, 404)
        self.assertTrue(PersonalNoteMaster.objects.filter(pk=self.personal.pk).exists())

    def test_the_owner_still_edits(self):
        res = self.put(self.user, "personal", self.personal.note_id)
        self.assertEqual(res.status_code, 200, res.content)


class RequireReadRole(Gates):
    """The 403 stays for a teammate; it never reaches a stranger to the note's team."""

    def test_a_teammate_with_no_role_still_gets_403_for_the_access_request_panel(self):
        for family, code in FAMILIES:
            with self.subTest(family=family):
                res = self.single(self.user2, family, self.notes[code].note_id)
                self.assertEqual(res.status_code, 403, res.content)
                self.assertEqual(res.json(), {"error": "You do not have access to this note."})

    def test_a_stranger_to_the_notes_team_gets_the_404_a_missing_note_gets(self):
        """Whatever team id the request names: the note's own team decides."""
        other = TeamMembers.objects.get(attendee=self.outsider).team_id
        for family, code in FAMILIES:
            for named in (self.team.team_id, other):
                with self.subTest(family=family, named_team=str(named)):
                    said = self.single(self.outsider, family, self.notes[code].note_id, named)
                    missing = self.single(self.outsider, family, MISSING, named)
                    self.assertEqual(said.status_code, 404, said.content)
                    self.assertEqual(said.json(), missing.json())

    def test_a_member_who_left_the_team_is_a_stranger_to_it(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        for family, code in FAMILIES:
            with self.subTest(family=family):
                self.assertEqual(
                    self.single(self.user2, family, self.notes[code].note_id).status_code, 404
                )

    def test_a_team_owner_with_no_membership_row_is_still_in_the_team(self):
        """The owner is identified by `TeamMaster.owner` and may have no `TeamMembers` row."""
        boss = User.objects.create_user(username="boss", email="boss@example.com", password="x")
        team = TeamMaster.objects.create(team_name="Boss", team_email="b@example.com", owner=boss)
        note = PersonalNoteMaster.objects.create(team=team, owner=self.user, title="t", body=[])
        self.assertFalse(TeamMembers.objects.filter(team=team, attendee=boss).exists())
        res = require_read_role(boss.id, NOTE_TYPE_PERSONAL, note.note_id, team.team_id)
        self.assertEqual(res.status_code, 403)

    def test_a_reader_still_gets_the_note(self):
        for family, code in FAMILIES:
            with self.subTest(family=family):
                res = self.single(self.user, family, self.notes[code].note_id)
                self.assertEqual(res.status_code, 200, res.content)
