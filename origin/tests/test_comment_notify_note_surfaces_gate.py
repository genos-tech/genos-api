"""MIP-1498: the surface-activity routes hold personal notes (6) and task notes (7) to the
note's own access rule, as MIP-1484 did for chat notes (8).

`POST /api/v3/activities/surface/` (mentions) and `/surface-comment/` (comment fan-out) with
`surface_type` 6 or 7 used to need only team membership. So a team member who cannot open the
note got back its owner's / members' user ids (a roster), a note id that does not exist got an
empty list (an existence oracle), and the call wrote "someone commented on your note" rows for
a note the caller has no access to (a spam / phishing vector into another member's inbox).

Now the caller must be able to read the note (`get_effective_role`, the one note rule), and a
note the caller cannot read answers exactly as a note that does not exist (404, the same body)
without writing anything. Recipients are unchanged.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Activity
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.tests.test_chat_note_one_rule import ChatNoteFixtures
from origin.views.utils.note_role import ROLE_OWNER, ROLE_VIEWER

User = get_user_model()
SURFACE_PERSONAL_NOTE = 6
SURFACE_TASK_NOTE = 7
COMMENT_URL = "/api/v3/activities/surface-comment/"
MENTION_URL = "/api/v3/activities/surface/"

# (surface_type, note_type)
PERSONAL = (SURFACE_PERSONAL_NOTE, 1)
TASK = (SURFACE_TASK_NOTE, 2)


class NoteSurfaceGate(ChatNoteFixtures):
    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.outsider)
        self.project = self.project_of("NSG", self.reader)

    # -- builders ------------------------------------------------------------------------ #

    def personal_note(self, owner=None, team=None):
        note = PersonalNoteMaster.objects.create(
            team=team or self.team, owner=owner or self.owner, title="Diary"
        )
        NotePermissionMaster.objects.create(
            team=note.team,
            user=note.owner,
            note_type=1,
            note_id=note.note_id,
            role_id=ROLE_OWNER,
        )
        return note

    def task_note(self):
        return TaskNoteMaster.objects.create(
            team=self.team, project=self.project, owner=self.owner, title="Spec"
        )

    def note_of(self, kind):
        return self.personal_note() if kind == PERSONAL else self.task_note()

    # -- the two routes ------------------------------------------------------------------ #

    def comment(self, user, kind, note_id, team=None):
        self.authenticate(user)
        return self.client.post(
            COMMENT_URL,
            {
                "surface_type": kind[0],
                "team_id": str((team or self.team).team_id),
                "entity_key": f"comment-participant:{note_id}:x",
                "meta": {"noteId": note_id, "noteType": kind[1]},
                "mentioned_user_ids": [],
            },
            format="json",
        )

    def mention(self, user, kind, note_id, targets, team=None):
        self.authenticate(user)
        return self.client.post(
            MENTION_URL,
            {
                "surface_type": kind[0],
                "team_id": str((team or self.team).team_id),
                "entity_key": f"note:{kind[1]}:{note_id}",
                "newly_mentioned_user_ids": [str(u) for u in targets],
                "meta": {"noteId": note_id, "noteType": kind[1]},
            },
            format="json",
        )

    @staticmethod
    def recipients(res):
        return {a["recipientUserId"] for a in res.json()["activities"]}

    def written(self, kind):
        return Activity.objects.filter(surface_type=kind[0]).count()

    # -- the caller ---------------------------------------------------------------------- #

    def test_a_team_member_who_cannot_read_the_note_is_refused_like_a_missing_note(self):
        for kind in (PERSONAL, TASK):
            with self.subTest(surface=kind[0]):
                note = self.note_of(kind)
                refused = self.comment(self.outsider, kind, note.note_id)
                missing = self.comment(self.outsider, kind, 999_999)
                self.assertEqual(refused.status_code, 404, refused.content)
                self.assertEqual(missing.status_code, 404, missing.content)
                self.assertEqual(refused.json(), missing.json())
                self.assertNotIn(str(self.owner.id), refused.content.decode())
                self.assertEqual(self.written(kind), 0)

    def test_the_mention_route_refuses_a_non_reader_too_and_writes_nothing(self):
        for kind in (PERSONAL, TASK):
            with self.subTest(surface=kind[0]):
                note = self.note_of(kind)
                refused = self.mention(self.outsider, kind, note.note_id, [self.owner.id])
                missing = self.mention(self.outsider, kind, 999_999, [self.owner.id])
                self.assertEqual((refused.status_code, missing.status_code), (404, 404))
                self.assertEqual(refused.json(), missing.json())
                self.assertEqual(self.written(kind), 0)

    def test_a_team_member_on_a_note_of_another_team_is_refused(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="other@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=other, attendee=self.outsider)
        TeamMembers.objects.create(team=other, attendee=self.owner)
        note = self.personal_note()  # filed in self.team; the caller names `other`
        res = self.comment(self.owner, PERSONAL, note.note_id, team=other)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertEqual(self.written(PERSONAL), 0)

    def test_a_malformed_note_id_is_refused_not_a_500(self):
        for kind in (PERSONAL, TASK):
            for bad in (None, "abc", [1], {"a": 1}):
                with self.subTest(surface=kind[0], note_id=bad):
                    self.authenticate(self.outsider)
                    res = self.client.post(
                        COMMENT_URL,
                        {
                            "surface_type": kind[0],
                            "team_id": str(self.team.team_id),
                            "entity_key": "k",
                            "meta": {"noteId": bad},
                        },
                        format="json",
                    )
                    self.assertEqual(res.status_code, 404, res.content)

    def test_the_owner_of_a_personal_note_is_let_in_and_a_grantee_is_notified(self):
        note = self.personal_note()
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.reader,
            note_type=1,
            note_id=note.note_id,
            role_id=ROLE_VIEWER,
        )
        res = self.comment(self.owner, PERSONAL, note.note_id)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.recipients(res), {str(self.reader.id)})
        # the grantee, in turn, may comment: the owner is told
        res = self.comment(self.reader, PERSONAL, note.note_id)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.recipients(res), {str(self.owner.id)})
        res = self.mention(self.reader, PERSONAL, note.note_id, [self.owner.id])
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.recipients(res), {str(self.owner.id)})

    def test_a_project_member_may_comment_on_a_task_note_and_the_roster_is_unchanged(self):
        note = self.task_note()
        res = self.comment(self.reader, TASK, note.note_id)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.recipients(res), {str(self.owner.id)})
        res = self.mention(self.reader, TASK, note.note_id, [self.owner.id])
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.recipients(res), {str(self.owner.id)})

    def test_an_explicit_grantee_outside_the_project_may_comment_on_a_task_note(self):
        note = self.task_note()
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.outsider,
            note_type=2,
            note_id=note.note_id,
            role_id=ROLE_VIEWER,
        )
        res = self.comment(self.outsider, TASK, note.note_id)
        self.assertEqual(res.status_code, 201, res.content)

    def test_someone_who_left_the_project_can_no_longer_comment_on_its_task_note(self):
        note = self.task_note()
        ProjectMembers.objects.filter(project=self.project, attendee=self.reader).delete()
        res = self.comment(self.reader, TASK, note.note_id)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertEqual(self.written(TASK), 0)

    def test_the_owner_of_a_note_made_through_the_real_create_endpoint_is_let_in(self):
        """Production state, not the fixture's: the REST create writes the owner's role row."""
        self.authenticate(self.owner)
        res = self.client.post(
            "/api/v2/note/personal/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.owner.id),
                "title": "Made by the endpoint",
                "body": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        note_id = res.data["noteId"]
        self.assertEqual(self.comment(self.owner, PERSONAL, note_id).status_code, 201)
        self.assertEqual(self.mention(self.owner, PERSONAL, note_id, []).status_code, 201)
        # and a stranger still is not
        self.assertEqual(self.comment(self.outsider, PERSONAL, note_id).status_code, 404)
