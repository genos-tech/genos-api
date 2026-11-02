"""MIP-869 — the agent's other note readers see public Team Notes folders.

`personal_note_acl_user_ids` is built for the search index: a note in a PUBLIC
Team Notes folder contributes one `team:<team_id>` sentinel instead of a user id
per member. A `user_id in allowed` test against it therefore refuses every
teammate. Each case failed closed, so nothing leaked; the note was just silently
missing from mentions, summaries and citations for everyone but its owner.

Four readers answer "may this user read it?" and are held to one table below.
`source_visibility` asks a different question (who may see an answer built on
these sources), so it has its own tests.

Callers: `self.user` owns the notes and the team; `self.user2` is a teammate;
`self.outsider` belongs only to another team.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.citation_resolver import _resolve_personal_notes
from origin.search_engine.agent.mentions import _resolve_note
from origin.search_engine.agent.note_summary import NoteSummaryError, fetch_note_for_agent
from origin.search_engine.agent.source_visibility import shareable_acl_for_sources
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.entity_chips import _personal_note_chip
from origin.search_engine.chunkers.base import NOTE_TYPE_PERSONAL

from .test_base import BaseAPITestCase

_PUBLIC = PersonalNoteFolder.VISIBILITY_PUBLIC
_PRIVATE = PersonalNoteFolder.VISIBILITY_PRIVATE


class _Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.outsider = get_user_model().objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)

    def note(self, visibility=None, owner=None):
        folder = None
        if visibility is not None:
            folder = PersonalNoteFolder.objects.create(
                team=self.team,
                owner=self.user,
                name="Handbook",
                scope=PersonalNoteFolder.SCOPE_TEAM,
                visibility=visibility,
            )
        return PersonalNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.user,
            title="On-call rota",
            body=[],
            folder_id=folder.folder_id if folder else None,
        )


def _by_mention(test, note, user):
    ctx = ToolContext(team_id=str(test.team.team_id), user_id=str(user.id))
    entry = {"note_type": NOTE_TYPE_PERSONAL, "note_id": note.note_id}
    return _resolve_note(entry, ctx) is not None


def _by_summary(test, note, user):
    try:
        fetch_note_for_agent(
            note_type=NOTE_TYPE_PERSONAL, note_id=note.note_id, user_id=str(user.id)
        )
    except NoteSummaryError:
        return False
    return True


def _by_citation(test, note, user):
    found = _resolve_personal_notes(
        [note.note_id],
        str(test.team.team_id),
        str(user.id),
        lambda *args: args,
    )
    return len(found) == 1


def _by_chip(test, note, user):
    return _personal_note_chip(note.note_id, str(test.team.team_id), str(user.id)) is not None


class EveryReaderAgreesTests(_Base):
    READERS = {
        "mentions": _by_mention,
        "note_summary": _by_summary,
        "citation_resolver": _by_citation,
        "entity_chips": _by_chip,
    }

    def assertReaders(self, note, user, expected):
        for name, reader in self.READERS.items():
            with self.subTest(reader=name):
                self.assertIs(reader(self, note, user), expected)

    def test_a_teammate_sees_a_note_in_a_public_folder(self):
        self.assertReaders(self.note(_PUBLIC), self.user2, True)

    def test_someone_outside_the_team_does_not(self):
        self.assertReaders(self.note(_PUBLIC), self.outsider, False)

    def test_a_private_folder_without_a_grant_stays_hidden(self):
        self.assertReaders(self.note(_PRIVATE), self.user2, False)

    def test_an_unfiled_note_stays_with_its_owner(self):
        note = self.note()
        self.assertReaders(note, self.user2, False)
        self.assertReaders(note, self.user, True)


class SharedAnswerAudienceTests(_Base):
    """Who may see a collected answer: everyone who can read ALL its sources."""

    def setUp(self):
        super().setUp()
        self.third = get_user_model().objects.create_user(
            username="third", email="third@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.third)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Audience",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )

    def _note_source(self, note):
        return {"entity_type": "note", "note_type": "personal", "note_id": note.note_id}

    def _task_source(self):
        task_id = self.task.task_id
        return {"entity_type": "task", "entity_id": f"task:{task_id}", "task_id": str(task_id)}

    def _join_project(self, *users):
        for user in users:
            ProjectMembers.objects.get_or_create(project=self.project, attendee=user)

    def test_a_teammate_who_can_read_both_sources_is_in_the_audience(self):
        self._join_project(self.user, self.user2)

        acl = shareable_acl_for_sources(
            [self._note_source(self.note(_PUBLIC)), self._task_source()]
        )

        self.assertEqual(set(acl), {str(self.user.id), str(self.user2.id)})

    def test_the_order_of_the_sources_does_not_matter(self):
        self._join_project(self.user, self.user2)

        acl = shareable_acl_for_sources(
            [self._task_source(), self._note_source(self.note(_PUBLIC))]
        )

        self.assertEqual(set(acl), {str(self.user.id), str(self.user2.id)})

    def test_a_teammate_outside_the_project_is_not(self):
        self._join_project(self.user)

        acl = shareable_acl_for_sources(
            [self._note_source(self.note(_PUBLIC)), self._task_source()]
        )

        self.assertNotIn(str(self.third.id), acl)
        self.assertNotIn(str(self.user2.id), acl)

    def test_a_project_member_from_another_team_is_not(self):
        """Being named on one side is not enough: the sentinel only stands for
        its own team's members."""
        self._join_project(self.user, self.outsider)

        acl = shareable_acl_for_sources(
            [self._note_source(self.note(_PUBLIC)), self._task_source()]
        )

        self.assertEqual(set(acl), {str(self.user.id)})

    def test_two_public_notes_keep_the_sentinel_rather_than_every_member(self):
        acl = shareable_acl_for_sources(
            [self._note_source(self.note(_PUBLIC)), self._note_source(self.note(_PUBLIC))]
        )

        self.assertIn(f"team:{self.team.team_id}", acl)
        self.assertNotIn(str(self.third.id), acl)

    def test_a_private_folder_gains_nothing(self):
        self._join_project(self.user, self.user2)

        acl = shareable_acl_for_sources(
            [self._note_source(self.note(_PRIVATE)), self._task_source()]
        )

        self.assertNotIn(str(self.user2.id), acl or [])
