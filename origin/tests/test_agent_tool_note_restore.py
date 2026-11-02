"""`fetch_note_history` and `restore_note_version` let a bad edit be walked back (MIP-883).

A restore is only as good as three promises: the text it replaces is kept (so it can be
undone), the version it came from stays intact (so it can be done again), and an editor
that is open is told, or the tool says plainly that it could not tell it. The page's
restore handler calls the same service, and the last class pins what it answers.
"""

from __future__ import annotations

from unittest import mock
from urllib.parse import urlencode

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden, wrap_workspace_content
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.fetch_note_history import FETCH_NOTE_HISTORY
from origin.search_engine.agent.tools.restore_note_version import RESTORE_NOTE_VERSION
from origin.services.collab_push import PushOutcome
from origin.services.note_version_restore import RestoreRefused
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER
from origin.views.utils.note_version import snapshot_note_version

PERSONAL, CHAT = 1, 3
PUSH = "origin.search_engine.agent.tools.restore_note_version.push_note_body_now"


def blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text, "styles": {}}]}]


def words(body):
    return "".join(s.get("text", "") for b in body or [] for s in b.get("content") or [])


class RestoreBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.stranger = get_user_model().objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.stranger)
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan v3", body=blocks("third draft")
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.note.note_id,
            note_type=PERSONAL,
            role_id=ROLE_OWNER,
        )
        self.v1 = self.version("Plan v1", "first draft", editor=self.user)
        self.v2 = self.version("Plan v2", "second draft", editor=self.user2, force_new=True)
        patcher = mock.patch(PUSH, return_value=PushOutcome.SENT)
        self.push = patcher.start()
        self.addCleanup(patcher.stop)

    def version(self, title, text, *, editor, force_new=True, note=None, code=PERSONAL):
        note = note or self.note
        return snapshot_note_version(
            team=self.team,
            editor=editor,
            note_type=code,
            note_id=note.note_id,
            title=title,
            body=blocks(text),
            force_new=force_new,
        )

    def ctx(self, user=None):
        return ToolContext(team_id=str(self.team.team_id), user_id=str((user or self.user).id))

    def history(self, args=None, user=None):
        args = {"note_type": "personal", "note_id": self.note.note_id, **(args or {})}
        return FETCH_NOTE_HISTORY.run(args, self.ctx(user))

    def restore(self, version_no, user=None, **extra):
        args = {
            "note_type": "personal",
            "note_id": self.note.note_id,
            "version_no": version_no,
            **extra,
        }
        return RESTORE_NOTE_VERSION.run(args, self.ctx(user))

    def refused(self, run, message, kind=ToolError):
        with self.assertRaises(kind) as caught:
            run()
        self.assertIn(message, str(caught.exception))

    def rows(self, note=None, code=PERSONAL):
        return list(
            NoteVersionMaster.objects.filter(
                note_type=code, note_id=(note or self.note).note_id
            ).order_by("version_no")
        )


class ReadingTheHistory(RestoreBase):
    def test_versions_come_newest_first_with_who_and_a_preview(self):
        out = self.history()
        self.assertEqual([v["version_no"] for v in out["versions"]], [2, 1])
        newest = out["versions"][0]
        self.assertEqual((newest["title"], newest["saved_by"]), ("Plan v2", "otheruser"))
        self.assertIn("second draft", newest["preview"])
        self.assertIsNone(newest["restored_from_version_no"])
        self.assertFalse(out["truncated"])
        self.assertIn("has 2 saved versions", out["__summary__"])

    def test_a_copy_kept_before_an_edit_has_no_author(self):
        self.version("Kept", "kept text", editor=None)
        self.assertIsNone(self.history()["versions"][0]["saved_by"])

    def test_a_restore_row_says_what_it_came_from(self):
        self.restore(1)
        self.assertEqual(self.history()["versions"][0]["restored_from_version_no"], 1)

    def test_one_version_in_full(self):
        out = self.history({"version_no": 1})
        self.assertEqual((out["version_no"], out["title"], out["is_newest"]), (1, "Plan v1", False))
        self.assertIn("first draft", out["body_markdown"])
        self.assertTrue(self.history({"version_no": 2})["is_newest"])

    def test_text_from_a_note_is_wrapped_as_untrusted_content(self):
        """Version bodies were written by people; the model must not read them as orders."""
        out = self.history({"version_no": 1})
        self.assertEqual(
            out["body_markdown"], wrap_workspace_content(blocks_to_markdown(self.v1.body))
        )
        self.assertNotEqual(out["body_markdown"], blocks_to_markdown(self.v1.body))
        preview = self.history()["versions"][-1]["preview"]
        self.assertEqual(preview, wrap_workspace_content("first draft"))

    def test_a_preview_is_the_first_120_characters_of_the_text(self):
        long_text = "word " * 100
        self.version("Long", long_text, editor=self.user)
        preview = self.history()["versions"][0]["preview"]
        self.assertEqual(preview, wrap_workspace_content(" ".join(long_text.split())[:120]))

    def test_a_version_that_does_not_exist_says_how_to_list_them(self):
        self.refused(lambda: self.history({"version_no": 99}), "Version 99 not found")
        self.refused(lambda: self.history({"version_no": "abc"}), "`version_no` must be an integer")

    def test_a_long_history_is_cut_at_fifty_and_says_so(self):
        for n in range(3, 60):
            self.version(f"v{n}", f"text {n}", editor=self.user)
        out = self.history()
        self.assertEqual(len(out["versions"]), 50)
        self.assertTrue(out["truncated"])
        self.assertEqual(out["versions"][0]["version_no"], 59)

    def test_another_familys_versions_with_the_same_id_are_not_listed(self):
        NoteVersionMaster.objects.create(
            team=self.team,
            note_type=2,
            note_id=self.note.note_id,
            version_no=1,
            title="A task note",
            body=blocks("elsewhere"),
            editor=self.user,
        )
        self.assertEqual(len(self.history()["versions"]), 2)

    def test_someone_elses_private_note_and_another_teams_read_as_missing(self):
        private = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Private", body=[]
        )
        for note_id in (private.note_id, 987654):
            with self.subTest(note_id=note_id):
                self.refused(lambda: self.history({"note_id": note_id}), "not found")

    def test_it_is_a_read(self):
        self.assertFalse(FETCH_NOTE_HISTORY.requires_approval)
        self.assertTrue(RESTORE_NOTE_VERSION.requires_approval)


class RestoringAVersion(RestoreBase):
    def test_the_title_and_body_come_back_and_the_restore_is_recorded(self):
        out = self.restore(1)
        self.note.refresh_from_db()
        self.assertEqual((self.note.title, words(self.note.body)), ("Plan v1", "first draft"))
        newest = self.rows()[-1]
        self.assertEqual(
            (newest.version_no, newest.restored_from_version_no), (out["new_version_no"], 1)
        )
        self.assertEqual(newest.editor_id, self.user.id)
        self.assertEqual(out["restored_from_version_no"], 1)
        self.assertIn('Restored "Plan v1" to version 1', out["__summary__"])

    def test_what_it_replaced_is_kept_so_the_restore_can_be_undone(self):
        """The note says "third draft" and no version holds it."""
        self.assertNotIn("third draft", [words(r.body) for r in self.rows()])
        self.restore(1)
        kept = [(r.title, words(r.body), r.editor_id) for r in self.rows()]
        self.assertIn(("Plan v3", "third draft", None), kept)
        # and the restore is walked back by restoring that copy
        kept_no = next(r.version_no for r in self.rows() if words(r.body) == "third draft")
        self.restore(kept_no)
        self.note.refresh_from_db()
        self.assertEqual((self.note.title, words(self.note.body)), ("Plan v3", "third draft"))

    def test_nothing_is_added_when_the_newest_version_already_is_the_current_state(self):
        self.note.title, self.note.body = "Plan v2", blocks("second draft")
        self.note.save()
        before = len(self.rows())
        self.restore(1)
        self.assertEqual(len(self.rows()), before + 1, "only the restore's own row")

    def test_the_version_it_came_from_is_left_intact(self):
        self.restore(1)
        self.assertEqual(words(self.rows()[0].body), "first draft")
        self.assertEqual(self.rows()[0].title, "Plan v1")

    def test_the_restore_does_not_overwrite_the_callers_own_recent_version(self):
        """Same person, within the coalescing window: the restore still gets its own row."""
        recent = self.version("Mine", "just typed", editor=self.user, force_new=False)
        self.restore(1)
        recent.refresh_from_db()
        self.assertEqual(words(recent.body), "just typed")

    def test_the_live_editor_is_given_the_restored_body(self):
        self.restore(1)
        self.push.assert_called_once_with("personal", self.note.note_id, blocks("first draft"))

    def test_a_failed_push_is_said_plainly_and_the_restore_is_kept(self):
        self.push.return_value = PushOutcome.FAILED
        out = self.restore(1)
        self.assertEqual(out["live_editor"], "failed")
        self.assertIn("could not be updated", out["__summary__"])
        self.assertIn("overwrite the restore", out["__summary__"])
        self.note.refresh_from_db()
        self.assertEqual(words(self.note.body), "first draft")

    def test_a_push_that_worked_or_had_nowhere_to_go_raises_no_alarm(self):
        for outcome in (PushOutcome.SENT, PushOutcome.SKIPPED, PushOutcome.DEFERRED):
            with self.subTest(outcome=outcome):
                self.push.return_value = outcome
                out = self.restore(1)
                self.assertEqual(out["live_editor"], outcome.value)
                self.assertNotIn("could not be updated", out["__summary__"])

    def test_a_chat_note_is_restored_and_pushed_to_its_own_document(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="DM")
        ChannelMember.objects.create(channel=dm, user=self.user)
        chat = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="Chat v2",
            body=blocks("now"),
            chat_type=1,
            channel=dm,
            is_thread=False,
        )
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=chat.note_id, note_type=CHAT, role_id=ROLE_OWNER
        )
        v1 = self.version("Chat v1", "before", editor=self.user, note=chat, code=CHAT)
        RESTORE_NOTE_VERSION.run(
            {"note_type": "chat", "note_id": chat.note_id, "version_no": v1.version_no}, self.ctx()
        )
        chat.refresh_from_db()
        self.assertEqual((chat.title, words(chat.body)), ("Chat v1", "before"))
        self.push.assert_called_once_with("chat", chat.note_id, blocks("before"))

    def test_a_version_of_another_note_cannot_be_restored_onto_this_one(self):
        other = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Other", body=blocks("other body")
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=other.note_id,
            note_type=PERSONAL,
            role_id=ROLE_OWNER,
        )
        self.version("Other v1", "other v1", editor=self.user, note=other)
        # `other` has version 1 only; this note's version 2 must not be reachable through it
        self.refused(
            lambda: RESTORE_NOTE_VERSION.run(
                {"note_type": "personal", "note_id": other.note_id, "version_no": 2}, self.ctx()
            ),
            "Version not found.",
        )
        other.refresh_from_db()
        self.assertEqual(words(other.body), "other body")

    def test_a_version_number_that_exists_only_in_another_family_is_not_found(self):
        bare = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Bare", body=blocks("as is")
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=bare.note_id,
            note_type=PERSONAL,
            role_id=ROLE_OWNER,
        )
        NoteVersionMaster.objects.create(
            team=self.team,
            note_type=2,
            note_id=bare.note_id,
            version_no=1,
            title="Task note",
            body=blocks("not this"),
            editor=self.user,
        )
        self.refused(
            lambda: RESTORE_NOTE_VERSION.run(
                {"note_type": "personal", "note_id": bare.note_id, "version_no": 1}, self.ctx()
            ),
            "Version not found.",
        )
        bare.refresh_from_db()
        self.assertEqual(words(bare.body), "as is")

    def test_a_restore_is_stamped_so_the_sidebar_and_search_see_it(self):
        stamp = self.note.ts_updated_at
        self.restore(1)
        self.note.refresh_from_db()
        self.assertGreater(self.note.ts_updated_at, stamp)

    def test_a_restore_that_fails_part_way_changes_nothing(self):
        with mock.patch(
            "origin.services.note_version_restore.snapshot_note_version",
            side_effect=RuntimeError("disk full"),
        ):
            self.refused(lambda: self.restore(1), "Failed to restore the version")
        self.note.refresh_from_db()
        self.assertEqual((self.note.title, words(self.note.body)), ("Plan v3", "third draft"))
        self.assertEqual(len(self.rows()), 2, "the kept copy is rolled back with it")
        self.push.assert_not_called()

    def test_the_hint_to_list_versions_is_only_for_a_missing_version(self):
        with mock.patch(
            "origin.search_engine.agent.tools.restore_note_version.restore_note_version",
            side_effect=RestoreRefused("Note not found.", 404),
        ):
            with self.assertRaises(ToolError) as caught:
                self.restore(1)
        self.assertEqual(str(caught.exception), "Note not found. Nothing was changed.")

    def test_a_missing_version_is_refused_with_the_way_to_list_them(self):
        self.refused(lambda: self.restore(99), "Version not found. Call fetch_note_history")
        self.note.refresh_from_db()
        self.assertEqual(words(self.note.body), "third draft")
        self.push.assert_not_called()

    def test_the_arguments_are_checked(self):
        self.refused(lambda: self.restore("abc"), "`version_no` must be an integer")
        self.refused(
            lambda: RESTORE_NOTE_VERSION.run(
                {"note_type": "personal", "note_id": self.note.note_id}, self.ctx()
            ),
            "`version_no` must be an integer",
        )


class WhoMayRestore(RestoreBase):
    def shared(self, role):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=self.note.note_id,
            note_type=PERSONAL,
            role_id=role,
        )

    def test_a_viewer_can_read_the_history_but_not_restore(self):
        self.shared(ROLE_VIEWER)
        self.assertEqual(len(self.history(user=self.user2)["versions"]), 2)
        self.refused(lambda: self.restore(1, user=self.user2), "you can read", kind=ToolForbidden)
        self.note.refresh_from_db()
        self.assertEqual(words(self.note.body), "third draft")
        self.push.assert_not_called()

    def test_an_editor_may(self):
        self.shared(ROLE_EDITOR)
        self.restore(1, user=self.user2)
        self.note.refresh_from_db()
        self.assertEqual(words(self.note.body), "first draft")
        self.assertEqual(self.rows()[-1].editor_id, self.user2.id)

    def test_an_author_with_no_permission_row_may(self):
        NotePermissionMaster.objects.filter(note_id=self.note.note_id).delete()
        self.restore(1)
        self.note.refresh_from_db()
        self.assertEqual(words(self.note.body), "first draft")

    def test_someone_with_no_access_reads_it_as_missing(self):
        self.refused(lambda: self.restore(1, user=self.user2), "not found")

    def test_the_approval_card_names_the_note(self):
        card = controller._friendly_arguments(
            {"note_type": "personal", "note_id": self.note.note_id, "version_no": 1},
            "restore_note_version",
            ctx=self.ctx(),
        )
        self.assertEqual(card["note_title"], "Plan v3")


class ThePagesRestoreStillWorks(RestoreBase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def post(self, version_no):
        return self.client.post(
            "/api/v2/note/version/restore/",
            {
                "team_id": self.team.team_id,
                "note_type": PERSONAL,
                "note_id": self.note.note_id,
                "version_no": version_no,
            },
            format="json",
        )

    def test_the_page_restores_and_answers_with_the_new_version(self):
        res = self.post(1)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["restoredFromVersionNo"], 1)
        self.assertEqual(res.data["title"], "Plan v1")
        self.assertEqual(words(res.data["body"]), "first draft")
        self.note.refresh_from_db()
        self.assertEqual(words(self.note.body), "first draft")

    def test_the_page_now_keeps_what_it_replaced_too(self):
        self.post(1)
        self.assertIn("third draft", [words(r.body) for r in self.rows()])

    def test_a_missing_version_is_still_a_404(self):
        res = self.post(99)
        self.assertEqual((res.status_code, res.data["error"]), (404, "Version not found."))

    def test_the_page_still_demands_write_access(self):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=self.note.note_id,
            note_type=PERSONAL,
            role_id=ROLE_VIEWER,
        )
        self.authenticate(self.user2)
        res = self.post(1)
        self.assertEqual(res.status_code, 403)
        self.note.refresh_from_db()
        self.assertEqual(words(self.note.body), "third draft")

    def test_the_page_lists_what_the_tool_restored(self):
        self.restore(1)
        res = self.client.get(
            "/api/v2/note/version/all/?"
            + urlencode(
                {"team_id": self.team.team_id, "note_type": PERSONAL, "note_id": self.note.note_id}
            )
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data[0]["restoredFromVersionNo"], 1)
