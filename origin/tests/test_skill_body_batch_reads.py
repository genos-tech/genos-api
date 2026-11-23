"""The batch read checks answer for a Genos Skill's body note the way the single-note rule does
(MIP-2190).

`note_role.get_effective_role` gives a skill body to `skills.service.skill_note_role`: the owner is
its Owner while on the team, a co-editor its Editor while on the team and the skill is not taken
down, nobody else has a role (a stray Viewer or Editor row opens nothing). `readable_note_ids`,
`personal_note_reader_pairs` and the two callers that add the note's owner on top of it used the
older rule (an explicit row, or the owner, is a reader), so they disagreed in three edge cases:
a taken-down skill's co-editors, an owner who left the team, a stray Editor row of a non-member.
Each test below compares the batch answer with `get_effective_role` over every kind of person.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_models import TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.skills.skill_models import STATUS_TAKEN_DOWN, Skill, SkillGrant
from origin.services.activity_surface_access import (
    SURFACE_PERSONAL_NOTE,
    _readable_notes,
)
from origin.tests.test_skills_team_sharing import SharingBase
from origin.views.utils.note_role import (
    NOTE_TYPE_PERSONAL,
    ROLE_VIEWER,
    get_effective_role,
    note_reader_pairs,
    personal_note_reader_pairs,
    readable_note_ids,
)

User = get_user_model()


class SkillBodyBatchReadTests(SharingBase):
    def setUp(self):
        super().setUp()
        self.stray = User.objects.create_user(
            username="stray", email="stray@example.com", password="x-pass-123"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.stray)

    def people(self):
        return [self.user, self.user2, self.editor, self.reader, self.guest, self.outsider]

    def assert_batch_equals_single(self, note_id):
        """Both batch checks say, for every person, what `get_effective_role` says."""
        pairs = personal_note_reader_pairs([u.pk for u in self.people()], [note_id])
        self.assertEqual(
            pairs, note_reader_pairs([u.pk for u in self.people()], NOTE_TYPE_PERSONAL, [note_id])
        )
        for person in self.people():
            single = get_effective_role(person.pk, NOTE_TYPE_PERSONAL, note_id) is not None
            self.assertEqual(
                note_id in readable_note_ids(person.pk, NOTE_TYPE_PERSONAL, {note_id}),
                single,
                f"readable_note_ids for {person.username}",
            )
            self.assertEqual(
                (str(person.pk), note_id) in pairs, single, f"reader pairs for {person.username}"
            )

    def test_a_shared_skill_body_reads_like_the_single_note_rule(self):
        skill = self.skill()
        self.invite(skill, self.editor)
        self.assert_batch_equals_single(skill["noteId"])
        readers = {p for p in self.people() if get_effective_role(p.pk, 1, skill["noteId"])}
        self.assertEqual(readers, {self.user2, self.editor})

    def test_a_person_a_skill_is_shared_with_reads_the_published_text_not_the_body_note(self):
        """Sharing with chosen people (MIP-2212) lets a grantee read and run the PUBLISHED version
        through the skill API; it gives no role on the body note, so the batch checks (and the
        single rule) must not open the live draft to them, while the owner and a co-editor still
        read it."""
        skill = self.skill()
        Skill.objects.filter(pk=skill["id"]).update(visibility="specific")
        SkillGrant.objects.create(skill_id=skill["id"], user=self.reader)
        self.invite(skill, self.editor)
        self.assert_batch_equals_single(skill["noteId"])
        self.assertIsNone(get_effective_role(self.reader.pk, NOTE_TYPE_PERSONAL, skill["noteId"]))
        self.assertNotIn(
            skill["noteId"],
            readable_note_ids(self.reader.pk, NOTE_TYPE_PERSONAL, {skill["noteId"]}),
        )
        self.assertEqual(
            {p for p in self.people() if get_effective_role(p.pk, 1, skill["noteId"])},
            {self.user2, self.editor},
        )

    def test_a_stray_viewer_row_opens_nothing(self):
        skill = self.skill()
        NotePermissionMaster.objects.create(
            user=self.reader,
            note_type=NOTE_TYPE_PERSONAL,
            note_id=skill["noteId"],
            role_id=ROLE_VIEWER,
        )
        self.assert_batch_equals_single(skill["noteId"])
        self.assertNotIn(
            skill["noteId"],
            readable_note_ids(self.reader.pk, NOTE_TYPE_PERSONAL, {skill["noteId"]}),
        )

    def test_a_taken_down_skills_co_editors_stop_reading(self):
        skill = self.skill()
        self.invite(skill, self.editor)
        Skill.objects.filter(pk=skill["id"]).update(status=STATUS_TAKEN_DOWN)
        self.assert_batch_equals_single(skill["noteId"])
        self.assertNotIn(
            skill["noteId"],
            readable_note_ids(self.editor.pk, NOTE_TYPE_PERSONAL, {skill["noteId"]}),
        )
        self.assertIn(
            skill["noteId"], readable_note_ids(self.user2.pk, NOTE_TYPE_PERSONAL, {skill["noteId"]})
        )

    def test_an_owner_who_left_the_team_with_a_private_skill_stops_reading(self):
        skill = self.skill(visibility="private", slug="mine")
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.assert_batch_equals_single(skill["noteId"])
        self.assertNotIn(
            skill["noteId"], readable_note_ids(self.user2.pk, NOTE_TYPE_PERSONAL, {skill["noteId"]})
        )

    def test_the_callers_that_add_the_owner_follow_too(self):
        skill = self.skill(visibility="private", slug="mine")
        note = skill["noteId"]
        self.assertEqual(_readable_notes(self.user2.pk, SURFACE_PERSONAL_NOTE, {note}), {note})
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.assertEqual(_readable_notes(self.user2.pk, SURFACE_PERSONAL_NOTE, {note}), set())

    def test_a_stray_editor_row_of_someone_off_the_team_opens_nothing(self):
        """The race of an invite and the person leaving the team: the row survives, the single
        note check ignores it, and so must the batch checks."""
        skill = self.skill()
        self.invite(skill, self.editor)
        TeamMembers.objects.filter(team=self.team, attendee=self.editor).update(is_deleted=True)
        self.assert_batch_equals_single(skill["noteId"])
        self.assertNotIn(
            skill["noteId"],
            readable_note_ids(self.editor.pk, NOTE_TYPE_PERSONAL, {skill["noteId"]}),
        )

    def test_an_ordinary_note_and_a_skill_body_in_one_batch(self):
        """The skill rule applies to the skill's note only: an ordinary note's explicit row and
        its owner still read it, in the same call."""
        from origin.models.note.personal_note_models import PersonalNoteMaster

        skill = self.skill()
        plain = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="plain", body=[]
        )
        NotePermissionMaster.objects.create(
            user=self.reader,
            note_type=NOTE_TYPE_PERSONAL,
            note_id=plain.note_id,
            role_id=ROLE_VIEWER,
        )
        ids = {skill["noteId"], plain.note_id}
        self.assertEqual(
            readable_note_ids(self.reader.pk, NOTE_TYPE_PERSONAL, ids), {plain.note_id}
        )
        self.assertEqual(
            personal_note_reader_pairs([self.reader.pk, self.guest.pk], ids),
            {(str(self.reader.pk), plain.note_id)},
        )

    def test_the_skill_check_costs_the_same_for_one_skill_and_many(self):
        def queries(fn) -> int:
            with CaptureQueriesContext(connection) as ctx:
                fn()
            return len(ctx)

        notes = [self.skill(slug=f"s-{n}")["noteId"] for n in range(2)]
        who = [self.editor.pk, self.reader.pk, self.user2.pk]
        few_ids = queries(lambda: readable_note_ids(self.editor.pk, NOTE_TYPE_PERSONAL, set(notes)))
        few_pairs = queries(lambda: personal_note_reader_pairs(who, notes))
        notes += [self.skill(slug=f"s-{n}")["noteId"] for n in range(2, 7)]
        self.assertEqual(
            queries(lambda: readable_note_ids(self.editor.pk, NOTE_TYPE_PERSONAL, set(notes))),
            few_ids,
        )
        self.assertEqual(queries(lambda: personal_note_reader_pairs(who, notes)), few_pairs)
