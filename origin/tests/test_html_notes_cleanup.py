"""An HTML note's pages die with their note on the paths that remove whole workspaces (MIP-2214).

`test_html_notes_service.PurgeTests` proves the satellite helper and the model delete. These go
through the REAL teardowns, on a seeded workspace with an HTML page on a note of each family:

- the team purge (`team_purge.purge_team`), which deletes a closed team's data;
- the demo teardown (`delete_demo_environment`), which once died on a team-keyed side table of notes
  (`NoteVersionMaster`) and skipped that user's whole workspace forever. `NoteHtmlRevision` has no
  team key, so the teardown cannot trip on it, and the note's own delete takes its pages.

Another team's pages are left alone. TransactionTestCase, as the teardowns commit.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.html_note_models import NoteHtmlRevision
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.services import html_notes, team_purge
from origin.services.demo_seeder import create_demo_environment, delete_demo_environment
from origin.tests.transaction_support import CheapFlushTransactionTestCase

User = get_user_model()
PERSONAL, TASK, CHAT = 1, 2, 3
PAGE = "<!doctype html><title>T</title><p>page</p>"


class _Seeded(CheapFlushTransactionTestCase):
    def _workspace(self, name):
        user = User.objects.create_user(
            username=name, email=f"{name}@example.com", password="x", is_demo=True
        )
        team_id = create_demo_environment(user)["team_id"]
        return user, team_id

    def _add_pages(self, user, team_id):
        """A page on one seeded note of each family; returns `[(note_type, note_id)]`."""
        pairs = []
        prepared = html_notes.prepare(PAGE, summary="s")
        for code, model in (
            (PERSONAL, PersonalNoteMaster),
            (TASK, TaskNoteMaster),
            (CHAT, ChatNoteMaster),
        ):
            note = model.objects.filter(team=team_id).first()
            if note is None:
                self.skipTest(f"the seeder made no note of type {code}")
            html_notes.mark_html(code, note.note_id)
            html_notes.append_revision(code, note.note_id, str(user.id), prepared)
            html_notes.append_revision(code, note.note_id, str(user.id), prepared)
            pairs.append((code, note.note_id))
        return pairs

    def _pages(self, pairs):
        return {
            pair: NoteHtmlRevision.objects.filter(note_type=pair[0], note_id=pair[1]).count()
            for pair in pairs
        }


class DemoTeardown(_Seeded):
    def test_the_teardown_finishes_and_removes_every_familys_pages(self):
        user, team_id = self._workspace("html_demo")
        pairs = self._add_pages(user, team_id)
        self.assertEqual(set(self._pages(pairs).values()), {2})

        delete_demo_environment(user)

        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(TeamMaster.objects.filter(team_id=team_id).exists())
        self.assertEqual(set(self._pages(pairs).values()), {0})

    def test_another_workspaces_pages_are_left_alone(self):
        user, team_id = self._workspace("html_demo_a")
        other_user, other_team = self._workspace("html_demo_b")
        mine = self._add_pages(user, team_id)
        theirs = self._add_pages(other_user, other_team)

        delete_demo_environment(user)

        self.assertEqual(set(self._pages(mine).values()), {0})
        self.assertEqual(set(self._pages(theirs).values()), {2})

    def test_a_page_whose_note_number_matches_a_note_elsewhere_is_not_confused(self):
        """Keyed by family AND number: a page of a note of ANOTHER family with the same number stays."""
        user, team_id = self._workspace("html_demo_c")
        pairs = self._add_pages(user, team_id)
        stranger_note_id = max(note_id for _, note_id in pairs) + 1000
        keep = NoteHtmlRevision.objects.create(
            note_type=CHAT, note_id=stranger_note_id, rev=1, html=PAGE, sha256="x", size=1
        )

        delete_demo_environment(user)

        self.assertTrue(NoteHtmlRevision.objects.filter(pk=keep.pk).exists())


class TeamPurge(_Seeded):
    def _closed_team(self, name):
        user, team_id = self._workspace(name)
        TeamMaster.objects.filter(team_id=team_id).update(is_demo=False)
        TeamMembers.objects.filter(team_id=team_id).update(is_deleted=True)
        TeamMaster.objects.filter(team_id=team_id).update(is_deleted=True)
        return user, TeamMaster.objects.get(team_id=team_id)

    def _purge(self, team):
        with mock.patch("origin.services.collab_push.forget_document", return_value=True):
            return team_purge.purge_team(team.team_id)

    def test_a_purged_teams_pages_are_deleted_with_its_notes(self):
        user, team = self._closed_team("html_purge")
        pairs = self._add_pages(user, team.team_id)

        outcome = self._purge(team)

        self.assertTrue(outcome.purged)
        self.assertFalse(TeamMaster.objects.filter(team_id=team.team_id).exists())
        self.assertEqual(set(self._pages(pairs).values()), {0})

    def test_another_teams_pages_survive_the_purge(self):
        user, team = self._closed_team("html_purge_a")
        other_user, other_team = self._workspace("html_purge_b")
        self._add_pages(user, team.team_id)
        theirs = self._add_pages(other_user, other_team)

        self._purge(team)

        self.assertEqual(set(self._pages(theirs).values()), {2})

    def test_the_survey_still_counts_the_notes_it_will_delete(self):
        user, team = self._closed_team("html_purge_c")
        self._add_pages(user, team.team_id)
        self.assertGreaterEqual(team_purge.survey(team.team_id)["notes"], 3)
