"""Incremental chat chunking (MIP-2383): `since` re-emits only what changed.

Each cron tick used to re-chunk every channel with one changed message, whole. Now a
channel's changed threads are rebuilt, and its main timeline only when it can have changed.
The contract these tests pin: for every entity the incremental pass emits, the chunks are
identical (ids, text, metadata) to the full pass, and an entity it does not emit did not
change.
"""

from django.utils import timezone

from origin.models.chat.unified_models import Channel, Message
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.acl_dirty import DirtyScopes
from origin.search_engine.chunkers.base import CHAT_TYPE_PM
from origin.search_engine.chunkers.chat_chunker import iter_pm_chunks
from origin.tests.test_base import BaseAPITestCase

THREADS = 12


def _bn(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class TestIncrementalChatChunker(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        project = ProjectMaster.objects.create(team=self.team, project_name="P", owner=self.user)
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        self.pm = Channel.objects.get(project=project, kind=CHAT_TYPE_PM)
        self.seq = 0
        self.plain = [self._msg(f"plain message {i}") for i in range(4)]
        self.roots = []
        self.replies = {}
        for i in range(THREADS):
            root = self._msg(f"root {i}")
            self.roots.append(root)
            self.replies[root.id] = [self._msg(f"reply {i}.{j}", root=root) for j in range(2)]
        self.since = timezone.now()

    def _msg(self, text, root=None):
        self.seq += 1
        return Message.objects.create(
            channel=self.pm,
            sender=self.user,
            seq=self.seq,
            body=_bn(text),
            is_thread_reply=root is not None,
            thread_root=root,
            parent=root,
        )

    def _edit(self, msg, text):
        msg.body = _bn(text)
        msg.save()

    def _full(self):
        return {e.entity_id: e.chunks for e in iter_pm_chunks()}

    def _incremental(self, dirty=None):
        return {e.entity_id: e.chunks for e in iter_pm_chunks(since=self.since, dirty=dirty)}

    def _thread_eid(self, root):
        return f"pm:{self.pm.id}:thread:{root.id}"

    def _main_eid(self):
        return f"pm:{self.pm.id}"

    def assertEquivalent(self, inc, full):
        """Every entity the incremental pass emits equals the full pass's."""
        for eid, chunks in inc.items():
            self.assertIn(eid, full)
            self.assertEqual(chunks, full[eid], eid)

    def test_one_changed_reply_emits_only_its_thread(self):
        self._edit(self.replies[self.roots[3].id][1], "reply 3.1 edited")
        inc = self._incremental()
        self.assertEqual(list(inc), [self._thread_eid(self.roots[3])])
        full = self._full()
        self.assertEqual(len(full), THREADS + 1)
        self.assertGreaterEqual(len(full) / len(inc), 10)
        self.assertEquivalent(inc, full)
        self.assertTrue(
            any("reply 3.1 edited" in c.search_text for c in inc[self._thread_eid(self.roots[3])])
        )

    def test_new_reply_in_existing_thread(self):
        self._msg("one more", root=self.roots[5])
        inc = self._incremental()
        self.assertEqual(list(inc), [self._thread_eid(self.roots[5])])
        self.assertEquivalent(inc, self._full())

    def test_first_reply_moves_the_root_out_of_the_main_timeline(self):
        # plain[1] becomes a thread root: its chunk leaves main, its thread appears.
        self._msg("first reply", root=self.plain[1])
        inc = self._incremental()
        self.assertEqual(set(inc), {self._main_eid(), self._thread_eid(self.plain[1])})
        full = self._full()
        self.assertEquivalent(inc, full)
        main_ids = {c.chunk_id for c in inc[self._main_eid()]}
        self.assertNotIn(f"chat:pm:{self.pm.id}:msg:{self.plain[1].id}", main_ids)

    def test_last_reply_deleted_restores_the_root_to_the_main_timeline(self):
        solo = self._msg("solo root")
        reply = self._msg("solo reply", root=solo)
        self.since = timezone.now()
        reply.deleted_at = timezone.now()
        reply.save()
        inc = self._incremental()
        self.assertNotIn(self._thread_eid(solo), inc)  # swept by the orphan sweep, as before
        self.assertEquivalent(inc, self._full())
        main_ids = {c.chunk_id for c in inc[self._main_eid()]}
        self.assertIn(f"chat:pm:{self.pm.id}:msg:{solo.id}", main_ids)

    def test_changed_main_message_rebuilds_main_with_context(self):
        self._edit(self.plain[1], "plain message 1 edited")
        inc = self._incremental()
        self.assertIn(self._main_eid(), inc)
        self.assertEquivalent(inc, self._full())
        later = [c for c in inc[self._main_eid()] if c.chat_message_id == str(self.plain[2].id)]
        self.assertIn("plain message 1 edited", later[0].search_text)  # context of the next one

    def test_changed_anchor_rebuilds_its_thread(self):
        self._edit(self.roots[7], "root 7 edited")
        inc = self._incremental()
        self.assertIn(self._thread_eid(self.roots[7]), inc)
        self.assertEquivalent(inc, self._full())

    def test_several_changes_at_once(self):
        self._edit(self.replies[self.roots[0].id][0], "a")
        self._edit(self.replies[self.roots[1].id][0], "b")
        self._msg("c", root=self.roots[2])
        inc = self._incremental()
        self.assertEqual(set(inc), {self._thread_eid(self.roots[i]) for i in range(3)})
        self.assertEquivalent(inc, self._full())

    def test_acl_dirty_channel_still_re_chunks_whole(self):
        self._edit(self.replies[self.roots[3].id][1], "edited")
        dirty = DirtyScopes(channel_ids=frozenset({str(self.pm.id)}))
        inc = self._incremental(dirty=dirty)
        self.assertEqual(inc, self._full())

    def test_nothing_changed_emits_nothing(self):
        self.assertEqual(self._incremental(), {})
