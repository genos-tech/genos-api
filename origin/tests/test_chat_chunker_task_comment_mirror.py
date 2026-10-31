"""A task comment's copy in its project-chat thread isn't indexed as chat.

`unified_writer.write_task_comment_as_thread_reply` mirrors every task
comment into the task's PM thread. The comment is indexed from the task
already (`task_chunker`), so indexing the mirror as well put the same text
in Spotlight twice, and the chat copy ranked above the comment.
"""

from origin.models.chat.unified_models import Channel, Message
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.chunkers.base import CHAT_TYPE_PM
from origin.search_engine.chunkers.chat_chunker import iter_pm_chunks
from origin.services import unified_writer
from origin.tests.test_base import BaseAPITestCase

COMMENT = "Real cause of the missing comments: not indexing"


def _bn(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class TestTaskCommentMirrorIsNotIndexedAsChat(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        project = ProjectMaster.objects.create(team=self.team, project_name="P", owner=self.user)
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        self.pm = Channel.objects.get(project=project, kind=CHAT_TYPE_PM)
        self.task = TaskMaster.objects.create(
            team=self.team, project=project, title="Comments not searchable", reporter=self.user
        )
        self.mirror = unified_writer.write_task_comment_as_thread_reply(
            task_id=self.task.task_id,
            comment_id=1,
            sender_id=str(self.user.id),
            body=_bn(COMMENT),
            project_id=project.project_id,
            bypass_flag=True,
        )
        self.assertIsNotNone(self.mirror)
        self.root = self.mirror.thread_root

    def _reply(self, text, **extra):
        seq = Message.objects.filter(channel=self.pm).order_by("-seq").first().seq + 1
        return Message.objects.create(
            channel=self.pm,
            sender=self.user,
            seq=seq,
            body=_bn(text),
            is_thread_reply=True,
            thread_root=self.root,
            parent=self.root,
            **extra,
        )

    def _thread_chunks(self):
        entity_id = f"pm:{self.pm.id}:thread:{self.root.id}"
        batches = [b for b in iter_pm_chunks() if b.entity_id == entity_id]
        self.assertEqual(len(batches), 1, "the thread entity is still emitted")
        return batches[0].chunks

    def test_no_thread_chunk_carries_the_comment(self):
        self._reply("Agreed, let's fix the ranking")
        chunks = self._thread_chunks()
        for c in chunks:
            self.assertNotIn("missing comments", c.search_text, c.chunk_id)
        self.assertNotIn(
            str(self.mirror.id), {c.chat_message_id for c in chunks if c.chat_message_id}
        )

    def test_a_person_s_reply_in_the_same_thread_is_still_indexed(self):
        reply = self._reply("Agreed, let's fix the ranking")
        chunks = self._thread_chunks()
        texts = [c.search_text for c in chunks if c.chat_message_id == str(reply.id)]
        self.assertEqual(len(texts), 1)
        self.assertIn("fix the ranking", texts[0])

    def test_a_thread_of_only_mirrors_keeps_its_anchor(self):
        chunks = self._thread_chunks()
        self.assertTrue(any(c.chunk_id.endswith(f":anchor:{self.root.id}") for c in chunks))

    def test_only_the_mirror_marker_excludes_a_message(self):
        # A reply about the task, or one carrying stray metadata without a
        # task, is ordinary chat.
        about_task = self._reply("Card reply mentioning the task", task_id=self.task.task_id)
        stray = self._reply("Has metadata only", metadata={"taskCommentId": 7})
        ids = {c.chat_message_id for c in self._thread_chunks()}
        self.assertIn(str(about_task.id), ids)
        self.assertIn(str(stray.id), ids)
