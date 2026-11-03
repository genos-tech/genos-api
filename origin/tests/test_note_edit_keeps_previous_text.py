"""An agent edit never destroys the only copy of the text it replaces (MIP-883).

Two things made `update_note` lose text, and a third made it claim success it
couldn't back:

* The version row it wrote was COALESCED into the newest one when the same person
  had saved within five minutes. An agent runs as the key's or the user's own
  account, so an edit a minute after the user's own autosave overwrote the row that
  held the text it had just replaced.
* Version rows are written after an edit, so a note nobody had edited through the
  app had none, and the first agent edit left no copy of what it replaced.
* The push into the live editor logged a failure and told nobody. An editor that is
  open keeps the old text and its next autosave writes it back over the edit.
"""

from __future__ import annotations

from unittest import mock

import requests
from django.test import override_settings

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.update_note import UPDATE_NOTE
from origin.services import collab_push
from origin.services.collab_push import PushOutcome, push_note_body_now
from origin.views.utils.note_role import ROLE_OWNER
from origin.views.utils.note_version import keep_current_state, snapshot_note_version

from .test_base import BaseAPITestCase

PERSONAL = 1


def blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text, "styles": {}}]}]


def words(body):
    return "".join(s.get("text", "") for b in body or [] for s in b.get("content") or [])


class NoteVersionBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan", body=blocks("the original")
        )
        # The explicit owner row the app writes when it creates a note.
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.note.note_id,
            note_type=PERSONAL,
            role_id=ROLE_OWNER,
        )
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def versions(self):
        return list(
            NoteVersionMaster.objects.filter(
                note_type=PERSONAL, note_id=self.note.note_id
            ).order_by("version_no")
        )

    def edit(self, text):
        return UPDATE_NOTE.run(
            {"note_type": "personal", "note_id": self.note.note_id, "content_text": text}, self.ctx
        )

    def autosave(self, text, user=None):
        """What the app's own save does: a coalescing snapshot by the editing user."""
        self.note.body = blocks(text)
        self.note.save(update_fields=["body"])
        return snapshot_note_version(
            team=self.team,
            editor=user or self.user,
            note_type=PERSONAL,
            note_id=self.note.note_id,
            title=self.note.title,
            body=self.note.body,
        )


class TheReplacedTextSurvives(NoteVersionBase):
    def test_an_edit_right_after_the_users_own_save_does_not_overwrite_it(self):
        self.autosave("what the user just typed")
        typed = self.versions()[0]

        self.edit("the agent's rewrite")

        rows = self.versions()
        self.assertEqual(
            words(rows[0].body), "what the user just typed", "the user's row is intact"
        )
        self.assertEqual(rows[0].pk, typed.pk)
        self.assertEqual(words(rows[-1].body), "the agent's rewrite")
        self.assertEqual(len(rows), 2)

    def test_a_note_with_no_history_still_keeps_what_it_held(self):
        self.assertEqual(self.versions(), [])
        self.edit("first agent edit")
        rows = self.versions()
        self.assertEqual([words(r.body) for r in rows], ["the original", "first agent edit"])
        self.assertIsNone(rows[0].editor_id, "nobody made that state")
        self.assertEqual(rows[1].editor_id, self.user.id)

    def test_the_original_can_be_walked_back_to_through_the_apps_restore(self):
        self.edit("a mistaken rewrite")
        self.client.credentials()
        self.authenticate()
        res = self.client.post(
            "/api/v2/note/version/restore/",
            {
                "team_id": str(self.team.team_id),
                "note_type": PERSONAL,
                "note_id": self.note.note_id,
                "version_no": self.versions()[0].version_no,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.note.refresh_from_db()
        self.assertEqual(words(self.note.body), "the original")

    def test_a_second_edit_does_not_add_a_duplicate_of_the_state_it_replaces(self):
        self.edit("one")
        self.edit("two")
        self.assertEqual([words(r.body) for r in self.versions()], ["the original", "one", "two"])

    def test_a_title_only_edit_is_kept_too(self):
        UPDATE_NOTE.run(
            {"note_type": "personal", "note_id": self.note.note_id, "title": "Renamed"}, self.ctx
        )
        rows = self.versions()
        self.assertEqual([r.title for r in rows], ["Plan", "Renamed"])

    def test_an_edit_that_changes_nothing_writes_nothing(self):
        """Saying the same thing again is not an edit."""
        self.edit("same words")
        rows = len(self.versions())
        again = self.edit("same words")
        self.assertEqual(again["changed_fields"], [])
        self.assertEqual(len(self.versions()), rows)


class TheVersionFunctions(NoteVersionBase):
    def kw(self, **extra):
        return dict(
            team=self.team,
            note_type=PERSONAL,
            note_id=self.note.note_id,
            title="Plan",
            body=blocks("x"),
            **extra,
        )

    def test_the_apps_save_still_coalesces_a_typing_session(self):
        snapshot_note_version(editor=self.user, **self.kw())
        snapshot_note_version(editor=self.user, **self.kw())
        self.assertEqual(len(self.versions()), 1)

    def test_force_new_does_not_coalesce(self):
        snapshot_note_version(editor=self.user, **self.kw())
        snapshot_note_version(editor=self.user, force_new=True, **self.kw())
        self.assertEqual(len(self.versions()), 2)

    def test_keep_current_state_writes_nothing_when_the_newest_row_matches(self):
        first = keep_current_state(
            team=self.team,
            note_type=PERSONAL,
            note_id=self.note.note_id,
            title="Plan",
            body=self.note.body,
        )
        again = keep_current_state(
            team=self.team,
            note_type=PERSONAL,
            note_id=self.note.note_id,
            title="Plan",
            body=self.note.body,
        )
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(len(self.versions()), 1)

    def test_keep_current_state_writes_a_row_when_the_newest_differs(self):
        self.autosave("older")
        keep_current_state(
            team=self.team,
            note_type=PERSONAL,
            note_id=self.note.note_id,
            title="Plan",
            body=blocks("what the note holds now"),
        )
        self.assertEqual(
            [words(r.body) for r in self.versions()], ["older", "what the note holds now"]
        )

    def test_keep_current_state_notices_a_title_that_differs(self):
        """Same body, different title: the title an edit is about to replace is
        part of what has to be kept."""
        snapshot_note_version(editor=self.user, **self.kw())
        keep_current_state(
            team=self.team,
            note_type=PERSONAL,
            note_id=self.note.note_id,
            title="Renamed in place",
            body=blocks("x"),
        )
        self.assertEqual([r.title for r in self.versions()], ["Plan", "Renamed in place"])

    def test_an_empty_body_and_none_are_the_same_state(self):
        keep_current_state(team=self.team, note_type=PERSONAL, note_id=1, title="", body=None)
        keep_current_state(team=self.team, note_type=PERSONAL, note_id=1, title="", body=[])
        self.assertEqual(NoteVersionMaster.objects.filter(note_id=1).count(), 1)


@override_settings(
    COLLAB_INTERNAL_URL="http://collab.internal:8891", COLLAB_INTERNAL_SECRET="test-only-secret"
)
class ThePushSaysWhetherItWorked(NoteVersionBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(collab_push.requests, "post")
        self.post = patcher.start()
        self.post.return_value = mock.Mock(status_code=200, text="{}")
        self.addCleanup(patcher.stop)
        # Tests run inside a transaction; a tool in production does not.
        outside = mock.patch.object(collab_push, "_in_transaction", return_value=False)
        outside.start()
        self.addCleanup(outside.stop)

    def push(self):
        return push_note_body_now("personal", self.note.note_id, blocks("x"))

    def test_a_push_collab_applied_is_sent(self):
        self.assertEqual(self.push(), PushOutcome.SENT)
        self.post.assert_called_once()

    def test_a_refusal_is_failed(self):
        self.post.return_value = mock.Mock(status_code=500, text="boom")
        self.assertEqual(self.push(), PushOutcome.FAILED)

    def test_an_unreachable_collab_is_failed(self):
        self.post.side_effect = requests.ConnectionError("down")
        self.assertEqual(self.push(), PushOutcome.FAILED)

    def test_a_timeout_is_failed(self):
        self.post.side_effect = requests.Timeout("slow")
        self.assertEqual(self.push(), PushOutcome.FAILED)

    @override_settings(COLLAB_INTERNAL_URL="")
    def test_collab_not_configured_is_skipped_not_failed(self):
        self.assertEqual(self.push(), PushOutcome.SKIPPED)
        self.post.assert_not_called()

    @override_settings(COLLAB_INTERNAL_SECRET="")
    def test_no_secret_is_skipped(self):
        self.assertEqual(self.push(), PushOutcome.SKIPPED)
        self.post.assert_not_called()

    def test_inside_a_transaction_the_push_waits_for_commit(self):
        with mock.patch.object(collab_push, "_in_transaction", return_value=True):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                outcome = self.push()
        self.assertEqual(outcome, PushOutcome.DEFERRED)
        self.post.assert_not_called()
        self.assertEqual(len(callbacks), 1, "a rolled-back write must never push")

    def test_update_note_tells_the_model_when_the_editor_was_not_updated(self):
        self.post.return_value = mock.Mock(status_code=502, text="bad gateway")
        result = self.edit("a new body")
        self.assertEqual(result["live_editor"], "failed")
        self.assertIn("could not be updated", result["__summary__"])
        self.assertIn("saved", result["__summary__"])
        self.note.refresh_from_db()
        self.assertEqual(words(self.note.body), "a new body", "the edit itself is kept")

    def test_update_note_reports_a_good_push_without_alarm(self):
        result = self.edit("a new body")
        self.assertEqual(result["live_editor"], "sent")
        self.assertNotIn("could not be updated", result["__summary__"])

    def test_a_title_only_edit_pushes_nothing_and_says_nothing(self):
        result = UPDATE_NOTE.run(
            {"note_type": "personal", "note_id": self.note.note_id, "title": "Renamed"}, self.ctx
        )
        self.post.assert_not_called()
        self.assertNotIn("live_editor", result)
