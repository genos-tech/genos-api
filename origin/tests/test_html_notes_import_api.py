"""`POST /api/v3/notes/html/import/`, the `html_notes` key of `/agent/features/`, and `bodyFormat` on
the note payloads the web client reads (MIP-2215).

What is pinned:

- the import is the MCP tool's own function: a note of every kind (personal, team, task, chat) is made
  with `body_format="html"` and revision 1, the plan gate holds (Free stores nothing and gets a
  `saved: false` answer, not an error), the plan's size limit is the tool's, and `no_summary` is not
  shown to a person;
- the route is `HTML_NOTES`-gated (404 off), needs a login (401), and a team the caller is not in is
  the usual 404;
- `/agent/features/` says `html_notes` only while the switch is on: `max_bytes` null for Free, the
  plan's number otherwise;
- every note read and list the client uses carries `bodyFormat` ("html" for an HTML note).
"""

from __future__ import annotations

from unittest import mock

from django.test import override_settings

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.html_note_models import NoteHtmlRevision
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.services import html_note_limits, html_notes
from origin.tests.test_note_role_probes_404 import Probes

IMPORT = "/api/v3/notes/html/import/"
FEATURES = "/api/v2/agent/features/"
TIER_PATH = "origin.search_engine.quota.get_effective_tier"
GOOD = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Sprint burndown</title>"
    "</head><body><h1>Sprint burndown</h1><p>Velocity dropped.</p><script>var d=[1];</script>"
    "</body></html>"
)
BAD = (
    "<!doctype html><html><head><title>Bad</title>"
    '<script src="https://cdn.example.com/chart.js"></script></head><body>'
    "<script>fetch('/api')</script></body></html>"
)
CORE = html_note_limits.HTML_NOTE_LIMITS["core"]


@override_settings(HTML_NOTES=True)
class ImportBase(Probes):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(TIER_PATH, return_value="core")
        self.tier = patcher.start()
        self.addCleanup(patcher.stop)
        # An open editor is told about the page only on a replace; an import never replaces.
        self.authenticate(self.user)

    def post(self, **fields):
        body = {"team_id": str(self.team.team_id), "html": GOOD, "note_type": "personal"}
        body.update(fields)
        return self.client.post(IMPORT, body, format="json")

    def team_folder(self):
        return PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Reports",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )


class ImportCreates(ImportBase):
    def test_a_personal_note_is_made_with_the_page_and_the_title_of_the_page(self):
        res = self.post()
        self.assertEqual(res.status_code, 201, res.content)
        data = res.json()
        self.assertTrue(data["saved"])
        self.assertEqual(
            (data["note_type"], data["title"], data["html_rev"]), ("personal", "Sprint burndown", 1)
        )
        note = PersonalNoteMaster.objects.get(note_id=data["note_id"])
        self.assertEqual((note.body_format, note.owner_id), ("html", self.user.id))
        self.assertEqual(NoteHtmlRevision.objects.get(note_type=1, note_id=note.note_id).html, GOOD)

    def stored_html(self, res):
        self.assertEqual(res.status_code, 201, res.content)
        return NoteHtmlRevision.objects.get(note_type=1, note_id=res.json()["note_id"]).html

    def test_an_upload_with_an_escaped_closing_fence_tag_is_stored_byte_for_byte(self):
        # MIP-2301: the tool's wrapper un-escapes a fence tag the in-app model copied back; an
        # upload is a person's text, so it must reach the store unchanged.
        page = (
            "<!doctype html><html><head><title>Fence</title></head><body>"
            "<p>&lt;/workspace_content> and &amp;lt;/web_content></p></body></html>"
        )
        self.assertEqual(self.stored_html(self.post(html=page)), page)

    def test_an_upload_that_is_exactly_a_whole_fence_is_stored_unchanged(self):
        page = (
            "<workspace_content><!doctype html><html><head><title>W</title></head>"
            "<body><p>x</p></body></html></workspace_content>"
        )
        self.assertEqual(self.stored_html(self.post(html=page, title="Whole fence")), page)

    def test_a_title_the_person_typed_wins_over_the_page_title(self):
        res = self.post(title="My report")
        self.assertEqual(res.json()["title"], "My report")

    def test_a_personal_note_may_go_in_a_folder(self):
        folder = PersonalNoteFolder.objects.create(team=self.team, owner=self.user, name="Mine")
        res = self.post(folder_id=folder.folder_id)
        self.assertEqual(res.status_code, 201, res.content)
        note = PersonalNoteMaster.objects.get(note_id=res.json()["note_id"])
        self.assertEqual(note.folder_id, folder.folder_id)

    def test_a_team_note_needs_a_folder_and_goes_into_it(self):
        refused = self.post(note_type="team")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("folder_id", refused.json()["error"])
        self.assertEqual(NoteHtmlRevision.objects.count(), 0)

        folder = self.team_folder()
        res = self.post(note_type="team", folder_id=folder.folder_id)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["folder_id"], folder.folder_id)
        note = PersonalNoteMaster.objects.get(note_id=res.json()["note_id"])
        self.assertEqual((note.folder_id, note.body_format), (folder.folder_id, "html"))

    def test_a_task_note_belongs_to_the_task(self):
        res = self.post(
            note_type="task", project_id=self.project.project_id, task_id=self.task.task_id
        )
        self.assertEqual(res.status_code, 201, res.content)
        note = TaskNoteMaster.objects.get(note_id=res.json()["note_id"])
        self.assertEqual((note.task_id, note.body_format), (self.task.task_id, "html"))
        self.assertEqual(NoteHtmlRevision.objects.filter(note_type=2).count(), 1)

    def test_a_chat_note_is_made_on_the_chat(self):
        channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="gm2", owner=self.user
        )
        ChannelMember.objects.create(channel=channel, user=self.user, role="owner")
        res = self.post(note_type="chat", chat_id=str(channel.id))
        self.assertEqual(res.status_code, 201, res.content)
        note = ChatNoteMaster.objects.get(note_id=res.json()["note_id"])
        self.assertEqual((note.channel_id, note.body_format), (channel.id, "html"))

    def test_a_note_id_is_ignored_an_import_never_replaces(self):
        first = self.post().json()
        second = self.post(note_id=first["note_id"])
        self.assertEqual(second.status_code, 201, second.content)
        self.assertNotEqual(second.json()["note_id"], first["note_id"])
        self.assertEqual(NoteHtmlRevision.objects.filter(note_id=first["note_id"]).count(), 1)

    def test_warnings_come_back_without_the_advice_to_a_model(self):
        res = self.post(html=BAD)
        self.assertEqual(res.status_code, 201, res.content)
        codes = [w["code"] for w in res.json()["warnings"]]
        self.assertTrue(codes)
        self.assertNotIn("no_summary", codes)
        for warning in res.json()["warnings"]:
            self.assertTrue(warning["message"] and warning["fix"])
        self.assertNotIn("next_step", res.json())


class ImportGates(ImportBase):
    def test_a_free_plan_stores_nothing_and_is_told_so_without_an_error(self):
        self.tier.return_value = "free"
        res = self.post()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {"saved": False, "plan_required": "core", "your_plan": "free"})
        self.assertEqual(NoteHtmlRevision.objects.count(), 0)

    def test_a_page_over_the_plans_size_is_refused_with_the_numbers(self):
        big = GOOD.replace("</body>", "<p>" + "x" * CORE.max_bytes + "</p></body>")
        res = self.post(html=big)
        self.assertEqual(res.status_code, 400)
        self.assertIn(f"{CORE.max_bytes:,}", res.json()["error"])
        self.assertEqual(NoteHtmlRevision.objects.count(), 0)

    def test_a_page_with_no_title_anywhere_is_refused_with_the_fix(self):
        res = self.post(html="<!doctype html><p>no title</p>")
        self.assertEqual(res.status_code, 400)
        self.assertIn("title", res.json()["error"].lower())

    def test_the_hourly_limit_is_the_plans_and_is_a_refusal_not_a_crash(self):
        with mock.patch.object(
            html_notes, "check_rate", side_effect=html_notes.HtmlNoteRefused("Too many saves.")
        ):
            res = self.post()
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], "Too many saves.")

    def test_a_folder_that_is_not_yours_is_refused(self):
        theirs = PersonalNoteFolder.objects.create(team=self.team, owner=self.user2, name="Theirs")
        res = self.post(folder_id=theirs.folder_id)
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(NoteHtmlRevision.objects.count(), 0)

    def test_the_route_is_a_404_while_the_switch_is_off(self):
        with override_settings(HTML_NOTES=False):
            res = self.post()
        self.assertEqual(res.status_code, 404)
        self.assertEqual(NoteHtmlRevision.objects.count(), 0)

    def test_a_login_is_needed(self):
        self.client.credentials()
        res = self.client.post(IMPORT, {"html": GOOD}, format="json")
        self.assertIn(res.status_code, (401, 403))

    def test_a_team_the_caller_is_not_in_is_a_404(self):
        self.authenticate(self.outsider)
        res = self.post()
        self.assertEqual(res.status_code, 404, res.content)
        self.assertEqual(NoteHtmlRevision.objects.count(), 0)

    def test_a_missing_or_bad_team_is_a_400(self):
        self.assertEqual(self.post(team_id="not-a-uuid").status_code, 400)
        self.assertEqual(self.post(team_id="").status_code, 400)


class FeaturesKey(ImportBase):
    def features(self):
        # The endpoint reads the plan through `resolve_effective_tier`, so that is what a test sets.
        resolved = {"tier": self.tier.return_value, "source": "personal", "team_name": None}
        with mock.patch(
            "origin.search_engine.agent_views.resolve_effective_tier", return_value=resolved
        ):
            res = self.client.get(FEATURES)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def test_core_sees_the_plans_page_limit(self):
        self.assertEqual(self.features()["html_notes"], {"max_bytes": CORE.max_bytes})

    def test_pro_sees_a_larger_limit(self):
        self.tier.return_value = "pro"
        pro = html_note_limits.HTML_NOTE_LIMITS["pro"]
        self.assertEqual(self.features()["html_notes"], {"max_bytes": pro.max_bytes})

    def test_free_sees_null_so_the_dialog_can_say_it_is_a_core_feature(self):
        self.tier.return_value = "free"
        self.assertEqual(self.features()["html_notes"], {"max_bytes": None})

    def test_the_key_does_not_exist_while_the_switch_is_off(self):
        with override_settings(HTML_NOTES=False):
            self.assertNotIn("html_notes", self.features())


class BodyFormatOnPayloads(ImportBase):
    """Every payload the client builds a note, a sidebar row or a recent row from says the format."""

    def setUp(self):
        super().setUp()
        for code, note in (
            (1, self.personal),
            (2, self.task_note),
            (3, self.chat_note),
        ):
            prepared = html_notes.prepare(GOOD, summary="s")
            note.body = prepared.body
            note.save(update_fields=["body"])
            html_notes.mark_html(code, note.note_id)
            html_notes.append_revision(code, note.note_id, str(self.user.id), prepared)
        self.team_id = str(self.team.team_id)
        self.user_id = str(self.user.id)

    def rows(self, res):
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()
        return data if isinstance(data, list) else [data]

    def test_single_note_reads(self):
        for path, note in (
            ("personal", self.personal),
            ("task", self.task_note),
            ("chat", self.chat_note),
        ):
            with self.subTest(path=path):
                res = self.client.get(
                    f"/api/v2/note/{path}/single/",
                    {"team_id": self.team_id, "note_id": note.note_id},
                )
                self.assertEqual(self.rows(res)[0]["bodyFormat"], "html")

    def test_an_ordinary_note_says_blocknote(self):
        plain = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="plain", body=[]
        )
        from origin.models.note.common_note_models import NotePermissionMaster

        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=plain.note_id, note_type=1, role_id=1
        )
        res = self.client.get(
            "/api/v2/note/personal/single/",
            {"team_id": self.team_id, "note_id": plain.note_id},
        )
        self.assertEqual(self.rows(res)[0]["bodyFormat"], "blocknote")

    def test_personal_lists(self):
        for path in ("all", "meta"):
            with self.subTest(path=path):
                res = self.client.get(
                    f"/api/v2/note/personal/{path}/",
                    {"team_id": self.team_id, "user_id": self.user_id},
                )
                rows = [r for r in self.rows(res) if r["noteId"] == self.personal.note_id]
                self.assertEqual([r["bodyFormat"] for r in rows], ["html"])

    def _row(self, res, key, note):
        rows = res.json()[key] if isinstance(res.json(), dict) else res.json()
        return [r for r in rows if r["noteId"] == note.note_id]

    def test_task_and_chat_lists(self):
        for path, note in (("task", self.task_note), ("chat", self.chat_note)):
            for kind in ("all", "meta"):
                with self.subTest(path=path, kind=kind):
                    res = self.client.get(
                        f"/api/v2/note/{path}/{kind}/",
                        {"team_id": self.team_id, "user_id": self.user_id},
                    )
                    self.assertEqual(res.status_code, 200, res.content)
                    rows = [r for r in res.json() if r["noteId"] == note.note_id]
                    self.assertEqual([r["bodyFormat"] for r in rows], ["html"])

    def test_the_team_meta_and_the_shared_meta(self):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Reports",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )
        made = self.client.post(
            IMPORT,
            {
                "team_id": self.team_id,
                "html": GOOD,
                "note_type": "team",
                "folder_id": folder.folder_id,
            },
            format="json",
        ).json()
        res = self.client.get(
            "/api/v2/note/team/meta/", {"team_id": self.team_id, "user_id": self.user_id}
        )
        self.assertEqual(res.status_code, 200, res.content)
        rows = [r for r in res.json() if r["noteId"] == made["note_id"]]
        self.assertEqual([r["bodyFormat"] for r in rows], ["html"])

    def test_favorites_and_recents(self):
        from origin.models.note.favorite_note_models import NoteFavoriteMaster
        from origin.models.note.recent_note_models import NoteRecentMaster

        for code, note in ((1, self.personal), (2, self.task_note), (3, self.chat_note)):
            NoteFavoriteMaster.objects.create(
                team=self.team, user=self.user, note_id=note.note_id, note_type=code
            )
            NoteRecentMaster.objects.create(
                team=self.team, user=self.user, note_id=note.note_id, note_type=code
            )
        for path in ("favorite", "recent"):
            res = self.client.get(
                f"/api/v2/note/{path}/meta/",
                {"team_id": self.team_id, "user_id": self.user_id},
            )
            self.assertEqual(res.status_code, 200, res.content)
            data = res.json()
            for key, note in (
                ("personalNotes", self.personal),
                ("taskNotes", self.task_note),
                ("chatNotes", self.chat_note),
            ):
                with self.subTest(path=path, key=key):
                    rows = [r for r in data[key] if r["noteId"] == note.note_id]
                    self.assertEqual([r["bodyFormat"] for r in rows], ["html"])
