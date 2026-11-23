"""A Genos Skill's body note stays out of search, the agent's note list and Recent notes (MIP-2121).

The body of a skill is a note in a hidden `scope="skill"` folder (MIP-2040). It is the skill's text,
edited in the Skills editor and run on purpose; it must not come back as a note in the owner's
Spotlight search, in the in-app agent's answers or in Recent notes. The cheap, leak-proof way is
to never index it: the personal-note, note-folder and note-summary chunkers skip it, and the purge
sweep removes any chunk that was indexed before the rule. Every test pairs the hidden case with a
normal note that must be unchanged.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.search_engine import purge
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.list_note_folders import LIST_NOTE_FOLDERS
from origin.search_engine.chunkers.base import NOTE_TYPE_PERSONAL
from origin.search_engine.chunkers.note_chunker import iter_personal_note_chunks
from origin.search_engine.chunkers.note_folder_chunker import iter_note_folder_chunks
from origin.search_engine.chunkers.note_summary_chunker import iter_note_summary_chunks
from origin.search_engine.models import NoteSummary
from origin.tests import test_skills_api
from origin.tests.test_search_chunkers_agent import _bn
from origin.views.utils.note_role import ROLE_OWNER

RECENT = "/api/v2/note/recent/"
RECENT_META = "/api/v2/note/recent/meta/"


@override_settings(GENOS_SKILLS=True, GENOS_SKILLS_PUBLIC=True)
class SkillNotesHiddenTests(test_skills_api.SkillBase):
    def setUp(self):
        super().setUp()
        self.skill = self.make_ok()
        self.skill_note = PersonalNoteMaster.objects.get(note_id=self.skill["noteId"])
        self.skill_folder = PersonalNoteFolder.objects.get(folder_id=self.skill_note.folder_id)
        self.plain_folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name="Ideas"
        )
        self.plain = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plain note", body=_bn("ordinary text")
        )
        self.filed = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="Filed note",
            body=_bn("filed text"),
            folder_id=self.plain_folder.folder_id,
        )

        # The note editor's own rule needs an owner role row to open a note (as the real create
        # path writes), so Recent notes can see them.
        for note in (self.skill_note, self.plain, self.filed):
            NotePermissionMaster.objects.get_or_create(
                team=self.team,
                user=self.user,
                note_id=note.note_id,
                note_type=1,
                defaults={"role_id": ROLE_OWNER},
            )

    # -- the index -------------------------------------------------------------------------

    def test_the_note_chunker_skips_the_skill_note_and_keeps_normal_notes(self):
        entities = {b.entity_id for b in iter_personal_note_chunks()}
        self.assertEqual(
            entities,
            {f"note:personal:{self.plain.note_id}", f"note:personal:{self.filed.note_id}"},
        )

    def test_the_one_note_refresh_lane_skips_it_too(self):
        # `refresh_personal_notes` (a folder move) goes through the same iterator by id.
        got = list(iter_personal_note_chunks(note_ids=[self.skill_note.note_id]))
        self.assertEqual(got, [])
        got = list(iter_personal_note_chunks(note_ids=[self.plain.note_id]))
        self.assertEqual(len(got), 1)

    def test_an_edit_of_the_skill_body_still_indexes_nothing(self):
        self.as_user(self.user)
        res = self.client.patch(
            test_skills_api.detail(self.skill["id"]),
            {"bodyMarkdown": "# New\n\nOther words."},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        since = timezone.now() - timedelta(minutes=5)
        entities = {b.entity_id for b in iter_personal_note_chunks(since=since)}
        self.assertIn(f"note:personal:{self.plain.note_id}", entities)
        self.assertNotIn(f"note:personal:{self.skill_note.note_id}", entities)

    def test_the_folder_chunker_skips_the_skill_folder(self):
        entities = {b.entity_id for b in iter_note_folder_chunks()}
        self.assertEqual(entities, {f"note_folder:{self.plain_folder.folder_id}"})

    def test_the_summary_chunker_skips_a_summary_of_the_skill_note(self):
        for note in (self.skill_note, self.plain):
            NoteSummary.objects.create(
                team_id=self.team_id,
                note_type=NOTE_TYPE_PERSONAL,
                note_id=note.note_id,
                summary_text="A summary.",
            )
        entities = {b.entity_id for b in iter_note_summary_chunks()}
        self.assertEqual(entities, {f"note_summary:{NOTE_TYPE_PERSONAL}:{self.plain.note_id}"})

    # -- chunks indexed before the rule ------------------------------------------------------

    def test_the_sweep_calls_an_old_skill_note_chunk_dead_and_a_normal_one_live(self):
        skill_eid = f"note:personal:{self.skill_note.note_id}"
        plain_eid = f"note:personal:{self.plain.note_id}"
        dead, unparseable = purge._resolve_notes([skill_eid, plain_eid])
        self.assertEqual(dead, {skill_eid})
        self.assertEqual(unparseable, 0)

    def test_the_sweep_calls_an_old_skill_folder_chunk_dead(self):
        skill_eid = f"note_folder:{self.skill_folder.folder_id}"
        plain_eid = f"note_folder:{self.plain_folder.folder_id}"
        dead, _ = purge._resolve_note_folders([skill_eid, plain_eid])
        self.assertEqual(dead, {skill_eid})

    def test_the_sweep_calls_an_old_skill_summary_chunk_dead(self):
        eids = []
        for note in (self.skill_note, self.plain):
            NoteSummary.objects.create(
                team_id=self.team_id,
                note_type=NOTE_TYPE_PERSONAL,
                note_id=note.note_id,
                summary_text="A summary.",
            )
            eids.append(f"note_summary:{NOTE_TYPE_PERSONAL}:{note.note_id}")
        dead, _ = purge._resolve_note_summaries(eids)
        self.assertEqual(dead, {eids[0]})

    # -- the agent's note list ---------------------------------------------------------------

    def test_the_agent_note_list_leaves_the_skill_note_out(self):
        out = LIST_NOTE_FOLDERS.run(
            {}, ToolContext(team_id=self.team_id, user_id=str(self.user.id))
        )
        listed = {n["note_id"] for n in out["top_level_notes"]}
        for folder in out["folders"]:
            listed |= {n["note_id"] for n in folder["notes"]}
        self.assertEqual(listed, {self.plain.note_id, self.filed.note_id})
        self.assertNotIn("Weekly update", str(out))
        self.assertEqual([f["name"] for f in out["folders"]], ["Ideas"])

    # -- Recent notes ------------------------------------------------------------------------

    def record(self, note):
        self.as_user(self.user)
        return self.client.post(
            RECENT,
            {
                "team_id": self.team_id,
                "user_id": str(self.user.id),
                "note_id": note.note_id,
                "note_type": 1,
            },
            format="json",
        )

    def recent_titles(self):
        res = self.client.get(f"{RECENT_META}?team_id={self.team_id}&user_id={self.user.id}")
        self.assertEqual(res.status_code, 200)
        return [n["title"] for n in res.json()["personalNotes"]]

    def test_opening_the_skill_note_is_not_recorded(self):
        res = self.record(self.skill_note)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(
            NoteRecentMaster.objects.filter(note_id=self.skill_note.note_id, note_type=1).exists()
        )
        self.assertEqual(self.recent_titles(), [])

    def test_a_normal_note_is_still_recorded_and_listed(self):
        res = self.record(self.plain)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["noteId"], self.plain.note_id)
        self.assertTrue(
            NoteRecentMaster.objects.filter(note_id=self.plain.note_id, note_type=1).exists()
        )
        self.assertEqual(self.recent_titles(), ["Plain note"])

    def test_a_skill_note_recorded_before_the_rule_is_not_listed_and_takes_no_slot(self):
        NoteRecentMaster.objects.create(
            team=self.team, user=self.user, note_type=1, note_id=self.skill_note.note_id
        )
        self.record(self.plain)
        self.assertEqual(self.recent_titles(), ["Plain note"])

    def test_the_skills_editor_still_opens_and_saves_the_body(self):
        self.as_user(self.user)
        res = self.client.get(test_skills_api.detail(self.skill["id"]))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn("Read the open tasks", res.json()["skill"]["bodyMarkdown"])
        self.assertEqual(res.json()["skill"]["noteId"], self.skill_note.note_id)
