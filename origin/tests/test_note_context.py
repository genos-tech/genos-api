"""MIP-839: a note's index entry knows what contains it.

`note_context` builds, per note, the container names (`context_text`, the
searched copy; `context_path`, the shown one) and `context_acl`, the people
who may see them. The chunker attaches it and re-emits a note when a
container is renamed or moved.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.chunkers.note_chunker import (
    iter_chat_note_chunks,
    iter_personal_note_chunks,
    iter_task_note_chunks,
)
from origin.search_engine.note_context import (
    chat_note_contexts,
    personal_note_contexts,
    task_note_contexts,
)
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER

from .test_base import BaseAPITestCase


class PersonalNotesBase(BaseAPITestCase):
    def _folder(self, name, *, parent=None, scope="personal", visibility=None, owner=None):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=owner or self.user,
            name=name,
            parent_folder_id=parent.folder_id if parent else None,
            scope=scope,
            visibility=visibility,
        )
        if scope == "team":
            NoteFolderPermission.objects.create(
                team=self.team, folder=folder, user=folder.owner, role_id=ROLE_OWNER
            )
        return folder

    def _note(self, title="n", folder=None, parent=None):
        return PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title=title,
            body=[],
            folder_id=folder.folder_id if folder else None,
            parent_note_id=parent.note_id if parent else None,
        )


class PersonalNoteContextTests(PersonalNotesBase):
    def test_my_notes_chain_is_root_first_and_private_to_the_owner(self):
        specs = self._folder("Specs", parent=self._folder("Design"))
        note = self._note("Roadmap", folder=specs)
        ctx = personal_note_contexts([note])[note.note_id]
        self.assertEqual(ctx.path, ["Design", "Specs"])
        self.assertEqual(ctx.text, "Design Specs")
        self.assertEqual(ctx.acl, [str(self.user.id)])

    def test_a_top_level_note_has_no_context(self):
        self.assertEqual(personal_note_contexts([self._note("Loose")]), {})

    def test_a_child_note_sits_in_its_parents_folder(self):
        folder = self._folder("Specs")
        parent = self._note("Parent", folder=folder)
        child = self._note("Child", parent=parent)
        ctx = personal_note_contexts([child])
        self.assertEqual(ctx[child.note_id].path, ["Specs"])

    def test_a_broken_chain_gives_nothing_rather_than_a_partial_path(self):
        folder = self._folder("Orphan")
        PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).update(
            parent_folder_id=987654
        )
        self.assertEqual(personal_note_contexts([self._note("n", folder=folder)]), {})

    def test_a_public_team_chain_is_the_team_sentinel(self):
        handbook = self._folder("Handbook", scope="team", visibility="public")
        note = self._note("Guide", folder=handbook)
        ctx = personal_note_contexts([note])[note.note_id]
        self.assertEqual(ctx.path, ["Handbook"])
        self.assertIn(f"team:{self.team.team_id}", ctx.acl)

    def test_a_private_subfolder_narrows_a_public_parent_to_its_members(self):
        parent = self._folder("Company", scope="team", visibility="public")
        sub = self._folder("Board", parent=parent, scope="team", visibility="private")
        NoteFolderPermission.objects.create(
            team=self.team, folder=sub, user=self.user2, role_id=ROLE_EDITOR
        )
        note = self._note("Minutes", folder=sub)
        acl = personal_note_contexts([note])[note.note_id].acl
        self.assertEqual(set(acl), {str(self.user.id), str(self.user2.id)})
        self.assertNotIn(f"team:{self.team.team_id}", acl)

    def test_a_grant_on_a_subfolder_alone_does_not_reveal_the_parents_name(self):
        # user2 may open "Specs" but not "Design" above it: they must not
        # find the note by, or read, the name "Design".
        parent = self._folder("Design", scope="team", visibility="private")
        sub = self._folder("Specs", parent=parent, scope="team", visibility="private")
        NoteFolderPermission.objects.create(
            team=self.team, folder=sub, user=self.user2, role_id=ROLE_EDITOR
        )
        note = self._note("Roadmap", folder=sub)
        ctx = personal_note_contexts([note])[note.note_id]
        self.assertEqual(ctx.path, ["Design", "Specs"])
        self.assertEqual(ctx.acl, [str(self.user.id)])

    def test_the_chunker_attaches_it_and_sorts_the_acl(self):
        folder = self._folder("Specs")
        self._note("Roadmap", folder=folder)
        chunks = [c for b in iter_personal_note_chunks() for c in b.chunks]
        self.assertEqual({c.context_path and tuple(c.context_path) for c in chunks}, {("Specs",)})
        self.assertEqual({c.context_text for c in chunks}, {"Specs"})
        self.assertEqual({tuple(c.context_acl) for c in chunks}, {(str(self.user.id),)})

    def test_a_note_without_context_carries_none(self):
        self._note("Loose")
        chunk = next(c for b in iter_personal_note_chunks() for c in b.chunks)
        self.assertIsNone(chunk.context_path)
        self.assertNotIn("context_acl", chunk.to_dict())


class PersonalRefreshWindowTests(PersonalNotesBase):
    """A renamed or moved folder re-emits every note beneath it."""

    def setUp(self):
        super().setUp()
        self.top = self._folder("Design")
        self.sub = self._folder("Specs", parent=self.top)
        self.deep = self._note("Deep", folder=self.sub)
        self.child = self._note("Child", parent=self.deep)
        self.other = self._note("Elsewhere", folder=self._folder("Other"))
        self.loose = self._note("Loose")
        old = timezone.now() - timedelta(days=2)
        PersonalNoteMaster.objects.update(ts_updated_at=old)
        PersonalNoteFolder.objects.update(ts_updated_at=old)
        self.since = timezone.now() - timedelta(minutes=1)

    def _emitted(self):
        return {b.entity_id for b in iter_personal_note_chunks(since=self.since)}

    def test_nothing_changed_nothing_emitted(self):
        self.assertEqual(self._emitted(), set())

    def test_renaming_a_folder_reaches_subfolders_notes_and_child_notes(self):
        PersonalNoteFolder.objects.filter(folder_id=self.top.folder_id).update(
            name="Product", ts_updated_at=timezone.now()
        )
        self.assertEqual(
            self._emitted(),
            {f"note:personal:{self.deep.note_id}", f"note:personal:{self.child.note_id}"},
        )

    def test_the_new_name_is_what_gets_indexed(self):
        PersonalNoteFolder.objects.filter(folder_id=self.top.folder_id).update(
            name="Product", ts_updated_at=timezone.now()
        )
        chunks = [
            c
            for b in iter_personal_note_chunks(since=self.since)
            if b.entity_id == f"note:personal:{self.deep.note_id}"
            for c in b.chunks
        ]
        self.assertEqual({tuple(c.context_path) for c in chunks}, {("Product", "Specs")})

    def test_a_single_note_refresh_by_id(self):
        ids = {b.entity_id for b in iter_personal_note_chunks(note_ids=[self.other.note_id])}
        self.assertEqual(ids, {f"note:personal:{self.other.note_id}"})


class TaskNoteContextTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Genos", code="GEN", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.backing = TaskMaster.objects.create(
            team=self.team, project=self.project, title="M3 backing", is_milestone=True
        )
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="Launch", task=self.backing
        )
        self.parent = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Search epic",
            milestone=self.milestone,
            parent_task_id=self.backing.task_id,
        )
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Search by id",
            milestone=self.milestone,
            parent_task_id=self.parent.task_id,
        )
        self.task.refresh_from_db()

    def _note(self, task=None, title="Plan"):
        return TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            owner=self.user,
            task=task,
            title=title,
            body=[],
        )

    def test_path_runs_project_milestone_parent_task(self):
        note = self._note(self.task)
        ctx = task_note_contexts([note])[note.note_id]
        # The milestone's own backing task is the milestone, not a folder.
        self.assertEqual(ctx.path, ["Genos", "Launch", "Search epic", "Search by id"])
        self.assertIn(f"GEN-{self.task.project_task_number}", ctx.text)
        self.assertEqual(ctx.acl, [str(self.user.id)])

    def test_a_note_on_a_milestone_stops_at_the_milestone(self):
        note = self._note(self.backing)
        self.assertEqual(task_note_contexts([note])[note.note_id].path, ["Genos", "Launch"])

    def test_a_note_on_the_project_is_just_the_project(self):
        note = self._note(None)
        self.assertEqual(task_note_contexts([note])[note.note_id].path, ["Genos"])

    def test_only_project_members_may_see_the_names(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        note = self._note(self.task)
        acl = set(task_note_contexts([note])[note.note_id].acl)
        self.assertEqual(acl, {str(self.user.id), str(self.user2.id)})

    def test_renames_re_emit_the_notes_beneath(self):
        note = self._note(self.task)
        sub_note = self._note(self.task, "Sub")
        far = self._note(None, "Project-level")
        old = timezone.now() - timedelta(days=2)
        TaskNoteMaster.objects.update(ts_updated_at=old)
        TaskMaster.objects.update(ts_updated_at=old)
        MilestoneMaster.objects.update(ts_updated_at=old)
        ProjectMaster.objects.update(ts_updated_at=old)
        since = timezone.now() - timedelta(minutes=1)

        def emitted():
            return {b.entity_id for b in iter_task_note_chunks(since=since)}

        self.assertEqual(emitted(), set())
        # A parent task renamed: its sub-task's note path changed.
        TaskMaster.objects.filter(task_id=self.parent.task_id).update(
            title="Search epic v2", ts_updated_at=timezone.now()
        )
        self.assertEqual(emitted(), {f"note:task:{note.note_id}", f"note:task:{sub_note.note_id}"})
        TaskMaster.objects.update(ts_updated_at=old)
        # A milestone renamed.
        MilestoneMaster.objects.filter(milestone_id=self.milestone.milestone_id).update(
            title="Launch 2", ts_updated_at=timezone.now()
        )
        self.assertEqual(emitted(), {f"note:task:{note.note_id}", f"note:task:{sub_note.note_id}"})
        MilestoneMaster.objects.update(ts_updated_at=old)
        # The project renamed: every note in it, project-level ones too.
        ProjectMaster.objects.filter(project_id=self.project.project_id).update(
            project_name="Genos 2", ts_updated_at=timezone.now()
        )
        self.assertEqual(
            emitted(),
            {f"note:task:{n.note_id}" for n in (note, sub_note, far)},
        )


class ChatNoteContextTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.carol = get_user_model().objects.create_user(
            username="carol", email="carol@example.com", password="x"
        )

    def _note(self, channel):
        return ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=int(channel.kind),
            channel=channel,
            is_thread=False,
            title="Decisions",
            body=[],
        )

    def test_a_group_chat_is_named_by_its_title(self):
        gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Design", owner=self.user
        )
        note = self._note(gm)
        ctx = chat_note_contexts([note], {gm.id: {str(self.user.id)}})[note.note_id]
        self.assertEqual((ctx.path, ctx.text, ctx.acl), (["Design"], "Design", [str(self.user.id)]))

    def test_a_dm_is_found_by_its_members_names(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        for u in (self.user, self.carol):
            ChannelMember.objects.create(channel=dm, user=u, role="member")
        note = self._note(dm)
        ctx = chat_note_contexts([note], {dm.id: {str(self.user.id), str(self.carol.id)}})[
            note.note_id
        ]
        self.assertEqual(set(ctx.text.split()), {"testuser", "carol"})
        self.assertEqual(len(ctx.acl), 2)

    def test_a_project_chat_is_named_by_its_project(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="Genos", code="GEN", owner=self.user
        )
        # Creating a project already makes its PM channel.
        pm = Channel.objects.get(project=project, kind=ChannelKind.PM)
        note = self._note(pm)
        self.assertEqual(chat_note_contexts([note], {})[note.note_id].path, ["Genos"])

    def test_renaming_the_channel_re_emits_its_notes(self):
        gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Design", owner=self.user
        )
        other = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Ops", owner=self.user
        )
        mine, theirs = self._note(gm), self._note(other)
        old = timezone.now() - timedelta(days=2)
        ChatNoteMaster.objects.update(ts_updated_at=old)
        Channel.objects.update(ts_updated_at=old)
        since = timezone.now() - timedelta(minutes=1)
        self.assertEqual({b.entity_id for b in iter_chat_note_chunks(since=since)}, set())
        Channel.objects.filter(id=gm.id).update(
            title="Product design", ts_updated_at=timezone.now()
        )
        self.assertEqual(
            {b.entity_id for b in iter_chat_note_chunks(since=since)},
            {f"note:chat:{mine.note_id}"},
        )
        del theirs


class MoveRefreshTests(PersonalNotesBase):
    """A folder move changes a note's context but not its `ts_updated_at`,
    so the incremental window can't see it: the move re-indexes the note
    (and its child notes) itself."""

    def _move(self, note, folder):
        self.authenticate()
        return self.client.put(
            "/api/v2/note/personal/move/",
            {
                "team_id": self.team.team_id,
                "user_id": str(self.user.id),
                "note_id": note.note_id,
                "folder_id": folder.folder_id if folder else None,
            },
            format="json",
        )

    def test_a_move_refreshes_the_note_and_its_children(self):
        folder = self._folder("Specs")
        parent = self._note("Parent")
        child = self._note("Child", parent=parent)
        with mock.patch("origin.views.utils.personal_note_move.refresh_personal_notes") as refresh:
            self.assertEqual(self._move(parent, folder).status_code, 200)
        refresh.assert_called_once()
        self.assertEqual(set(refresh.call_args.args[0]), {parent.note_id, child.note_id})

    def test_a_refused_move_refreshes_nothing(self):
        folder = self._folder("Theirs", owner=self.user2)
        note = self._note("Mine")
        with mock.patch("origin.views.utils.personal_note_move.refresh_personal_notes") as refresh:
            self.assertEqual(self._move(note, folder).status_code, 400)
        refresh.assert_not_called()

    def test_the_refreshed_entry_carries_the_new_path_and_none_after_leaving(self):
        folder = self._folder("Specs")
        note = self._note("Roadmap")
        self.assertEqual(self._move(note, folder).status_code, 200)

        def path():
            chunk = next(
                c for b in iter_personal_note_chunks(note_ids=[note.note_id]) for c in b.chunks
            )
            return chunk.context_path

        self.assertEqual(path(), ["Specs"])
        self.assertEqual(self._move(note, None).status_code, 200)
        self.assertIsNone(path())
