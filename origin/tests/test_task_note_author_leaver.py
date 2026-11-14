"""MIP-1511: a TASK note's author who left the project loses the note on every surface, like a
chat note's author since MIP-1492; the author who is still on the project keeps everything.

`TaskNoteMaster.owner` used to be admitted as "the note's own ACL" by the agent's `may_read_note`,
the task-note ACL set (`task_note_acl_user_ids`: `get_note`, source visibility, mentions,
citations, note summaries), the OpenSearch chunk ACL, `has_write_permission`, the timeline's
`readable_notes`, the briefing's "edits to your notes", the comment-notification audience and the
note-access-request router. REST has refused the leaver since MIP-1493 (the Owner row ends with
the membership), so the agent / MCP / briefing / search surfaces still opened what the app did not.

Re-joining restores the Owner row on the notes whose `owner_id` is the person, once, never
overwriting a row; a SHARE a leaver holds stays; nothing about My Notes (personal notes) changes.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.common.team_models import TeamMembers
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.search_engine.agent.acl import may_read_note, task_note_acl_user_ids
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.note_access import has_write_permission
from origin.search_engine.chunkers.note_chunker import iter_task_note_chunks
from origin.services import briefing, timeline
from origin.services.note_role_revoke import restore_task_author_roles
from origin.services.v3_activity import SURFACE_TASK_NOTE, _comment_recipients
from origin.tests.test_chat_note_one_rule import ChatNoteFixtures
from origin.views.utils.note_role import (
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_VIEWER,
    get_effective_role,
    get_explicit_role,
)

User = get_user_model()
TASK = NOTE_TYPE_TASK
SECRET = "secret task body"


class Fixtures(ChatNoteFixtures):
    """`self.reader` is the author under test, `self.owner` is another project member."""

    def setUp(self):
        super().setUp()
        purge = mock.patch("origin.search_engine.purge.purge_entities")
        self.purge = purge.start()
        self.addCleanup(purge.stop)
        self.project = self.project_of("TNA", self.reader)
        self.note = self.task_note(self.reader)

    def task_note(self, author, project=None, title="Task note"):
        """What `POST /api/v2/note/task/` writes: the note and the author's Owner row."""
        project = project or self.project
        note = TaskNoteMaster.objects.create(
            team=self.team,
            owner=author,
            project=project,
            title=title,
            body=[{"type": "paragraph", "content": [{"type": "text", "text": SECRET}]}],
        )
        NotePermissionMaster.objects.create(
            team=self.team, user=author, note_type=TASK, note_id=note.note_id, role_id=ROLE_OWNER
        )
        return note

    def leave(self, user=None, project=None):
        ProjectMembers.objects.filter(
            project=project or self.project, attendee=user or self.reader
        ).delete()

    def rejoin(self, user=None, project=None):
        return ProjectMembers.objects.create(
            team=self.team, project=project or self.project, attendee=user or self.reader
        )

    def mcp_get_note(self, note_id):
        from origin.models.common.api_key_models import SCOPE_READ

        self._key(scope=SCOPE_READ, user=self.reader)
        return self._tool_text(self._call("get_note", {"note_type": "task", "note_id": note_id}))


class Surfaces(Fixtures):
    def verdicts(self, note, user=None) -> dict[str, bool]:
        """What every gate that used to admit the author by `owner_id` says, for `user`."""
        user = user or self.reader
        uid = str(user.id)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=uid)
        in_chunks = any(
            uid in c.acl_user_ids
            for batch in iter_task_note_chunks()
            if batch.entity_id == f"note:task:{note.note_id}"
            for c in batch.chunks
        )
        return {
            "may_read_note": may_read_note(
                user_id=uid, note_type_code=TASK, note_id=note.note_id, owner_id=note.owner_id
            ),
            "task_note_acl_user_ids": uid
            in task_note_acl_user_ids(
                owner_id=note.owner_id, project_id=note.project_id, note_id=note.note_id
            ),
            "chunk acl": in_chunks,
            "has_write_permission": has_write_permission(
                owner_id=note.owner_id, note_id=note.note_id, note_type_code=TASK, ctx=ctx
            ),
            "timeline": (TASK, note.note_id)
            in timeline.readable_notes(ctx, [(TASK, note.note_id)]),
            "briefing _may_read_note": briefing._may_read_note(ctx, TASK, note),
            "briefing _changed_notes": self.hears_of_edits(note, ctx),
            "comment audience": uid
            in _comment_recipients(SURFACE_TASK_NOTE, {"noteId": note.note_id}),
        }

    def hears_of_edits(self, note, ctx) -> bool:
        """Does the author's briefing list another person's edit of the note?"""
        NoteVersionMaster.objects.filter(note_type=TASK, note_id=note.note_id).delete()
        NoteVersionMaster.objects.create(
            team=self.team,
            note_type=TASK,
            note_id=note.note_id,
            version_no=1,
            title=note.title,
            body=note.body,
            editor=self.owner,
        )
        now = timezone.now()
        win = briefing.Window(
            name="t", since=now - timedelta(days=1), until=now + timedelta(days=1)
        )
        found = briefing._changed_notes(ctx, win, briefing._Names(ctx))
        return any(i["note_id"] == note.note_id and i["note_type"] == "task" for i in found)

    def assertAll(self, note, expected: bool, user=None):
        got = self.verdicts(note, user)
        self.assertEqual(got, {k: expected for k in got}, got)

    def assertMcpGetNote(self, note, expected: bool):
        text, is_error = self.mcp_get_note(note.note_id)
        self.assertEqual(not is_error and SECRET in text, expected, text)


class AuthorStillOnTheProject(Surfaces):
    def test_the_author_who_is_a_member_passes_everywhere(self):
        self.assertAll(self.note, True)
        self.assertMcpGetNote(self.note, True)

    def test_the_author_with_no_owner_row_but_a_membership_passes_everywhere(self):
        """A legacy note from before role rows existed: membership alone carries the author."""
        NotePermissionMaster.objects.filter(note_type=TASK, note_id=self.note.note_id).delete()
        self.assertAll(self.note, True)

    def test_another_member_passes_after_the_author_left(self):
        self.leave()
        got = self.verdicts(self.note, self.owner)
        got.pop("briefing _changed_notes")  # "edits to MY notes": the note is not theirs
        got.pop("comment audience")  # asserted on its own below
        self.assertEqual(got, {k: True for k in got}, got)
        self.assertIn(
            str(self.owner.id),
            _comment_recipients(SURFACE_TASK_NOTE, {"noteId": self.note.note_id}),
        )


class AuthorWhoLeftTheProject(Surfaces):
    def test_the_leaver_is_refused_on_every_gate(self):
        self.leave()
        self.assertAll(self.note, False)
        self.assertMcpGetNote(self.note, False)

    def test_a_leaver_whose_owner_row_survived_reads_through_that_row_like_the_app(self):
        """A row written before MIP-1493 (it never ended) is a grant: every surface honours it
        the way `get_effective_role` does, so the agent and the app agree."""
        self.leave()
        NotePermissionMaster.objects.update_or_create(
            team=self.team,
            user=self.reader,
            note_type=TASK,
            note_id=self.note.note_id,
            defaults={"role_id": ROLE_OWNER},
        )
        # an Owner row is a grant: it opens the note through `get_effective_role`
        self.assertTrue(get_effective_role(self.reader.id, TASK, self.note.note_id) is not None)
        # ... which is exactly what the app does, and the agent now agrees (set rule too)
        got = self.verdicts(self.note)
        self.assertTrue(got["may_read_note"], got)
        self.assertTrue(got["task_note_acl_user_ids"], got)

    def test_a_share_the_leaver_holds_still_opens_it(self):
        NotePermissionMaster.objects.filter(note_type=TASK, note_id=self.note.note_id).delete()
        self.leave()
        NotePermissionMaster.objects.create(
            team=self.team, user=self.reader, note_type=TASK, note_id=self.note.note_id, role_id=3
        )
        got = self.verdicts(self.note)
        self.assertFalse(got.pop("has_write_permission"))  # a Viewer share writes nowhere
        for gate in (
            "may_read_note",
            "task_note_acl_user_ids",
            "chunk acl",
            "timeline",
            "briefing _may_read_note",
            "comment audience",
        ):
            self.assertTrue(got[gate], gate)
        self.assertMcpGetNote(self.note, True)

    def test_a_note_in_another_project_the_author_is_still_on_passes(self):
        other = self.project_of("TNB", self.reader)
        mine = self.task_note(self.reader, other)
        self.leave()
        self.assertAll(mine, True)
        self.assertAll(self.note, False)

    def test_the_access_request_of_a_stranger_has_nobody_to_ask_after_the_author_left(self):
        from origin.views.common.inbox_views import _note_and_owner

        self.leave()
        # no Owner row left, and the FK owner is no longer on the project: nobody to ask
        _note, owner_id = _note_and_owner(TASK, self.note.note_id)
        self.assertIsNone(owner_id)

    def test_the_access_request_of_a_legacy_note_still_reaches_its_member_author(self):
        from origin.views.common.inbox_views import _note_and_owner

        NotePermissionMaster.objects.filter(note_type=TASK, note_id=self.note.note_id).delete()
        _note, owner_id = _note_and_owner(TASK, self.note.note_id)
        self.assertEqual(str(owner_id), str(self.reader.id))


class BriefingEditsOfAHeldTaskNote(Surfaces):
    def test_a_leaver_who_still_holds_the_task_does_not_hear_of_edits_to_their_old_note(self):
        """`_changed_notes` also lists the task notes of tasks the caller holds: the author who
        left the project but is still the task's assignee must not get the note through the
        `foreign` check's author exclusion."""
        from origin.models.task.task_models import TaskMaster

        task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.owner,
            assignee=self.reader,
            title="held",
            status="Open",
        )
        self.note.task = task
        self.note.save()
        self.assertTrue(self.verdicts(self.note)["briefing _changed_notes"])
        self.leave()
        self.assertFalse(self.verdicts(self.note)["briefing _changed_notes"])


class QueryCountsDoNotGrow(Surfaces):
    def test_the_briefing_rule_costs_what_it_did_for_an_author_who_is_a_member(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.reader.id))
        with CaptureQueriesContext(connection) as ctx_q:
            briefing._may_read_note(ctx, TASK, self.note)
        # the set rule: members + grants (the owner is one of the members now, not a free pass)
        self.assertLessEqual(len(ctx_q), 2, [q["sql"] for q in ctx_q.captured_queries])


class RejoiningTheProject(Fixtures):
    def test_the_leaver_who_comes_back_gets_their_owner_row_back_once(self):
        self.leave()
        self.assertIsNone(get_explicit_role(self.reader.id, TASK, self.note.note_id))
        self.rejoin()
        self.assertEqual(get_explicit_role(self.reader.id, TASK, self.note.note_id), ROLE_OWNER)
        self.assertEqual(
            NotePermissionMaster.objects.filter(
                user=self.reader, note_type=TASK, note_id=self.note.note_id
            ).count(),
            1,
        )

    def test_the_restored_owner_can_share_and_unshare_again(self):
        from origin.services import note_sharing

        self.leave()
        self.rejoin()
        peer = User.objects.create_user(username="peer", email="peer@example.com", password="x")
        TeamMembers.objects.create(team=self.team, attendee=peer)
        note_sharing.share_note(
            team_id=self.team.team_id,
            granter_id=self.reader.id,
            note_type=TASK,
            note_id=self.note.note_id,
            target_user_id=peer.id,
            role_id=ROLE_VIEWER,
        )
        self.assertEqual(get_explicit_role(peer.id, TASK, self.note.note_id), ROLE_VIEWER)

    def test_running_the_restore_again_changes_nothing(self):
        self.leave()
        self.rejoin()
        before = list(
            NotePermissionMaster.objects.order_by("pk").values_list("pk", "ts_updated_at")
        )
        self.assertEqual(
            restore_task_author_roles(user_id=self.reader.id, project_id=self.project.project_id), 0
        )
        self.assertEqual(
            before,
            list(NotePermissionMaster.objects.order_by("pk").values_list("pk", "ts_updated_at")),
        )

    def test_a_row_that_exists_is_never_overwritten(self):
        """The leaver was handed an Editor share while away: re-joining keeps it, not Owner."""
        NotePermissionMaster.objects.filter(note_type=TASK, note_id=self.note.note_id).delete()
        self.leave()
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.reader,
            note_type=TASK,
            note_id=self.note.note_id,
            role_id=ROLE_EDITOR,
        )
        self.rejoin()
        self.assertEqual(get_explicit_role(self.reader.id, TASK, self.note.note_id), ROLE_EDITOR)

    def test_a_joiner_gets_nothing_on_notes_they_did_not_write(self):
        newcomer = User.objects.create_user(username="n", email="n@example.com", password="x")
        TeamMembers.objects.create(team=self.team, attendee=newcomer)
        self.rejoin(newcomer)
        self.assertIsNone(get_explicit_role(newcomer.id, TASK, self.note.note_id))

    def test_only_the_notes_of_that_project_come_back(self):
        other = self.project_of("TNC", self.reader)
        elsewhere = self.task_note(self.reader, other)
        self.leave()
        self.leave(project=other)
        self.rejoin()
        self.assertEqual(get_explicit_role(self.reader.id, TASK, self.note.note_id), ROLE_OWNER)
        self.assertIsNone(get_explicit_role(self.reader.id, TASK, elsewhere.note_id))

    def test_a_note_someone_else_wrote_is_not_restored_to_the_joiner(self):
        theirs = self.task_note(self.owner)
        NotePermissionMaster.objects.filter(note_type=TASK, note_id=theirs.note_id).delete()
        self.leave()
        self.rejoin()
        self.assertIsNone(get_explicit_role(self.reader.id, TASK, theirs.note_id))

    def test_saving_an_existing_membership_again_restores_nothing(self):
        member = ProjectMembers.objects.get(project=self.project, attendee=self.reader)
        NotePermissionMaster.objects.filter(note_type=TASK, note_id=self.note.note_id).delete()
        member.save()  # not a creation
        self.assertIsNone(get_explicit_role(self.reader.id, TASK, self.note.note_id))

    def test_the_restored_row_carries_the_notes_own_team(self):
        """The owner row is written with the note's own team."""
        self.leave()
        self.rejoin()
        row = NotePermissionMaster.objects.get(
            user=self.reader, note_type=TASK, note_id=self.note.note_id
        )
        self.assertEqual(row.team_id, self.note.team_id)


class PersonalNotesAreUnchanged(Fixtures):
    """(b) A leaver keeps their private notes and the shares they received; a team-folder author
    keeps Owner after losing the folder while they stay on the team."""

    def setUp(self):
        super().setUp()
        self.mine = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.reader, title="private", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.reader,
            note_type=NOTE_TYPE_PERSONAL,
            note_id=self.mine.note_id,
            role_id=ROLE_OWNER,
        )

    def ctx(self):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(self.reader.id))

    def test_a_project_leaver_keeps_their_my_notes_note(self):
        self.leave()
        self.assertTrue(
            may_read_note(
                user_id=str(self.reader.id),
                note_type_code=NOTE_TYPE_PERSONAL,
                note_id=self.mine.note_id,
                owner_id=self.mine.owner_id,
            )
        )
        self.assertTrue(
            has_write_permission(
                owner_id=self.mine.owner_id,
                note_id=self.mine.note_id,
                note_type_code=NOTE_TYPE_PERSONAL,
                ctx=self.ctx(),
            )
        )
        self.assertIn(
            (NOTE_TYPE_PERSONAL, self.mine.note_id),
            timeline.readable_notes(self.ctx(), [(NOTE_TYPE_PERSONAL, self.mine.note_id)]),
        )
        self.assertEqual(
            get_explicit_role(self.reader.id, NOTE_TYPE_PERSONAL, self.mine.note_id), ROLE_OWNER
        )

    def test_a_personal_share_received_survives_leaving_the_project(self):
        shared = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.owner, title="shared with me", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.reader,
            note_type=NOTE_TYPE_PERSONAL,
            note_id=shared.note_id,
            role_id=ROLE_VIEWER,
        )
        self.leave()
        self.assertIsNotNone(get_effective_role(self.reader.id, NOTE_TYPE_PERSONAL, shared.note_id))

    def test_a_team_folder_author_who_lost_the_folder_keeps_owner(self):
        folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.owner, name="Team", scope=PersonalNoteFolder.SCOPE_TEAM
        )
        grant = NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=self.reader, role_id=ROLE_EDITOR
        )
        note = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.reader,
            title="team note",
            body=[],
            folder_id=folder.folder_id,
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.reader,
            note_type=NOTE_TYPE_PERSONAL,
            note_id=note.note_id,
            role_id=ROLE_OWNER,
        )
        grant.delete()  # the folder access is gone; the person is still on the team
        self.assertEqual(
            get_explicit_role(self.reader.id, NOTE_TYPE_PERSONAL, note.note_id), ROLE_OWNER
        )
        self.assertTrue(
            may_read_note(
                user_id=str(self.reader.id),
                note_type_code=NOTE_TYPE_PERSONAL,
                note_id=note.note_id,
                owner_id=note.owner_id,
            )
        )

    def test_rejoining_a_project_touches_no_personal_note_row(self):
        self.leave()
        before = NotePermissionMaster.objects.filter(note_type=NOTE_TYPE_PERSONAL).count()
        self.rejoin()
        self.assertEqual(
            NotePermissionMaster.objects.filter(note_type=NOTE_TYPE_PERSONAL).count(), before
        )
