"""Tests for the note move endpoints:

  - PUT /api/v2/note/personal/move/  (PersonalNoteMoveView)
  - PUT /api/v2/note/task/move/      (TaskNoteMoveView)
  - PUT /api/v2/note/chat/move/      (ChatNoteMoveView)

Key contracts under test: owner-only personal moves (an editor grant is
NOT enough to rearrange the owner's folders), re-rooting on folder move,
target validation, the descendant cascade on task/chat re-anchors, the
explicit `ts_updated_at` bump on cascaded rows (what routes them into
the incremental OpenSearch reindex window so ACLs refresh), meta
`folderId` exposure, and thread-anchor clearing on chat moves.
"""

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

PERSONAL_MOVE_URL = "/api/v2/note/personal/move/"
TASK_MOVE_URL = "/api/v2/note/task/move/"
CHAT_MOVE_URL = "/api/v2/note/chat/move/"
PERSONAL_META_URL = "/api/v2/note/personal/meta/"
PERSONAL_NOTE_URL = "/api/v2/note/personal/"


class PersonalNoteMoveViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name="Work"
        )

    def _params(self):
        return {"team_id": self.team.team_id, "user_id": str(self.user.id)}

    def _create_note(self, title="n", parent_note_id=None, owner=None):
        return PersonalNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.user,
            title=title,
            body=[],
            parent_note_id=parent_note_id,
        )

    def test_move_into_folder_re_roots_and_keeps_ts(self):
        parent = self._create_note("parent")
        child = self._create_note("child", parent_note_id=parent.note_id)
        ts_before = child.ts_updated_at

        res = self.client.put(
            PERSONAL_MOVE_URL,
            {**self._params(), "note_id": child.note_id, "folder_id": self.folder.folder_id},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        child.refresh_from_db()
        self.assertEqual(child.folder_id, self.folder.folder_id)
        # Folders own ROOT notes — a nested child is re-rooted on move.
        self.assertIsNone(child.parent_note_id)
        # .update() skips auto_now: no sidebar reshuffle, no re-embed.
        self.assertEqual(child.ts_updated_at, ts_before)

    def test_move_to_root_with_explicit_null(self):
        note = self._create_note("in-folder")
        PersonalNoteMaster.objects.filter(note_id=note.note_id).update(
            folder_id=self.folder.folder_id
        )

        res = self.client.put(
            PERSONAL_MOVE_URL,
            {**self._params(), "note_id": note.note_id, "folder_id": None},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        note.refresh_from_db()
        self.assertIsNone(note.folder_id)

    def test_missing_folder_id_key_is_400(self):
        note = self._create_note()
        res = self.client.put(
            PERSONAL_MOVE_URL,
            {**self._params(), "note_id": note.note_id},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_move_into_foreign_folder_rejected(self):
        theirs = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user2, name="theirs"
        )
        note = self._create_note()
        res = self.client.put(
            PERSONAL_MOVE_URL,
            {**self._params(), "note_id": note.note_id, "folder_id": theirs.folder_id},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_editor_grant_cannot_move_owners_note(self):
        note = self._create_note("owned-by-user1")
        # user2 gets an explicit editor grant — enough to edit the BODY,
        # not to rearrange the owner's sidebar.
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user2, note_type=1, note_id=note.note_id, role_id=2
        )
        folder2 = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user2, name="user2 folder"
        )
        self.authenticate(self.user2)
        res = self.client.put(
            PERSONAL_MOVE_URL,
            {
                "team_id": self.team.team_id,
                "user_id": str(self.user2.id),
                "note_id": note.note_id,
                "folder_id": folder2.folder_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_meta_contains_folder_id_and_post_accepts_folder(self):
        res = self.client.post(
            PERSONAL_NOTE_URL,
            {
                **self._params(),
                "title": "filed note",
                "body": [],
                "folder_id": self.folder.folder_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["folderId"], self.folder.folder_id)

        meta = self.client.get(PERSONAL_META_URL, self._params())
        self.assertEqual(meta.status_code, 200)
        row = next(r for r in meta.data if r["noteId"] == res.data["noteId"])
        self.assertEqual(row["folderId"], self.folder.folder_id)


class TaskNoteMoveViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

        self.project_a = ProjectMaster.objects.create(
            team=self.team, project_name="Proj A", owner=self.user
        )
        self.project_b = ProjectMaster.objects.create(
            team=self.team, project_name="Proj B", owner=self.user
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.project_a, attendee=self.user
        )
        self.task_a = TaskMaster.objects.create(
            team=self.team, project=self.project_a, title="task A", status="Open"
        )
        self.task_b = TaskMaster.objects.create(
            team=self.team, project=self.project_b, title="task B", status="Open"
        )

        # 3-level note chain anchored on task A: root -> mid -> leaf.
        self.root = self._create_note("root", self.task_a)
        self.mid = self._create_note("mid", self.task_a, parent_note_id=self.root.note_id)
        self.leaf = self._create_note("leaf", self.task_a, parent_note_id=self.mid.note_id)

    def _create_note(self, title, task, parent_note_id=None, owner=None):
        return TaskNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.user,
            project=task.project,
            task=task,
            title=title,
            body=[],
            parent_note_id=parent_note_id,
        )

    def _params(self):
        return {"team_id": self.team.team_id, "user_id": str(self.user.id)}

    def test_move_updates_task_derived_project_and_cascades(self):
        # user is a member of project B too.
        ProjectMembers.objects.create(
            team=self.team, project=self.project_b, attendee=self.user
        )
        ts_before = self.leaf.ts_updated_at

        res = self.client.put(
            TASK_MOVE_URL,
            {**self._params(), "note_id": self.root.note_id, "task_id": self.task_b.task_id},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["taskId"], self.task_b.task_id)
        self.assertEqual(res.data["projectId"], self.project_b.project_id)
        # Enriched hierarchy fields ride along (shared _task_hierarchy_fields).
        self.assertEqual(res.data["taskTitle"], "task B")
        self.assertEqual(res.data["projectName"], "Proj B")

        for note in (self.root, self.mid, self.leaf):
            note.refresh_from_db()
            self.assertEqual(note.task_id, self.task_b.task_id)
            # Project always derived from the target task.
            self.assertEqual(note.project_id, self.project_b.project_id)

        # Cascaded rows MUST get a ts bump — that's what routes them
        # into the incremental reindex window so their OpenSearch ACL
        # switches to project B's members.
        self.assertGreater(self.leaf.ts_updated_at, ts_before)

    def test_non_member_of_target_project_403(self):
        # user is NOT a member of project B.
        res = self.client.put(
            TASK_MOVE_URL,
            {**self._params(), "note_id": self.root.note_id, "task_id": self.task_b.task_id},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_missing_target_task_404(self):
        res = self.client.put(
            TASK_MOVE_URL,
            {**self._params(), "note_id": self.root.note_id, "task_id": 999999},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_no_write_role_403(self):
        # user2 has no project membership and no explicit grant.
        self.authenticate(self.user2)
        res = self.client.put(
            TASK_MOVE_URL,
            {
                "team_id": self.team.team_id,
                "user_id": str(self.user2.id),
                "note_id": self.root.note_id,
                "task_id": self.task_b.task_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)


class ChatNoteMoveViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

        self.channel_a = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="GM A", owner=self.user
        )
        self.channel_b = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="GM B", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel_a, user=self.user, role="owner")

        self.root = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=2,
            channel=self.channel_a,
            is_thread=True,
            thread_root_id="00000000-0000-0000-0000-000000000001",
            title="root",
            body=[],
        )
        self.child = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=2,
            channel=self.channel_a,
            is_thread=True,
            thread_root_id="00000000-0000-0000-0000-000000000001",
            title="child",
            body=[],
            parent_note_id=self.root.note_id,
        )
        # ORM-created notes bypass the POST that grants the owner role;
        # chat members only get implicit VIEWER, so the move's
        # require_write_role needs this explicit owner row.
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_type=3,
            note_id=self.root.note_id,
            role_id=1,
        )

    def _params(self):
        return {"team_id": self.team.team_id, "user_id": str(self.user.id)}

    def test_move_repoints_channel_clears_thread_and_cascades(self):
        ChannelMember.objects.create(channel=self.channel_b, user=self.user, role="owner")
        ts_before = self.child.ts_updated_at

        res = self.client.put(
            CHAT_MOVE_URL,
            {
                **self._params(),
                "note_id": self.root.note_id,
                "chat_type": 2,
                "channel_id": str(self.channel_b.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["chatId"], str(self.channel_b.id))
        self.assertFalse(res.data["isThread"])
        self.assertIsNone(res.data["threadId"])

        for note in (self.root, self.child):
            note.refresh_from_db()
            self.assertEqual(note.channel_id, self.channel_b.id)
            # Thread anchoring cleared — the old thread root lives in
            # the old channel.
            self.assertFalse(note.is_thread)
            self.assertIsNone(note.thread_root_id)

        # Cascade ts bump (incremental-reindex ACL refresh).
        self.assertGreater(self.child.ts_updated_at, ts_before)

    def test_non_member_of_target_channel_403(self):
        res = self.client.put(
            CHAT_MOVE_URL,
            {
                **self._params(),
                "note_id": self.root.note_id,
                "chat_type": 2,
                "channel_id": str(self.channel_b.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_missing_target_channel_404(self):
        ChannelMember.objects.create(channel=self.channel_b, user=self.user, role="owner")
        res = self.client.put(
            CHAT_MOVE_URL,
            {
                **self._params(),
                "note_id": self.root.note_id,
                "chat_type": 2,
                "channel_id": "00000000-0000-0000-0000-00000000dead",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
