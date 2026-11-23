"""`GET /api/v3/notes/<type>/<id>/html` (MIP-2214): the saved page, under the note's own ACL.

The page may be read by exactly the people who may read the note:

- for ALL THREE families (personal, task, chat) and for Team Notes folders (public, private, with a
  grant), the answer equals `get_effective_role(...) is not None` and equals what the existing single-note
  reader says (200 / 403 / 404), for the owner, an editor, a viewer, a teammate with no role, a project
  or chat member, and a stranger on another team;
- a stranger and a missing note get the SAME 404 body (the id space cannot be walked), and a teammate
  with no role gets the same 403 the note reader gives;
- it is JSON, never `text/html`, and never cached (the page is untrusted text, served as data);
- `HTML_NOTES` off answers 404 for everyone, as if the route did not exist; an ordinary note answers 404.
"""

from __future__ import annotations

from django.test import override_settings

from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.services import html_notes
from origin.tests.test_note_role_probes_404 import MISSING, Probes
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    ROLE_EDITOR,
    ROLE_VIEWER,
    get_effective_role,
)

PAGE = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Burndown</title></head>"
    "<body><h1>Burndown</h1><script>var a = 1;</script></body></html>"
)
FAMILIES = (
    ("personal", NOTE_TYPE_PERSONAL),
    ("task", NOTE_TYPE_TASK),
    ("chat", NOTE_TYPE_CHAT),
)


def url(family, note_id):
    return f"/api/v3/notes/{family}/{note_id}/html"


@override_settings(HTML_NOTES=True)
class HtmlNoteApi(Probes):
    def setUp(self):
        super().setUp()
        self.notes = {
            NOTE_TYPE_PERSONAL: self.personal,
            NOTE_TYPE_TASK: self.task_note,
            NOTE_TYPE_CHAT: self.chat_note,
        }
        for code, note in self.notes.items():
            self.make_html(code, note)

    def make_html(self, code, note, html=PAGE):
        prepared = html_notes.prepare(html, summary="s")
        note.body = prepared.body
        note.save(update_fields=["body"])
        html_notes.mark_html(code, note.note_id)
        html_notes.append_revision(code, note.note_id, str(self.user.id), prepared)

    def get(self, user, family, note_id):
        self.authenticate(user)
        return self.client.get(url(family, note_id))

    def single(self, user, family, note_id):
        """What the existing single-note reader says to the same caller."""
        self.authenticate(user)
        return self.client.get(
            f"/api/v2/note/{family}/single/",
            {"team_id": str(self.team.team_id), "note_id": note_id},
        )

    def grant(self, user, code, role_id):
        NotePermissionMaster.objects.update_or_create(
            user=user,
            note_type=code,
            note_id=self.notes[code].note_id,
            defaults={"team": self.team, "role_id": role_id},
        )


class WhoMayRead(HtmlNoteApi):
    def test_the_owner_reads_the_page_of_every_family(self):
        for family, code in FAMILIES:
            with self.subTest(family=family):
                res = self.get(self.user, family, self.notes[code].note_id)
                self.assertEqual(res.status_code, 200, res.content)
                data = res.json()
                self.assertEqual(data["html"], PAGE)
                self.assertEqual(data["rev"], 1)
                self.assertEqual(data["note_type"], family)
                self.assertEqual(data["note_id"], self.notes[code].note_id)
                self.assertEqual(len(data["sha256"]), 64)
                self.assertEqual(data["size"], len(PAGE.encode()))
                self.assertIsInstance(data["warnings"], list)

    def test_the_numbers_1_2_3_name_the_same_families(self):
        for number, code in (
            ("1", NOTE_TYPE_PERSONAL),
            ("2", NOTE_TYPE_TASK),
            ("3", NOTE_TYPE_CHAT),
        ):
            with self.subTest(number=number):
                res = self.get(self.user, number, self.notes[code].note_id)
                self.assertEqual(res.status_code, 200, res.content)

    def test_an_unknown_family_is_a_404(self):
        self.assertEqual(self.get(self.user, "shared", self.personal.note_id).status_code, 404)
        self.assertEqual(self.get(self.user, "9", self.personal.note_id).status_code, 404)

    def test_a_teammate_with_no_role_is_told_what_the_note_reader_tells_them(self):
        for family, code in FAMILIES:
            if code != NOTE_TYPE_PERSONAL:
                continue  # task and chat notes give their members Editor implicitly (below)
            with self.subTest(family=family):
                html = self.get(self.user2, family, self.notes[code].note_id)
                note = self.single(self.user2, family, self.notes[code].note_id)
                self.assertEqual(html.status_code, note.status_code)
                self.assertEqual(html.status_code, 403)
                self.assertEqual(html.json(), note.json())
                self.assertNotIn("html", html.json())

    def test_a_stranger_gets_the_same_404_as_for_a_missing_note(self):
        for family, code in FAMILIES:
            with self.subTest(family=family):
                said = self.get(self.outsider, family, self.notes[code].note_id)
                missing = self.get(self.outsider, family, MISSING)
                self.assertEqual(said.status_code, 404)
                self.assertEqual(
                    (said.status_code, said.json()), (missing.status_code, missing.json())
                )
                self.assertNotIn("Burndown", said.content.decode())

    def test_a_viewer_and_an_editor_grant_both_read_it(self):
        for role in (ROLE_VIEWER, ROLE_EDITOR):
            for family, code in FAMILIES:
                with self.subTest(family=family, role=role):
                    self.grant(self.user2, code, role)
                    res = self.get(self.user2, family, self.notes[code].note_id)
                    self.assertEqual(res.status_code, 200, res.content)

    def test_project_members_read_a_task_note_and_chat_members_a_chat_note(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.assertEqual(self.get(self.user2, "task", self.task_note.note_id).status_code, 200)
        from origin.models.chat.unified_models import ChannelMember

        ChannelMember.objects.create(channel=self.chat_note.channel, user=self.user2, role="member")
        self.assertEqual(self.get(self.user2, "chat", self.chat_note.note_id).status_code, 200)

    def test_it_answers_exactly_as_get_effective_role_and_the_note_reader_do(self):
        """The parity table: every caller, every family."""
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        callers = {"owner": self.user, "teammate": self.user2, "stranger": self.outsider}
        for family, code in FAMILIES:
            note_id = self.notes[code].note_id
            for who, user in callers.items():
                with self.subTest(family=family, who=who):
                    readable = get_effective_role(user.id, code, note_id) is not None
                    html = self.get(user, family, note_id)
                    note = self.single(user, family, note_id)
                    self.assertEqual(html.status_code == 200, readable)
                    self.assertEqual(html.status_code, note.status_code)

    def test_an_anonymous_caller_is_refused(self):
        self.client.credentials()
        res = self.client.get(url("personal", self.personal.note_id))
        self.assertIn(res.status_code, (401, 403))


class TeamFolders(HtmlNoteApi):
    """A Team Notes folder carries the ACL of the personal notes filed in it."""

    def filed(self, visibility, owner=None):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Handbook",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )
        note = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.user,
            title="Filed",
            body=[],
            folder_id=folder.folder_id,
        )
        self.notes[NOTE_TYPE_PERSONAL] = note
        self.make_html(NOTE_TYPE_PERSONAL, note)
        return folder, note

    def test_a_public_team_folder_lets_every_teammate_read_but_not_a_stranger(self):
        _folder, note = self.filed(PersonalNoteFolder.VISIBILITY_PUBLIC)
        self.assertEqual(self.get(self.user2, "personal", note.note_id).status_code, 200)
        self.assertEqual(self.get(self.outsider, "personal", note.note_id).status_code, 404)

    def test_a_private_team_folder_needs_a_grant(self):
        folder, note = self.filed(PersonalNoteFolder.VISIBILITY_PRIVATE)
        before = self.get(self.user2, "personal", note.note_id)
        self.assertEqual(before.status_code, 403)
        self.assertNotIn("html", before.json())
        NoteFolderPermission.objects.create(
            team=self.team, folder_id=folder.folder_id, user=self.user2, role_id=ROLE_VIEWER
        )
        self.assertEqual(self.get(self.user2, "personal", note.note_id).status_code, 200)

    def test_team_folder_answers_match_get_effective_role_and_the_note_reader(self):
        for visibility in (
            PersonalNoteFolder.VISIBILITY_PUBLIC,
            PersonalNoteFolder.VISIBILITY_PRIVATE,
        ):
            _folder, note = self.filed(visibility)
            for user in (self.user, self.user2, self.outsider):
                with self.subTest(visibility=visibility, user=user.username):
                    readable = (
                        get_effective_role(user.id, NOTE_TYPE_PERSONAL, note.note_id) is not None
                    )
                    html = self.get(user, "personal", note.note_id)
                    self.assertEqual(html.status_code == 200, readable)
                    self.assertEqual(
                        html.status_code, self.single(user, "personal", note.note_id).status_code
                    )


class WhatIsServed(HtmlNoteApi):
    def test_it_is_json_never_html_and_never_cached(self):
        res = self.get(self.user, "personal", self.personal.note_id)
        self.assertTrue(res["Content-Type"].startswith("application/json"), res["Content-Type"])
        self.assertIn("no-store", res["Cache-Control"])
        self.assertIn("private", res["Cache-Control"])
        self.assertEqual(res["X-Content-Type-Options"], "nosniff")

    def test_the_newest_revision_is_served(self):
        newer = html_notes.prepare(PAGE.replace("Burndown", "Velocity"), summary="s")
        html_notes.append_revision(
            NOTE_TYPE_PERSONAL, self.personal.note_id, str(self.user.id), newer
        )
        data = self.get(self.user, "personal", self.personal.note_id).json()
        self.assertEqual(data["rev"], 2)
        self.assertIn("Velocity", data["html"])

    def test_the_saved_warnings_come_with_the_page(self):
        bad = html_notes.prepare(PAGE.replace("<h1>", '<img src="https://x.example/a.png"><h1>'))
        html_notes.append_revision(
            NOTE_TYPE_PERSONAL, self.personal.note_id, str(self.user.id), bad
        )
        data = self.get(self.user, "personal", self.personal.note_id).json()
        self.assertIn("remote_image", {w["code"] for w in data["warnings"]})

    def test_an_ordinary_note_is_a_404(self):
        plain = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="p", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=plain.note_id, note_type=1, role_id=1
        )
        res = self.get(self.user, "personal", plain.note_id)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), {"error": "This note is not an HTML note."})

    def test_a_note_marked_html_with_no_page_is_a_404_not_a_500(self):
        from origin.models.note.html_note_models import NoteHtmlRevision

        NoteHtmlRevision.objects.filter(note_id=self.personal.note_id).delete()
        self.assertEqual(self.get(self.user, "personal", self.personal.note_id).status_code, 404)

    def test_after_the_note_is_deleted_the_page_is_gone(self):
        note_id = self.personal.note_id
        PersonalNoteMaster.objects.filter(note_id=note_id).delete()
        self.assertEqual(self.get(self.user, "personal", note_id).status_code, 404)


class FlagOff(HtmlNoteApi):
    @override_settings(HTML_NOTES=False)
    def test_every_route_answers_404_as_if_it_did_not_exist(self):
        for family, code in FAMILIES:
            with self.subTest(family=family):
                res = self.get(self.user, family, self.notes[code].note_id)
                self.assertEqual(res.status_code, 404)
                self.assertNotIn("Burndown", res.content.decode())

    def test_the_setting_is_off_by_default(self):
        from apis import settings

        self.assertFalse(settings.HTML_NOTES)
