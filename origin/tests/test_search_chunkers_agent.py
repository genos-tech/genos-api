"""Tests for search_engine chunkers + agent ACL/citation-resolver.

Covers (deterministic chunk-boundary / ACL / citation assertions):

  * chunkers/base.py        — make_snippet / chat_entity_id / iso / Chunk.to_dict
  * chunkers/chat_chunker.py
  * chunkers/task_chunker.py
  * chunkers/todo_chunker.py
  * chunkers/thread_summary_chunker.py
  * chunkers/note_chunker.py
  * chunkers/note_summary_chunker.py
  * agent/acl.py
  * agent/citation_resolver.py

These modules are DB-driven transforms (they read live ORM rows and emit
EntityChunks / source dicts). We build minimal rows and assert exact chunk
ids, chunk_type splits, search_text framing, ACL membership, and the
citation-token parsing / ACL-filtering logic. No OpenSearch / LLM / network
is touched (chunkers don't embed; they only read the DB).
"""

import datetime as dt
import uuid

from django.test import SimpleTestCase, TestCase, override_settings

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.models.chat.unified_models import Channel, ChannelMember, Message
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent import acl as acl_mod
from origin.search_engine.agent import citation_resolver as cr
from origin.search_engine.chunkers.base import (
    CHAT_TYPE_DM,
    CHAT_TYPE_GM,
    CHAT_TYPE_MDM,
    CHAT_TYPE_PM,
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    Chunk,
    chat_entity_id,
    iso,
    make_snippet,
)
from origin.search_engine.chunkers.chat_chunker import iter_dm_chunks, iter_pm_chunks
from origin.search_engine.chunkers.milestone_chunker import iter_milestone_chunks
from origin.search_engine.chunkers.note_chunker import (
    iter_chat_note_chunks,
    iter_personal_note_chunks,
    iter_task_note_chunks,
)
from origin.search_engine.chunkers.note_summary_chunker import (
    iter_note_summary_chunks,
)
from origin.search_engine.chunkers.task_chunker import iter_task_chunks
from origin.search_engine.chunkers.thread_summary_chunker import (
    iter_thread_summary_chunks,
)
from origin.search_engine.chunkers.todo_chunker import iter_todo_chunks
from origin.search_engine.models import NoteSummary, ThreadSummary
from origin.tests.test_base import BaseAPITestCase

# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _bn(text):
    """Minimal BlockNote body holding a single paragraph of `text`."""
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


def _heading(text):
    return {"type": "heading", "content": [{"type": "text", "text": text}]}


def _para(text):
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


# --------------------------------------------------------------------------- #
# base.py pure helpers                                                         #
# --------------------------------------------------------------------------- #


class TestBaseHelpers(TestCase):
    def test_make_snippet_empty(self):
        self.assertEqual(make_snippet(""), "")
        self.assertEqual(make_snippet(None), "")

    def test_make_snippet_collapses_whitespace(self):
        self.assertEqual(make_snippet("a   b\n c\t d"), "a b c d")

    def test_make_snippet_short_text_unchanged(self):
        self.assertEqual(make_snippet("hello world", max_len=280), "hello world")

    def test_make_snippet_truncates_on_word_boundary_with_ellipsis(self):
        # 30 words of "word" -> length exceeds max_len=10; truncated at last
        # space within the first 10 chars, then "...".
        text = "alpha beta gamma delta"
        out = make_snippet(text, max_len=10)
        self.assertTrue(out.endswith("..."))
        # first 10 chars = "alpha beta"; rsplit on space -> "alpha"
        self.assertEqual(out, "alpha...")

    def test_chat_entity_id_no_thread(self):
        self.assertEqual(chat_entity_id("dm", 5), "dm:5")

    def test_chat_entity_id_with_thread(self):
        self.assertEqual(chat_entity_id("dm", 5, 9), "dm:5:thread:9")

    def test_chat_entity_id_thread_zero_is_included(self):
        # thread_id of 0 is not None, so it must be appended.
        self.assertEqual(chat_entity_id("gm", "abc", 0), "gm:abc:thread:0")

    def test_iso_none(self):
        self.assertIsNone(iso(None))

    def test_iso_datetime(self):
        d = dt.datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(iso(d), "2026-01-02T03:04:05")

    def test_chunk_to_dict_drops_none(self):
        c = Chunk(
            chunk_id="x",
            entity_type="chat",
            entity_id="dm:1",
            chunk_type="chat_message",
            team_id="t",
        )
        d = c.to_dict()
        # None-valued fields are removed.
        self.assertNotIn("author_id", d)
        self.assertNotIn("created_at", d)
        # Defaults (empty list / empty str) are kept (not None).
        self.assertEqual(d["acl_user_ids"], [])
        self.assertEqual(d["title"], "")
        self.assertEqual(d["chunk_id"], "x")


# --------------------------------------------------------------------------- #
# acl.py                                                                       #
# --------------------------------------------------------------------------- #


class TestAclHelpers(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        ChannelMember.objects.create(channel=self.dm, user=self.user)
        ChannelMember.objects.create(channel=self.dm, user=self.user2)

    def test_chat_acl_dm_returns_members(self):
        out = acl_mod.chat_acl_user_ids(CHAT_TYPE_DM, str(self.dm.id))
        self.assertEqual(out, {str(self.user.id), str(self.user2.id)})

    def test_chat_acl_excludes_soft_deleted_member(self):
        ChannelMember.objects.filter(channel=self.dm, user=self.user2).update(is_deleted=True)
        out = acl_mod.chat_acl_user_ids(CHAT_TYPE_DM, str(self.dm.id))
        self.assertEqual(out, {str(self.user.id)})

    def test_chat_acl_unknown_channel_id_returns_empty(self):
        self.assertEqual(acl_mod.chat_acl_user_ids(CHAT_TYPE_DM, uuid.uuid4()), set())

    def test_chat_acl_wrong_kind_returns_empty(self):
        # Channel exists but kind mismatch -> not found -> empty.
        self.assertEqual(acl_mod.chat_acl_user_ids(CHAT_TYPE_GM, str(self.dm.id)), set())

    def test_chat_acl_malformed_uuid_returns_empty_not_raises(self):
        # A non-UUID string must be swallowed (treated as not found).
        self.assertEqual(acl_mod.chat_acl_user_ids(CHAT_TYPE_DM, "not-a-uuid"), set())

    def test_chat_acl_pm_uses_project_members(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="ACL Proj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        # The PM Channel is auto-created by the ProjectMaster post_save
        # signal (1:1 via the partial unique constraint); fetch it rather
        # than creating a colliding one.
        pm_channel = Channel.objects.get(project=project, kind=CHAT_TYPE_PM)
        out = acl_mod.chat_acl_user_ids(CHAT_TYPE_PM, str(pm_channel.id))
        self.assertEqual(out, {str(self.user.id)})

    def test_task_acl_project_plus_assignee_plus_reporter(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="TaskACL Proj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        out = acl_mod.task_acl_user_ids(
            project_id=project.project_id,
            assignee_id=self.user2.id,
            reporter_id=self.user.id,
        )
        self.assertEqual(out, {str(self.user.id), str(self.user2.id)})

    def test_task_acl_no_project_just_assignee(self):
        out = acl_mod.task_acl_user_ids(project_id=None, assignee_id=self.user.id, reporter_id=None)
        self.assertEqual(out, {str(self.user.id)})

    def test_note_grants_user_ids(self):
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user2, note_id=77, note_type=NOTE_TYPE_PERSONAL, role_id=3
        )
        out = acl_mod.note_grants_user_ids(NOTE_TYPE_PERSONAL, 77)
        self.assertEqual(out, {str(self.user2.id)})
        # Wrong note_type -> no grant.
        self.assertEqual(acl_mod.note_grants_user_ids(NOTE_TYPE_TASK, 77), set())

    def test_personal_note_acl_owner_plus_grants(self):
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user2, note_id=5, note_type=NOTE_TYPE_PERSONAL, role_id=3
        )
        out = acl_mod.personal_note_acl_user_ids(owner_id=self.user.id, note_id=5)
        self.assertEqual(out, {str(self.user.id), str(self.user2.id)})

    def test_task_note_acl_owner_project_grants(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="TN Proj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
        out = acl_mod.task_note_acl_user_ids(
            owner_id=self.user.id, project_id=project.project_id, note_id=9
        )
        self.assertEqual(out, {str(self.user.id), str(self.user2.id)})

    def test_chat_note_acl_owner_plus_channel_members(self):
        out = acl_mod.chat_note_acl_user_ids(
            owner_id=self.user.id,
            chat_type_code=CHAT_TYPE_DM,
            channel_id=self.dm.id,
            note_id=3,
        )
        # owner already a member, user2 is the other member.
        self.assertEqual(out, {str(self.user.id), str(self.user2.id)})


# --------------------------------------------------------------------------- #
# chat_chunker.py                                                              #
# --------------------------------------------------------------------------- #


@override_settings(SEARCH_ENGINE={"RAG_CHAT_CONTEXT_WINDOW": 2})
class TestChatChunker(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM, title="My DM")
        ChannelMember.objects.create(channel=self.dm, user=self.user)
        ChannelMember.objects.create(channel=self.dm, user=self.user2)

    def _msg(self, **kw):
        defaults = dict(channel=self.dm, sender=self.user, body=_bn("hi"))
        defaults.update(kw)
        return Message.objects.create(**defaults)

    def test_main_channel_message_chunks(self):
        self._msg(seq=1, body=_bn("first message"))
        self._msg(seq=2, body=_bn("second message"))
        batches = list(iter_dm_chunks())
        self.assertEqual(len(batches), 1)
        batch = batches[0]
        self.assertEqual(batch.entity_type, "chat")
        self.assertEqual(batch.entity_id, f"dm:{self.dm.id}")
        self.assertEqual(len(batch.chunks), 2)
        c0 = batch.chunks[0]
        self.assertEqual(c0.chunk_type, "chat_message")
        self.assertEqual(c0.chat_type, "dm")
        self.assertEqual(c0.chat_id, str(self.dm.id))
        self.assertIsNone(c0.thread_id)
        self.assertEqual(c0.title, "My DM")
        self.assertEqual(c0.author_id, str(self.user.id))
        self.assertEqual(c0.author_name, "testuser")
        # ChannelMember query has no order_by, so ACL list order is not
        # contractually fixed — compare as a set.
        self.assertEqual(set(c0.acl_user_ids), {str(self.user.id), str(self.user2.id)})
        # First message: no prior context, search_text is the raw text.
        self.assertEqual(c0.search_text, "first message")

    def test_context_window_prefix_on_second_message(self):
        self._msg(seq=1, body=_bn("alpha"))
        self._msg(seq=2, body=_bn("beta"))
        chunks = list(iter_dm_chunks())[0].chunks
        c1 = chunks[1]
        self.assertEqual(c1.search_text, "Previously:\nalpha\n\nMessage:\nbeta")
        # Snippet stays focal-only.
        self.assertEqual(c1.snippet_text, "beta")

    @override_settings(SEARCH_ENGINE={"RAG_CHAT_CONTEXT_WINDOW": 0})
    def test_context_window_disabled(self):
        self._msg(seq=1, body=_bn("alpha"))
        self._msg(seq=2, body=_bn("beta"))
        chunks = list(iter_dm_chunks())[0].chunks
        # Window 0 -> no "Previously:" framing.
        self.assertEqual(chunks[1].search_text, "beta")

    def test_message_chunk_id_format(self):
        m = self._msg(seq=1, body=_bn("hello"))
        c = list(iter_dm_chunks())[0].chunks[0]
        self.assertEqual(c.chunk_id, f"chat:dm:{self.dm.id}:msg:{m.id}")
        self.assertEqual(c.chat_message_id, str(m.id))

    def test_empty_body_message_skipped(self):
        self._msg(seq=1, body=_bn("kept"))
        self._msg(seq=2, body=[])  # extract_text -> "" -> skipped
        chunks = list(iter_dm_chunks())[0].chunks
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].search_text, "kept")

    def test_soft_deleted_message_excluded(self):
        self._msg(seq=1, body=_bn("kept"))
        self._msg(seq=2, body=_bn("gone"), deleted_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc))
        chunks = list(iter_dm_chunks())[0].chunks
        self.assertEqual([c.search_text for c in chunks], ["kept"])

    def test_empty_acl_channel_skipped(self):
        # New channel with messages but no members -> skipped entirely.
        lonely = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        Message.objects.create(channel=lonely, sender=self.user, seq=1, body=_bn("nobody"))
        batches = [b for b in iter_dm_chunks() if b.entity_id == f"dm:{lonely.id}"]
        self.assertEqual(batches, [])

    def test_thread_splits_into_separate_entity(self):
        root = self._msg(seq=1, body=_bn("root question"))
        reply = self._msg(
            seq=2,
            body=_bn("the answer"),
            is_thread_reply=True,
            thread_root=root,
            parent=root,
        )
        batches = list(iter_dm_chunks())
        ids = {b.entity_id for b in batches}
        # The thread root is NOT in the main channel (mutually exclusive),
        # so the main channel produces no chunks -> no main entity. Only the
        # thread entity is emitted.
        self.assertEqual(ids, {f"dm:{self.dm.id}:thread:{root.id}"})
        thread_batch = next(b for b in batches if b.entity_id.endswith(f"thread:{root.id}"))
        types = [c.chunk_type for c in thread_batch.chunks]
        # anchor message + reply message + thread window.
        self.assertEqual(types.count("chat_message"), 2)
        self.assertEqual(types.count("chat_thread_window"), 1)

        anchor = next(
            c for c in thread_batch.chunks
            if c.chunk_type == "chat_message" and c.chat_message_id == str(root.id)
        )
        self.assertEqual(
            anchor.chunk_id,
            f"chat:dm:{self.dm.id}:thread:{root.id}:anchor:{root.id}",
        )
        self.assertEqual(anchor.thread_id, str(root.id))
        # Anchor search_text has no "Previously:" context framing.
        self.assertEqual(anchor.search_text, "root question")

        window = next(c for c in thread_batch.chunks if c.chunk_type == "chat_thread_window")
        self.assertEqual(window.chunk_id, f"chat:dm:{self.dm.id}:thread:{root.id}:window")
        self.assertEqual(window.search_text, "root question\nthe answer")
        # Window aggregates authors -> author_id stays None.
        self.assertIsNone(window.author_id)

    def test_thread_window_suppressed_when_summarized(self):
        root = self._msg(seq=1, body=_bn("root"))
        self._msg(seq=2, body=_bn("reply"), is_thread_reply=True, thread_root=root, parent=root)
        ThreadSummary.objects.create(
            team_id=str(self.team.team_id),
            chat_type=CHAT_TYPE_DM,
            chat_id=self.dm.id,
            thread_id=root.id,
            summary_text="summary",
        )
        batches = list(iter_dm_chunks())
        thread_batch = next(b for b in batches if b.entity_id.endswith(f"thread:{root.id}"))
        types = [c.chunk_type for c in thread_batch.chunks]
        # No thread window chunk because ThreadSummary exists.
        self.assertNotIn("chat_thread_window", types)
        self.assertEqual(types.count("chat_message"), 2)

    def test_task_link_in_related_entity_ids(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="LinkProj", owner=self.user
        )
        task = TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="open"
        )
        self._msg(seq=1, body=_bn("see task"), task=task)
        c = list(iter_dm_chunks())[0].chunks[0]
        self.assertEqual(c.related_entity_ids, [f"task:{task.task_id}"])

    def test_pm_acl_from_project_members_and_project_id(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="PMChat", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        # PM Channel auto-created by the ProjectMaster post_save signal.
        pm = Channel.objects.get(project=project, kind=CHAT_TYPE_PM)
        Message.objects.create(channel=pm, sender=self.user, seq=1, body=_bn("pm message"))
        batches = list(iter_pm_chunks())
        self.assertEqual(len(batches), 1)
        c = batches[0].chunks[0]
        self.assertEqual(c.chat_type, "pm")
        self.assertEqual(c.project_id, str(project.project_id))
        self.assertEqual(c.acl_user_ids, [str(self.user.id)])

    def test_placeholder_title_when_blank(self):
        blank = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM, title="")
        ChannelMember.objects.create(channel=blank, user=self.user)
        Message.objects.create(channel=blank, sender=self.user, seq=1, body=_bn("x"))
        batch = next(b for b in iter_dm_chunks() if b.entity_id == f"dm:{blank.id}")
        self.assertEqual(batch.chunks[0].title, "Direct message")


# --------------------------------------------------------------------------- #
# task_chunker.py                                                              #
# --------------------------------------------------------------------------- #


class TestTaskChunker(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="TaskChunk Proj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def _task(self, **kw):
        defaults = dict(
            team=self.team,
            project=self.project,
            title="Task title",
            status="open",
        )
        defaults.update(kw)
        return TaskMaster.objects.create(**defaults)

    def test_short_task_single_title_content_chunk(self):
        t = self._task(title="Fix bug", content=_bn("short description"))
        batches = list(iter_task_chunks())
        self.assertEqual(len(batches), 1)
        batch = batches[0]
        self.assertEqual(batch.entity_id, f"task:{t.task_id}")
        self.assertEqual(len(batch.chunks), 1)
        c = batch.chunks[0]
        self.assertEqual(c.chunk_type, "task_title_content")
        self.assertEqual(c.chunk_id, f"task:{t.task_id}:title_content")
        self.assertEqual(c.search_text, "Fix bug\nshort description")
        self.assertEqual(c.task_id, str(t.task_id))
        self.assertEqual(c.task_status, "open")

    def test_long_task_splits_title_and_content_chunks(self):
        long_text = "word " * 400  # > 1500 chars
        t = self._task(title="Big task", content=_bn(long_text.strip()))
        batch = list(iter_task_chunks())[0]
        types = sorted(c.chunk_type for c in batch.chunks)
        self.assertEqual(types, ["task_content_chunk", "task_title_content"])
        title_chunk = next(c for c in batch.chunks if c.chunk_type == "task_title_content")
        content_chunk = next(c for c in batch.chunks if c.chunk_type == "task_content_chunk")
        self.assertEqual(content_chunk.chunk_id, f"task:{t.task_id}:content")
        # Title chunk's content head is truncated; content chunk has it all.
        self.assertLess(len(title_chunk.search_text), len(content_chunk.search_text))
        self.assertTrue(content_chunk.search_text.startswith("word"))

    def test_comment_chunks_inherit_task_overlay(self):
        t = self._task(title="With comments", priority="high")
        TaskComments.objects.create(
            task=t, sender=self.user, comment_id=1, comment_body=_bn("first comment")
        )
        TaskComments.objects.create(
            task=t, sender=self.user, comment_id=2, comment_body=_bn("second comment")
        )
        batch = list(iter_task_chunks())[0]
        comment_chunks = [c for c in batch.chunks if c.chunk_type == "task_comment"]
        self.assertEqual(len(comment_chunks), 2)
        self.assertEqual(comment_chunks[0].chunk_id, f"task:{t.task_id}:comment:1")
        self.assertEqual(comment_chunks[0].search_text, "first comment")
        # Overlay inherited from parent task.
        self.assertEqual(comment_chunks[0].task_priority, "high")

    def test_deleted_comment_skipped(self):
        t = self._task(title="X")
        TaskComments.objects.create(
            task=t, sender=self.user, comment_id=1, comment_body=_bn("kept")
        )
        TaskComments.objects.create(
            task=t, sender=self.user, comment_id=2, comment_body=_bn("gone"), is_deleted=True
        )
        batch = list(iter_task_chunks())[0]
        comment_chunks = [c for c in batch.chunks if c.chunk_type == "task_comment"]
        self.assertEqual([c.search_text for c in comment_chunks], ["kept"])

    def test_acl_includes_assignee_and_reporter(self):
        t = self._task(title="Assigned", assignee=self.user2, reporter=self.user2)
        c = list(iter_task_chunks())[0].chunks[0]
        self.assertIn(str(self.user.id), c.acl_user_ids)  # project member
        self.assertIn(str(self.user2.id), c.acl_user_ids)  # assignee/reporter
        self.assertEqual(c.task_assignee_id, str(self.user2.id))

    def test_init_task_skipped(self):
        self._task(title="Init", is_init_task=True)
        self.assertEqual(list(iter_task_chunks()), [])

    def test_deleted_task_skipped(self):
        self._task(title="Deleted", is_deleted=True)
        self.assertEqual(list(iter_task_chunks()), [])

    def test_related_ids_chat_link_and_parent(self):
        parent = self._task(title="Parent")
        child = self._task(
            title="Child",
            parent_task_id=parent.task_id,
            chat_type=CHAT_TYPE_DM,
            chat_id="chan-uuid",
        )
        child_batch = next(
            b for b in iter_task_chunks() if b.entity_id == f"task:{child.task_id}"
        )
        related = child_batch.chunks[0].related_entity_ids
        self.assertIn("dm:chan-uuid", related)
        self.assertIn(f"task:{parent.task_id}", related)

    def test_title_only_task_emits_chunk(self):
        t = self._task(title="Only title", content=None)
        c = list(iter_task_chunks())[0].chunks[0]
        self.assertEqual(c.search_text, "Only title")
        self.assertEqual(c.title, "Only title")

    def test_since_filters_stale_tasks(self):
        old = self._task(title="Old task")
        new = self._task(title="New task")
        # Backdate the old task via queryset .update() (bypasses auto_now).
        stale_ts = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        TaskMaster.objects.filter(task_id=old.task_id).update(ts_updated_at=stale_ts)
        since = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        ids = {b.entity_id for b in iter_task_chunks(since=since)}
        self.assertIn(f"task:{new.task_id}", ids)
        self.assertNotIn(f"task:{old.task_id}", ids)

    def test_since_picks_up_task_with_recent_comment(self):
        # A task whose own row is stale but has a recent comment must be
        # re-emitted (comment_dirty_task_ids union).
        t = self._task(title="Stale body, fresh comment")
        TaskComments.objects.create(
            task=t, sender=self.user, comment_id=1, comment_body=_bn("new comment")
        )
        stale_ts = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        TaskMaster.objects.filter(task_id=t.task_id).update(ts_updated_at=stale_ts)
        since = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        ids = {b.entity_id for b in iter_task_chunks(since=since)}
        self.assertIn(f"task:{t.task_id}", ids)


class TestMilestoneChunker(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="MS Proj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def _ms(self, **kw):
        defaults = dict(team=self.team, project=self.project, title="Milestone title")
        defaults.update(kw)
        return MilestoneMaster.objects.create(**defaults)

    def test_milestone_title_and_description_chunk(self):
        backing = TaskMaster.objects.create(
            team=self.team, project=self.project, title="backing", status="open"
        )
        m = self._ms(title="Q3 launch", description=_bn("ship the new search"), task=backing)
        batches = list(iter_milestone_chunks())
        self.assertEqual(len(batches), 1)
        batch = batches[0]
        self.assertEqual(batch.entity_type, "milestone")
        self.assertEqual(batch.entity_id, f"milestone:{m.milestone_id}")
        self.assertEqual(len(batch.chunks), 1)
        c = batch.chunks[0]
        self.assertEqual(c.chunk_type, "milestone_title_content")
        self.assertEqual(c.chunk_id, f"milestone:{m.milestone_id}:title_content")
        self.assertEqual(c.search_text, "Q3 launch\nship the new search")
        self.assertEqual(c.title, "Q3 launch")
        # Backing task surfaced so Spotlight deep-links via the task view.
        self.assertEqual(c.task_id, str(backing.task_id))
        self.assertEqual(c.project_id, str(self.project.project_id))
        self.assertIn(str(self.user.id), c.acl_user_ids)  # project member

    def test_milestone_acl_includes_assignee_and_reporter(self):
        m = self._ms(title="Assigned MS", reporter=self.user2)
        MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user2)
        c = list(iter_milestone_chunks())[0].chunks[0]
        self.assertIn(str(self.user.id), c.acl_user_ids)  # project member
        self.assertIn(str(self.user2.id), c.acl_user_ids)  # assignee + reporter

    def test_deleted_milestone_skipped(self):
        self._ms(title="Gone", is_deleted=True)
        self.assertEqual(list(iter_milestone_chunks()), [])

    def test_title_only_milestone_emits_chunk_without_backing_task(self):
        self._ms(title="Only title", description=None)
        c = list(iter_milestone_chunks())[0].chunks[0]
        self.assertEqual(c.search_text, "Only title")
        self.assertIsNone(c.task_id)

    def test_since_filters_stale_milestones(self):
        old = self._ms(title="Old MS")
        new = self._ms(title="New MS")
        stale_ts = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        MilestoneMaster.objects.filter(milestone_id=old.milestone_id).update(
            ts_updated_at=stale_ts
        )
        since = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        ids = {b.entity_id for b in iter_milestone_chunks(since=since)}
        self.assertIn(f"milestone:{new.milestone_id}", ids)
        self.assertNotIn(f"milestone:{old.milestone_id}", ids)


# --------------------------------------------------------------------------- #
# todo_chunker.py                                                              #
# --------------------------------------------------------------------------- #


class TestTodoChunker(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=dt.date(2026, 5, 31)
        )

    def test_single_chunk_owner_only_acl(self):
        item = ToDoItem.objects.create(group=self.group, title="Buy milk", sort_order=0)
        batches = list(iter_todo_chunks())
        self.assertEqual(len(batches), 1)
        batch = batches[0]
        expected_id = f"todo:2026-05-31:item:{item.item_id}"
        self.assertEqual(batch.entity_id, expected_id)
        self.assertEqual(len(batch.chunks), 1)
        c = batch.chunks[0]
        self.assertEqual(c.chunk_id, expected_id)
        self.assertEqual(c.chunk_type, "todo_item")
        self.assertEqual(c.acl_user_ids, [str(self.user.id)])
        self.assertEqual(c.search_text, "Buy milk")
        self.assertEqual(c.related_entity_ids, ["todo:2026-05-31"])

    def test_search_text_joins_title_category_notes(self):
        cat = ToDoCategory.objects.create(team=self.team, user=self.user, name="Errands")
        item = ToDoItem.objects.create(
            group=self.group, title="Task", category=cat, notes=_bn("detail notes"), sort_order=0
        )
        c = list(iter_todo_chunks())[0].chunks[0]
        self.assertEqual(c.search_text, "Task\nErrands\ndetail notes")
        # Snippet prefers notes text.
        self.assertEqual(c.snippet_text, "detail notes")

    def test_item_without_text_skipped(self):
        # Title is required (max_length 512) but can be empty string.
        ToDoItem.objects.create(group=self.group, title="", notes=None, sort_order=0)
        self.assertEqual(list(iter_todo_chunks()), [])


# --------------------------------------------------------------------------- #
# thread_summary_chunker.py                                                    #
# --------------------------------------------------------------------------- #


class TestThreadSummaryChunker(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        ChannelMember.objects.create(channel=self.dm, user=self.user)
        self.thread_root = uuid.uuid4()

    def test_summary_chunk_fields(self):
        ts = ThreadSummary.objects.create(
            team_id=str(self.team.team_id),
            chat_type=CHAT_TYPE_DM,
            chat_id=self.dm.id,
            thread_id=self.thread_root,
            summary_text="A concise summary.",
        )
        batches = list(iter_thread_summary_chunks())
        self.assertEqual(len(batches), 1)
        c = batches[0].chunks[0]
        self.assertEqual(c.entity_type, "thread_summary")
        self.assertEqual(c.chunk_type, "thread_summary")
        self.assertEqual(
            c.entity_id, f"thread_summary:{ts.chat_type}:{ts.chat_id}:{ts.thread_id}"
        )
        self.assertEqual(c.search_text, "A concise summary.")
        self.assertEqual(c.acl_user_ids, [str(self.user.id)])
        self.assertEqual(c.chat_type, "dm")
        self.assertEqual(c.related_entity_ids, [chat_entity_id("dm", self.dm.id, self.thread_root)])

    def test_empty_acl_summary_skipped(self):
        # Channel exists but has no members -> ACL empty -> skipped.
        ChannelMember.objects.filter(channel=self.dm).update(is_deleted=True)
        ThreadSummary.objects.create(
            team_id=str(self.team.team_id),
            chat_type=CHAT_TYPE_DM,
            chat_id=self.dm.id,
            thread_id=self.thread_root,
            summary_text="orphaned",
        )
        self.assertEqual(list(iter_thread_summary_chunks()), [])

    def test_blank_summary_text_skipped(self):
        ThreadSummary.objects.create(
            team_id=str(self.team.team_id),
            chat_type=CHAT_TYPE_DM,
            chat_id=self.dm.id,
            thread_id=self.thread_root,
            summary_text="",
        )
        self.assertEqual(list(iter_thread_summary_chunks()), [])


# --------------------------------------------------------------------------- #
# note_chunker.py                                                              #
# --------------------------------------------------------------------------- #


class TestNoteChunker(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        ChannelMember.objects.create(channel=self.dm, user=self.user)
        ChannelMember.objects.create(channel=self.dm, user=self.user2)

    def test_personal_note_single_section(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="My note", body=_bn("body text")
        )
        batches = list(iter_personal_note_chunks())
        self.assertEqual(len(batches), 1)
        batch = batches[0]
        self.assertEqual(batch.entity_id, f"note:personal:{note.note_id}")
        self.assertEqual(len(batch.chunks), 1)
        c = batch.chunks[0]
        self.assertEqual(c.chunk_type, "note_section")
        self.assertEqual(c.chunk_id, f"note:personal:{note.note_id}:section:0")
        self.assertEqual(c.note_type, "personal")
        # Title repeated in section search_text.
        self.assertEqual(c.search_text, "My note\nbody text")
        self.assertEqual(c.acl_user_ids, [str(self.user.id)])
        self.assertEqual(c.note_owner_id, str(self.user.id))

    def test_note_splits_on_headings(self):
        body = [
            _para("intro text"),
            _heading("Risks"),
            _para("risk one"),
            _heading("Plan"),
            _para("the plan"),
        ]
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Project", body=body
        )
        chunks = list(iter_personal_note_chunks())[0].chunks
        # Pre-heading section + 2 heading sections = 3 chunks.
        self.assertEqual(len(chunks), 3)
        self.assertEqual([c.chunk_id.split(":")[-1] for c in chunks], ["0", "1", "2"])
        # Every section repeats the note title.
        for c in chunks:
            self.assertTrue(c.search_text.startswith("Project"))
        self.assertIn("Risks", chunks[1].search_text)
        self.assertIn("risk one", chunks[1].search_text)

    def test_title_only_note_degenerate_section(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Just a title", body=None
        )
        chunks = list(iter_personal_note_chunks())[0].chunks
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].search_text, "Just a title")

    def test_personal_note_grant_extends_acl(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Shared", body=_bn("x")
        )
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user2, note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL, role_id=3,
        )
        c = list(iter_personal_note_chunks())[0].chunks[0]
        self.assertEqual(set(c.acl_user_ids), {str(self.user.id), str(self.user2.id)})

    def test_chat_note_acl_union_and_coordinates(self):
        note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=CHAT_TYPE_DM,
            channel=self.dm,
            is_thread=False,
            title="Chat note",
            body=_bn("note body"),
        )
        batch = list(iter_chat_note_chunks())[0]
        self.assertEqual(batch.entity_id, f"note:chat:{note.note_id}")
        c = batch.chunks[0]
        self.assertEqual(c.note_type, "chat")
        self.assertEqual(c.chat_type, "dm")
        self.assertEqual(c.chat_id, str(self.dm.id))
        self.assertIsNone(c.thread_id)  # non-thread note
        # ACL = owner + both channel members.
        self.assertEqual(set(c.acl_user_ids), {str(self.user.id), str(self.user2.id)})
        self.assertIn(chat_entity_id("dm", self.dm.id), c.related_entity_ids)

    def test_task_note_acl_project_members(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="TN chunk proj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
        task = TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="open"
        )
        note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=project, task=task,
            title="Task note", body=_bn("body"),
        )
        c = list(iter_task_note_chunks())[0].chunks[0]
        self.assertEqual(c.note_type, "task")
        self.assertEqual(c.task_id, str(task.task_id))
        self.assertEqual(c.project_id, str(project.project_id))
        self.assertEqual(set(c.acl_user_ids), {str(self.user.id), str(self.user2.id)})
        self.assertIn(f"task:{task.task_id}", c.related_entity_ids)


# --------------------------------------------------------------------------- #
# note_summary_chunker.py                                                      #
# --------------------------------------------------------------------------- #


class TestNoteSummaryChunker(BaseAPITestCase):
    def test_personal_note_summary_chunk(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Topic", body=_bn("x")
        )
        NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=NOTE_TYPE_PERSONAL,
            note_id=note.note_id,
            summary_text="The summary.",
        )
        batches = list(iter_note_summary_chunks())
        self.assertEqual(len(batches), 1)
        c = batches[0].chunks[0]
        self.assertEqual(c.entity_type, "note_summary")
        self.assertEqual(c.entity_id, f"note_summary:{NOTE_TYPE_PERSONAL}:{note.note_id}")
        self.assertEqual(c.note_type, "personal")
        self.assertEqual(c.title, "Summary — Topic")
        self.assertEqual(c.search_text, "The summary.")
        self.assertEqual(c.acl_user_ids, [str(self.user.id)])
        self.assertEqual(c.related_entity_ids, [f"note:personal:{note.note_id}"])

    def test_summary_for_missing_note_skipped(self):
        # No underlying note row -> resolver returns None -> skipped.
        NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=NOTE_TYPE_PERSONAL,
            note_id=999999,
            summary_text="orphan",
        )
        self.assertEqual(list(iter_note_summary_chunks()), [])

    def test_blank_summary_skipped(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="T", body=_bn("x")
        )
        NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=NOTE_TYPE_PERSONAL,
            note_id=note.note_id,
            summary_text="",
        )
        self.assertEqual(list(iter_note_summary_chunks()), [])

    def test_task_note_summary_breadcrumbs_and_acl(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="NSTaskProj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="open"
        )
        note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=project, task=task,
            title="Task summary note", body=_bn("x"),
        )
        NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=NOTE_TYPE_TASK,
            note_id=note.note_id,
            summary_text="Task note summary.",
        )
        c = list(iter_note_summary_chunks())[0].chunks[0]
        self.assertEqual(c.note_type, "task")
        self.assertEqual(c.project_id, str(project.project_id))
        self.assertEqual(c.task_id, str(task.task_id))
        self.assertEqual(c.related_entity_ids, [f"note:task:{note.note_id}"])
        self.assertIn(str(self.user.id), c.acl_user_ids)

    def test_chat_note_summary_breadcrumbs(self):
        dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        ChannelMember.objects.create(channel=dm, user=self.user)
        thread_root = uuid.uuid4()
        note = ChatNoteMaster.objects.create(
            team=self.team, owner=self.user, chat_type=CHAT_TYPE_DM, channel=dm,
            is_thread=True, thread_root_id=thread_root, title="Chat summary note", body=_bn("x"),
        )
        NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=NOTE_TYPE_CHAT,
            note_id=note.note_id,
            summary_text="Chat note summary.",
        )
        c = list(iter_note_summary_chunks())[0].chunks[0]
        self.assertEqual(c.note_type, "chat")
        self.assertEqual(c.chat_type, "dm")
        self.assertEqual(c.chat_id, str(dm.id))
        self.assertEqual(c.thread_id, str(thread_root))
        self.assertEqual(c.related_entity_ids, [f"note:chat:{note.note_id}"])
        self.assertEqual(c.acl_user_ids, [str(self.user.id)])


# --------------------------------------------------------------------------- #
# citation_resolver.py                                                         #
# --------------------------------------------------------------------------- #


class TestCitationResolver(BaseAPITestCase):
    """Citation resolver builds new source dicts for [type:id] tokens.

    We pass simple echo builders so we can assert which entities were
    resolved (and that ACL filtering / seen_keys dedup happened).
    """

    def setUp(self):
        super().setUp()
        self.team_id = str(self.team.team_id)
        self.user_id = str(self.user.id)

        # Echo builders capturing positional args.
        self.build_task = lambda *a: {"kind": "task", "args": a}
        self.build_project = lambda *a: {"kind": "project", "args": a}
        self.build_chat = lambda *a: {"kind": "chat", "args": a}
        self.build_note = lambda *a: {"kind": "note", "args": a}
        self.build_todo = lambda *a: {"kind": "todo", "args": a}
        self.build_milestone = lambda *a: {"kind": "milestone", "args": a}

    def _resolve(self, answer, seen_keys=None):
        return cr.resolve_unresolved_citations(
            answer=answer,
            seen_keys=seen_keys or set(),
            team_id=self.team_id,
            user_id=self.user_id,
            build_task_source=self.build_task,
            build_project_source=self.build_project,
            build_chat_source=self.build_chat,
            build_note_source=self.build_note,
            build_todo_source=self.build_todo,
            build_milestone_source=self.build_milestone,
        )

    def test_empty_answer_returns_empty(self):
        self.assertEqual(self._resolve(""), [])

    def test_no_tokens_returns_empty(self):
        self.assertEqual(self._resolve("just some plain text, no citations"), [])

    def test_resolves_visible_task(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="CitProj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=self.team, project=project, title="Cited task", status="open"
        )
        out = self._resolve(f"see [task:{task.task_id}] for details")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "task")
        self.assertEqual(out[0]["args"], (task.task_id, "Cited task", project.project_id))

    def test_resolves_visible_task_link_form(self):
        # §4.6 natural-prose link `[prose](task:id)` — the resolver must
        # extract the id from the URL, not the single-word prose label.
        project = ProjectMaster.objects.create(
            team=self.team, project_name="CitProj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=self.team, project=project, title="Cited task", status="open"
        )
        out = self._resolve(f"the team [ruled it out](task:{task.task_id}) early")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "task")
        self.assertEqual(out[0]["args"], (task.task_id, "Cited task", project.project_id))

    def test_task_not_visible_filtered_out(self):
        # Task with a project the user is NOT a member of, and not
        # assignee/reporter -> ACL denies -> no source.
        project = ProjectMaster.objects.create(
            team=self.team, project_name="HiddenProj", owner=self.user2
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
        task = TaskMaster.objects.create(
            team=self.team, project=project, title="Hidden", status="open",
            assignee=self.user2, reporter=self.user2,
        )
        out = self._resolve(f"[task:{task.task_id}]")
        self.assertEqual(out, [])

    def test_seen_key_skips_token(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="SeenProj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=self.team, project=project, title="Already seen", status="open"
        )
        seen = {("task", f"task:{task.task_id}")}
        out = self._resolve(f"[task:{task.task_id}]", seen_keys=seen)
        self.assertEqual(out, [])

    def test_resolves_public_project(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="PubProj", owner=self.user, is_private=False
        )
        out = self._resolve(f"[project:{project.project_id}]")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "project")
        self.assertEqual(out[0]["args"], (project.project_id, "PubProj"))

    def test_private_project_requires_membership(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="PrivProj", owner=self.user2, is_private=True
        )
        # user is NOT a member -> filtered.
        out = self._resolve(f"[project:{project.project_id}]")
        self.assertEqual(out, [])
        # Add membership -> resolves.
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        out2 = self._resolve(f"[project:{project.project_id}]")
        self.assertEqual(len(out2), 1)

    def test_personal_note_token_resolves_for_owner(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="My personal note", body=_bn("x")
        )
        out = self._resolve(f"[note:personal:{note.note_id}]")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "note")
        # build(note_type_label, note_id, title, parent_context)
        self.assertEqual(out[0]["args"], ("personal", note.note_id, "My personal note", {}))

    def test_note_my_label_maps_to_personal(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Aliased", body=_bn("x")
        )
        # The frontend "my" label must map to the personal note code.
        out = self._resolve(f"[note:my:{note.note_id}]")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["args"][0], "personal")

    def test_personal_note_not_owner_filtered(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Not mine", body=_bn("x")
        )
        out = self._resolve(f"[note:personal:{note.note_id}]")
        self.assertEqual(out, [])

    def test_task_note_token_resolves_with_parent_context(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="CitTaskNoteProj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="open"
        )
        note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=project, task=task,
            title="Task note", body=_bn("x"),
        )
        out = self._resolve(f"[note:task:{note.note_id}]")
        self.assertEqual(len(out), 1)
        label, nid, title, parent = out[0]["args"]
        self.assertEqual((label, nid, title), ("task", note.note_id, "Task note"))
        # parent_context carries stringified project/task ids.
        self.assertEqual(parent["project_id"], str(project.project_id))
        self.assertEqual(parent["task_id"], str(task.task_id))

    def test_chat_note_token_resolves_with_parent_context(self):
        dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        ChannelMember.objects.create(channel=dm, user=self.user)
        thread_root = uuid.uuid4()
        note = ChatNoteMaster.objects.create(
            team=self.team, owner=self.user, chat_type=CHAT_TYPE_DM, channel=dm,
            is_thread=True, thread_root_id=thread_root, title="Chat note", body=_bn("x"),
        )
        out = self._resolve(f"[note:chat:{note.note_id}]")
        self.assertEqual(len(out), 1)
        label, nid, title, parent = out[0]["args"]
        self.assertEqual((label, nid, title), ("chat", note.note_id, "Chat note"))
        self.assertEqual(parent["chat_type"], "dm")
        self.assertEqual(parent["chat_id"], str(dm.id))
        self.assertEqual(parent["thread_id"], str(thread_root))
        self.assertTrue(parent["is_thread"])

    def test_chat_note_token_not_member_filtered(self):
        dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        ChannelMember.objects.create(channel=dm, user=self.user2)  # not self.user
        note = ChatNoteMaster.objects.create(
            team=self.team, owner=self.user2, chat_type=CHAT_TYPE_DM, channel=dm,
            is_thread=False, title="Hidden chat note", body=_bn("x"),
        )
        out = self._resolve(f"[note:chat:{note.note_id}]")
        self.assertEqual(out, [])

    def test_chat_token_resolves_with_membership(self):
        dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        ChannelMember.objects.create(channel=dm, user=self.user)
        out = self._resolve(f"[chat:dm:{dm.id}]")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "chat")
        # build(chat_label, chat_id, thread_id) -> thread_id None.
        self.assertEqual(out[0]["args"], ("dm", str(dm.id), None))

    def test_chat_token_with_thread_parsed(self):
        dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        ChannelMember.objects.create(channel=dm, user=self.user)
        thread = str(uuid.uuid4())
        out = self._resolve(f"[chat:dm:{dm.id}:thread:{thread}]")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["args"], ("dm", str(dm.id), thread))

    def test_chat_token_not_member_filtered(self):
        dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        ChannelMember.objects.create(channel=dm, user=self.user2)  # not self.user
        out = self._resolve(f"[chat:dm:{dm.id}]")
        self.assertEqual(out, [])

    def test_malformed_task_token_ignored(self):
        # Non-integer task id -> _safe_int None -> dropped silently.
        out = self._resolve("[task:abc]")
        self.assertEqual(out, [])

    def test_non_entity_bracket_not_matched(self):
        # The regex is anchored to known prefixes only.
        out = self._resolve("[reminder: ship Friday] and [foo:1]")
        self.assertEqual(out, [])

    def test_safe_int_helper(self):
        self.assertEqual(cr._safe_int("42"), 42)
        self.assertIsNone(cr._safe_int("x"))
        self.assertIsNone(cr._safe_int(None))

    def test_chat_label_to_code_mapping(self):
        # Sanity check the reverse map used to parse chat tokens.
        self.assertEqual(cr._CHAT_LABEL_TO_CODE["dm"], CHAT_TYPE_DM)
        self.assertEqual(cr._CHAT_LABEL_TO_CODE["pm"], CHAT_TYPE_PM)
        self.assertEqual(cr._CHAT_LABEL_TO_CODE["mdm"], CHAT_TYPE_MDM)

    # ----- todo -----

    def test_resolves_visible_todo(self):
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=dt.date(2026, 7, 3)
        )
        item = ToDoItem.objects.create(group=group, title="Cited todo", sort_order=0)
        out = self._resolve(f"deferred [todo:2026-07-03:item:{item.item_id}]")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "todo")
        # Date comes from the item's group, not the token.
        self.assertEqual(out[0]["args"], (item.item_id, "Cited todo", "2026-07-03"))

    def test_todo_not_owner_filtered_out(self):
        # Todo owned by user2 — the requesting user (self.user) can't see it.
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user2, local_date=dt.date(2026, 7, 3)
        )
        item = ToDoItem.objects.create(group=group, title="Private todo", sort_order=0)
        out = self._resolve(f"[todo:2026-07-03:item:{item.item_id}]")
        self.assertEqual(out, [])

    def test_seen_key_skips_todo_token(self):
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=dt.date(2026, 7, 3)
        )
        item = ToDoItem.objects.create(group=group, title="Seen todo", sort_order=0)
        seen = {("todo", f"todo:2026-07-03:item:{item.item_id}")}
        out = self._resolve(f"[todo:2026-07-03:item:{item.item_id}]", seen_keys=seen)
        self.assertEqual(out, [])

    def test_malformed_todo_token_ignored(self):
        # Missing the `item` segment / non-int id -> dropped silently.
        self.assertEqual(self._resolve("[todo:2026-07-03]"), [])
        self.assertEqual(self._resolve("[todo:2026-07-03:item:abc]"), [])

    # ----- milestone -----

    def test_resolves_visible_milestone(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="MSProj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        backing = TaskMaster.objects.create(
            team=self.team, project=project, title="backing", status="open"
        )
        m = MilestoneMaster.objects.create(
            team=self.team, project=project, title="Cited MS", task=backing
        )
        out = self._resolve(f"targeting [milestone:{m.milestone_id}]")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "milestone")
        # Backing task_id + project_id feed the frontend deep-link.
        self.assertEqual(
            out[0]["args"],
            (m.milestone_id, "Cited MS", project.project_id, backing.task_id),
        )

    def test_milestone_visible_via_assignee(self):
        # User is NOT a project member but IS a milestone assignee.
        project = ProjectMaster.objects.create(
            team=self.team, project_name="OtherProj", owner=self.user2
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
        m = MilestoneMaster.objects.create(team=self.team, project=project, title="Assigned MS")
        MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user)
        out = self._resolve(f"[milestone:{m.milestone_id}]")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["kind"], "milestone")
        # No backing task -> task_id resolves to None.
        self.assertEqual(
            out[0]["args"], (m.milestone_id, "Assigned MS", project.project_id, None)
        )

    def test_milestone_not_visible_filtered_out(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="HiddenMSProj", owner=self.user2
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
        m = MilestoneMaster.objects.create(team=self.team, project=project, title="Hidden MS")
        out = self._resolve(f"[milestone:{m.milestone_id}]")
        self.assertEqual(out, [])

    def test_deleted_milestone_filtered_out(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="DelMSProj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        m = MilestoneMaster.objects.create(
            team=self.team, project=project, title="Gone MS", is_deleted=True
        )
        out = self._resolve(f"[milestone:{m.milestone_id}]")
        self.assertEqual(out, [])


# --------------------------------------------------------------------------- #
# controller._INLINE_CITATION_RE — link-aware cited-id extraction (§4.6 D5)    #
# --------------------------------------------------------------------------- #


class TestInlineCitationScanner(SimpleTestCase):
    """The chip-ranking scanner must extract ids from BOTH citation forms:
    the natural-prose link `[prose](type:id)` and the bare `[type:id]`."""

    def test_extracts_id_from_both_forms(self):
        from origin.search_engine.agent.controller import _iter_cited_ids

        text = (
            "team [ruled out framer](task:42), the [spike](task:7), "
            "[note:personal:9], and the [MDN docs](https://mdn.dev)."
        )
        # Link form yields the URL id (not the prose label); bare form
        # yields the token; a real external link is ignored.
        self.assertEqual(set(_iter_cited_ids(text)), {"task:42", "task:7", "note:personal:9"})

    def test_ranks_link_form_cited_source_first(self):
        from origin.search_engine.agent.controller import _rank_sources_by_citation

        sources = [
            {"entity_type": "note", "entity_id": "note:personal:9"},
            {"entity_type": "task", "entity_id": "task:42"},
        ]
        # Only task:42 is cited (via a link) — it must sort ahead of the note.
        ranked = _rank_sources_by_citation("the team [ruled it out](task:42).", sources)
        self.assertEqual(ranked[0]["entity_id"], "task:42")
