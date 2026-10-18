"""Tests for the search ACL refresh queue (MIP-722).

The bug these lock down: chunk ACLs are copied from `ProjectMembers` /
`ChannelMember` at index time, and the incremental reindexer only
revisits entities whose content timestamp moved. Removing a member
touched nothing it watched, so the removed member kept finding the
project's tasks, notes and chats in Spotlight, the agent and MCP for
as long as the content stayed unedited — with no full reindex
scheduled, indefinitely.

Layers, each tested on its own:

  * writers — the receivers and the two hand-placed marks;
  * the queue — `claim` / `release` and the best-effort mark;
  * the chunkers — a marked scope is re-emitted with `since` in the
    future (so nothing qualifies by timestamp), and a scope with no
    readers left is tombstoned rather than skipped;
  * the reindex command — marks are released only by a pass that
    refreshed every claimed scope.

OpenSearch and the embedder are never touched.
"""

import datetime as dt
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelMember, Message
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine import acl_dirty, ingestion
from origin.search_engine.acl_dirty import DirtyScopes
from origin.search_engine.chunkers.base import (
    CHAT_TYPE_DM,
    CHAT_TYPE_GM,
    CHAT_TYPE_PM,
    NOTE_TYPE_CHAT,
    NOTE_TYPE_TASK,
)
from origin.search_engine.chunkers.chat_chunker import iter_gm_chunks, iter_pm_chunks
from origin.search_engine.chunkers.milestone_chunker import iter_milestone_chunks
from origin.search_engine.chunkers.note_chunker import (
    iter_chat_note_chunks,
    iter_task_note_chunks,
)
from origin.search_engine.chunkers.note_summary_chunker import iter_note_summary_chunks
from origin.search_engine.chunkers.task_chunker import iter_task_chunks
from origin.search_engine.chunkers.thread_summary_chunker import iter_thread_summary_chunks
from origin.search_engine.management.commands import opensearch_reindex
from origin.search_engine.models import NoteSummary, RagChunk, SearchAclDirty, ThreadSummary
from origin.services import external_grants
from origin.services.team_membership import drop_in_team_access
from origin.tests.test_base import BaseAPITestCase


def _bn(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


def _marks(kind):
    return set(SearchAclDirty.objects.filter(scope_kind=kind).values_list("scope_id", flat=True))


# Nothing is newer than this, so only a marked scope can qualify.
FUTURE = timezone.now() + dt.timedelta(days=365)


# --------------------------------------------------------------------------- #
# Writers                                                                     #
# --------------------------------------------------------------------------- #


class TestMembershipReceivers(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="ACL Proj", owner=self.user
        )
        self.gm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_GM, title="g")
        SearchAclDirty.objects.all().delete()

    def test_adding_a_project_member_marks_the_project(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.assertEqual(_marks(SearchAclDirty.PROJECT), {str(self.project.project_id)})

    def test_removing_a_project_member_marks_the_project(self):
        pm = ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.user2
        )
        SearchAclDirty.objects.all().delete()
        pm.delete()
        self.assertEqual(_marks(SearchAclDirty.PROJECT), {str(self.project.project_id)})

    def test_queryset_delete_of_project_members_marks_the_project(self):
        # `QuerySet.delete()` sends post_delete per row; the member-removal
        # views use it, so this is the path production actually takes.
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        SearchAclDirty.objects.all().delete()
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        self.assertEqual(_marks(SearchAclDirty.PROJECT), {str(self.project.project_id)})

    def test_channel_member_soft_delete_by_save_marks_the_channel(self):
        cm = ChannelMember.objects.create(channel=self.gm, user=self.user2)
        SearchAclDirty.objects.all().delete()
        cm.is_deleted = True
        cm.save()
        self.assertEqual(_marks(SearchAclDirty.CHANNEL), {str(self.gm.id)})

    def test_channel_member_create_marks_the_channel(self):
        ChannelMember.objects.create(channel=self.gm, user=self.user2)
        self.assertEqual(_marks(SearchAclDirty.CHANNEL), {str(self.gm.id)})


class TestHandPlacedMarks(BaseAPITestCase):
    """The queryset `.update()` sites, which no receiver sees."""

    def test_drop_in_team_access_marks_every_channel_left(self):
        gm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_GM, title="g")
        dm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_DM)
        other = Channel.objects.create(team=self.team, kind=CHAT_TYPE_GM, title="not theirs")
        for ch in (gm, dm):
            ChannelMember.objects.create(channel=ch, user=self.user2)
        ChannelMember.objects.create(channel=other, user=self.user)
        SearchAclDirty.objects.all().delete()

        drop_in_team_access(self.team.team_id, self.user2.id)

        self.assertFalse(ChannelMember.objects.filter(user=self.user2, is_deleted=False).exists())
        self.assertEqual(_marks(SearchAclDirty.CHANNEL), {str(gm.id), str(dm.id)})

    def test_external_participant_removal_marks_the_channel(self):
        gm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_GM, title="g")
        ChannelMember.objects.create(channel=gm, user=self.user2)
        SearchAclDirty.objects.all().delete()
        grant = mock.Mock(object_type=external_grants.ExternalGrant.ObjectType.CHANNEL)
        grant.object_id = str(gm.id)

        removed = external_grants._remove_participant(grant, self.user2.id)

        self.assertEqual(removed, 1)
        self.assertEqual(_marks(SearchAclDirty.CHANNEL), {str(gm.id)})

    def test_external_removal_of_a_non_member_marks_nothing(self):
        gm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_GM, title="g")
        SearchAclDirty.objects.all().delete()
        grant = mock.Mock(object_type=external_grants.ExternalGrant.ObjectType.CHANNEL)
        grant.object_id = str(gm.id)

        self.assertEqual(external_grants._remove_participant(grant, self.user2.id), 0)
        self.assertFalse(SearchAclDirty.objects.exists())


# --------------------------------------------------------------------------- #
# The queue                                                                   #
# --------------------------------------------------------------------------- #


class TestClaimRelease(TestCase):
    def test_claim_reads_both_kinds(self):
        acl_dirty.mark_projects_dirty([7, 8])
        acl_dirty.mark_channels_dirty(["c-1"])
        scopes = acl_dirty.claim()
        self.assertEqual(scopes.project_ids, frozenset({7, 8}))
        self.assertEqual(scopes.channel_ids, frozenset({"c-1"}))
        self.assertEqual(len(scopes.rows), 3)
        self.assertTrue(scopes)

    def test_empty_queue_is_falsy(self):
        self.assertFalse(acl_dirty.claim())

    def test_marking_twice_keeps_one_row(self):
        acl_dirty.mark_projects_dirty([7])
        acl_dirty.mark_projects_dirty([7])
        self.assertEqual(SearchAclDirty.objects.count(), 1)

    def test_release_deletes_what_was_claimed(self):
        acl_dirty.mark_projects_dirty([7])
        scopes = acl_dirty.claim()
        self.assertEqual(acl_dirty.release(scopes), 1)
        self.assertFalse(SearchAclDirty.objects.exists())

    def test_a_mark_added_after_the_claim_survives_release(self):
        acl_dirty.mark_projects_dirty([7])
        scopes = acl_dirty.claim()
        acl_dirty.mark_channels_dirty(["c-1"])
        acl_dirty.release(scopes)
        self.assertEqual(_marks(SearchAclDirty.CHANNEL), {"c-1"})

    def test_a_scope_marked_again_mid_pass_survives_release(self):
        # The pass may have chunked this project before the second change.
        acl_dirty.mark_projects_dirty([7])
        scopes = acl_dirty.claim()
        SearchAclDirty.objects.update(ts_marked_at=timezone.now() - dt.timedelta(hours=1))
        scopes = acl_dirty.claim()
        acl_dirty.mark_projects_dirty([7])
        self.assertEqual(acl_dirty.release(scopes), 0)
        self.assertEqual(_marks(SearchAclDirty.PROJECT), {"7"})

    def test_release_batches_large_claims(self):
        acl_dirty.mark_channels_dirty([f"c-{i}" for i in range(acl_dirty._RELEASE_BATCH + 3)])
        self.assertEqual(acl_dirty.release(acl_dirty.claim()), acl_dirty._RELEASE_BATCH + 3)
        self.assertFalse(SearchAclDirty.objects.exists())

    def test_a_malformed_project_mark_is_dropped_not_fatal(self):
        SearchAclDirty.objects.create(scope_kind=SearchAclDirty.PROJECT, scope_id="nope")
        acl_dirty.mark_projects_dirty([7])
        scopes = acl_dirty.claim()
        self.assertEqual(scopes.project_ids, frozenset({7}))
        # Still claimed, so release clears it instead of tripping on it forever.
        self.assertEqual(len(scopes.rows), 2)


class TestMarkIsBestEffort(BaseAPITestCase):
    def test_a_failed_mark_does_not_lose_the_membership_change(self):
        project = ProjectMaster.objects.create(team=self.team, project_name="P", owner=self.user)
        # A real database error, so the savepoint is what is under test:
        # without it the error aborts the caller's transaction and the
        # membership write after it fails too.
        with connection.cursor() as cur:
            cur.execute(f'ALTER TABLE "{SearchAclDirty._meta.db_table}" RENAME TO acl_gone')
        try:
            with self.assertLogs("origin.search_engine.acl_dirty", level="ERROR"):
                ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
            self.assertTrue(
                ProjectMembers.objects.filter(project=project, attendee=self.user2).exists()
            )
        finally:
            with connection.cursor() as cur:
                cur.execute(f'ALTER TABLE acl_gone RENAME TO "{SearchAclDirty._meta.db_table}"')


# --------------------------------------------------------------------------- #
# Chunkers                                                                    #
# --------------------------------------------------------------------------- #


class TestProjectScopedChunkers(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Dirty Proj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.other = ProjectMaster.objects.create(
            team=self.team, project_name="Clean Proj", owner=self.user
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="Roadmap", status="open"
        )
        TaskMaster.objects.create(team=self.team, project=self.other, title="Other", status="open")
        # The user2 removal under test.
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        self.dirty = acl_dirty.claim()

    def test_the_removal_marked_only_its_project(self):
        self.assertEqual(self.dirty.project_ids, frozenset({self.project.project_id}))

    def test_task_chunker_re_emits_tasks_of_a_marked_project(self):
        batches = list(iter_task_chunks(since=FUTURE, dirty=self.dirty))
        self.assertEqual([b.entity_id for b in batches], [f"task:{self.task.task_id}"])
        acl = batches[0].chunks[0].acl_user_ids
        self.assertNotIn(str(self.user2.id), acl)

    def test_task_chunker_without_marks_emits_nothing(self):
        self.assertEqual(list(iter_task_chunks(since=FUTURE, dirty=DirtyScopes())), [])

    def test_milestone_chunker_re_emits_milestones_of_a_marked_project(self):
        ms = MilestoneMaster.objects.create(team=self.team, project=self.project, title="M1")
        MilestoneMaster.objects.create(team=self.team, project=self.other, title="M2")
        batches = list(iter_milestone_chunks(since=FUTURE, dirty=self.dirty))
        self.assertEqual([b.entity_id for b in batches], [f"milestone:{ms.milestone_id}"])
        self.assertNotIn(str(self.user2.id), batches[0].chunks[0].acl_user_ids)

    def test_task_note_chunker_re_emits_notes_of_a_marked_project(self):
        note = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            project=self.project,
            task=self.task,
            title="Plan",
            body=_bn("x"),
        )
        batches = list(iter_task_note_chunks(since=FUTURE, dirty=self.dirty))
        self.assertEqual([b.entity_id for b in batches], [f"note:task:{note.note_id}"])

    def test_pm_chat_of_a_marked_project_is_re_emitted(self):
        pm = Channel.objects.get(project=self.project, kind=CHAT_TYPE_PM)
        Message.objects.create(channel=pm, sender=self.user, seq=1, body=_bn("pm"))
        batches = list(iter_pm_chunks(since=FUTURE, dirty=self.dirty))
        self.assertEqual([b.entity_id for b in batches], [f"pm:{pm.id}"])
        self.assertEqual(batches[0].chunks[0].acl_user_ids, [str(self.user.id)])

    def test_task_note_summary_of_a_marked_project_is_re_emitted(self):
        note = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            project=self.project,
            task=self.task,
            title="Plan",
            body=_bn("x"),
        )
        NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=NOTE_TYPE_TASK,
            note_id=note.note_id,
            summary_text="s",
        )
        NoteSummary.objects.filter(note_id=note.note_id).update(
            ts_updated_at=timezone.now() - dt.timedelta(days=30)
        )
        batches = list(iter_note_summary_chunks(since=FUTURE, dirty=self.dirty))
        self.assertEqual(
            [b.entity_id for b in batches], [f"note_summary:{NOTE_TYPE_TASK}:{note.note_id}"]
        )


class TestChannelScopedChunkers(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.gm = Channel.objects.create(team=self.team, kind=CHAT_TYPE_GM, title="Dirty")
        self.clean = Channel.objects.create(team=self.team, kind=CHAT_TYPE_GM, title="Clean")
        for ch in (self.gm, self.clean):
            ChannelMember.objects.create(channel=ch, user=self.user)
            Message.objects.create(channel=ch, sender=self.user, seq=1, body=_bn("hello"))
        self.root = Message.objects.create(
            channel=self.gm, sender=self.user, seq=2, body=_bn("root")
        )
        Message.objects.create(
            channel=self.gm,
            sender=self.user,
            seq=3,
            body=_bn("reply"),
            is_thread_reply=True,
            thread_root=self.root,
        )
        self.member = ChannelMember.objects.create(channel=self.gm, user=self.user2)
        SearchAclDirty.objects.all().delete()
        self.member.is_deleted = True
        self.member.save()
        self.dirty = acl_dirty.claim()

    def test_gm_chunker_re_emits_only_the_marked_channel(self):
        batches = list(iter_gm_chunks(since=FUTURE, dirty=self.dirty))
        ids = {b.entity_id for b in batches}
        self.assertEqual(ids, {f"gm:{self.gm.id}", f"gm:{self.gm.id}:thread:{self.root.id}"})
        for b in batches:
            for c in b.chunks:
                self.assertEqual(c.acl_user_ids, [str(self.user.id)])

    def test_last_reader_leaving_tombstones_main_and_threads(self):
        ChannelMember.objects.filter(channel=self.gm, user=self.user).update(is_deleted=True)
        batches = list(iter_gm_chunks(since=FUTURE, dirty=self.dirty))
        self.assertEqual(
            {b.entity_id for b in batches},
            {f"gm:{self.gm.id}", f"gm:{self.gm.id}:thread:{self.root.id}"},
        )
        self.assertTrue(all(b.chunks == [] for b in batches))

    def test_an_unmarked_channel_with_no_readers_is_still_skipped(self):
        # The old defensive skip still holds outside the dirty set.
        ChannelMember.objects.filter(channel=self.clean).update(is_deleted=True)
        batches = list(iter_gm_chunks(dirty=DirtyScopes()))
        self.assertNotIn(f"gm:{self.clean.id}", {b.entity_id for b in batches})

    def test_chat_note_in_a_marked_channel_is_re_emitted(self):
        note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=CHAT_TYPE_GM,
            channel=self.gm,
            is_thread=False,
            title="Notes",
            body=_bn("x"),
        )
        ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=CHAT_TYPE_GM,
            channel=self.clean,
            is_thread=False,
            title="Other",
            body=_bn("x"),
        )
        batches = list(iter_chat_note_chunks(since=FUTURE, dirty=self.dirty))
        self.assertEqual([b.entity_id for b in batches], [f"note:chat:{note.note_id}"])
        self.assertNotIn(str(self.user2.id), batches[0].chunks[0].acl_user_ids)

    def test_chat_note_in_a_marked_projects_pm_channel_is_re_emitted(self):
        project = ProjectMaster.objects.create(team=self.team, project_name="P", owner=self.user)
        pm = Channel.objects.get(project=project, kind=CHAT_TYPE_PM)
        note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=CHAT_TYPE_PM,
            channel=pm,
            is_thread=False,
            title="PM notes",
            body=_bn("x"),
        )
        dirty = DirtyScopes(project_ids=frozenset({project.project_id}))
        batches = list(iter_chat_note_chunks(since=FUTURE, dirty=dirty))
        self.assertEqual([b.entity_id for b in batches], [f"note:chat:{note.note_id}"])

    def test_thread_summary_in_a_marked_channel_is_re_emitted(self):
        ts = ThreadSummary.objects.create(
            team_id=str(self.team.team_id),
            chat_type=CHAT_TYPE_GM,
            chat_id=self.gm.id,
            thread_id=self.root.id,
            summary_text="s",
        )
        ThreadSummary.objects.filter(pk=ts.pk).update(
            ts_updated_at=timezone.now() - dt.timedelta(days=30)
        )
        entity_id = f"thread_summary:{CHAT_TYPE_GM}:{self.gm.id}:{self.root.id}"
        batches = list(iter_thread_summary_chunks(since=FUTURE, dirty=self.dirty))
        self.assertEqual([b.entity_id for b in batches], [entity_id])
        self.assertEqual(batches[0].chunks[0].acl_user_ids, [str(self.user.id)])

        # ...and tombstoned once nobody can read the chat.
        ChannelMember.objects.filter(channel=self.gm, user=self.user).update(is_deleted=True)
        batches = list(iter_thread_summary_chunks(since=FUTURE, dirty=self.dirty))
        self.assertEqual([(b.entity_id, b.chunks) for b in batches], [(entity_id, [])])

    def test_chat_note_summary_tombstoned_when_nobody_can_read_it(self):
        note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=None,
            chat_type=CHAT_TYPE_GM,
            channel=self.gm,
            is_thread=False,
            title="Notes",
            body=_bn("x"),
        )
        NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=NOTE_TYPE_CHAT,
            note_id=note.note_id,
            summary_text="s",
        )
        ChannelMember.objects.filter(channel=self.gm, user=self.user).update(is_deleted=True)
        entity_id = f"note_summary:{NOTE_TYPE_CHAT}:{note.note_id}"
        # Both modes: a full pass releases the marks too.
        for since in (FUTURE, None):
            batches = list(iter_note_summary_chunks(since=since, dirty=self.dirty))
            self.assertEqual([(b.entity_id, b.chunks) for b in batches], [(entity_id, [])])


# --------------------------------------------------------------------------- #
# End to end through ingestion                                                #
# --------------------------------------------------------------------------- #


class _IngestionHarness(BaseAPITestCase):
    """Real `_ingest_entity` routing; OpenSearch and the embedder mocked."""

    def setUp(self):
        super().setUp()
        patches = [
            mock.patch.object(
                ingestion, "embed_texts", side_effect=lambda texts: [[0.1] * 8 for _ in texts]
            ),
            mock.patch.object(ingestion, "_bulk_index"),
            mock.patch.object(
                ingestion, "get_active_embedding_model_name", return_value="test-embed-v1"
            ),
            mock.patch.object(ingestion, "get_index_alias", return_value="rag-test"),
            mock.patch.object(ingestion, "get_client"),
            mock.patch.object(ingestion.os_helpers, "bulk", return_value=(0, [])),
        ]
        started = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.embed, self.bulk = started[0], started[1]

        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="E2E", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Secret plan",
            content=_bn("launch details"),
            status="open",
        )
        ingestion.ingest_all(entity_types=["task"])
        self.embed.reset_mock()
        self.bulk.reset_mock()
        SearchAclDirty.objects.all().delete()

    def _ops(self):
        return [a for call in self.bulk.call_args_list for a in call.args[0]]

    def _row_acl(self):
        return RagChunk.objects.get(chunk_id=f"task:{self.task.task_id}:title_content")


class TestIncrementalPassRewritesAcl(_IngestionHarness):
    def test_removed_member_is_dropped_without_re_embedding(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        stats = ingestion.ingest_all(since=FUTURE, entity_types=["task"], dirty=acl_dirty.claim())
        self.assertEqual(stats.errors, [])
        self.assertEqual(stats.chunks_metadata_only, 1)
        self.embed.assert_not_called()
        (op,) = self._ops()
        self.assertEqual(op["_op_type"], "update")
        self.assertEqual(op["doc"]["acl_user_ids"], [str(self.user.id)])

    def test_without_the_mark_the_stale_acl_stays(self):
        # The original bug, kept as the control for the test above.
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        SearchAclDirty.objects.all().delete()
        ingestion.ingest_all(since=FUTURE, entity_types=["task"], dirty=acl_dirty.claim())
        self.assertEqual(self._ops(), [])


class TestReindexCommandReleasesMarks(_IngestionHarness):
    def setUp(self):
        super().setUp()
        patches = [
            mock.patch.object(opensearch_reindex, "get_client"),
            mock.patch.object(opensearch_reindex, "sweep_orphans", return_value={}),
            mock.patch.object(opensearch_reindex.Command, "_warn_on_schema_mismatch"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # Outside the `--since-minutes` window, so only the mark brings it back.
        TaskMaster.objects.filter(pk=self.task.pk).update(
            ts_updated_at=timezone.now() - dt.timedelta(days=30)
        )
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()

    def _run(self, *args):
        call_command("opensearch_reindex", "--since-minutes", "1", *args, stdout=mock.Mock())

    def test_a_clean_incremental_pass_refreshes_and_releases(self):
        self._run()
        self.assertFalse(SearchAclDirty.objects.exists())
        ops = [o for o in self._ops() if o["_id"] == f"task:{self.task.task_id}:title_content"]
        self.assertEqual([o["doc"]["acl_user_ids"] for o in ops], [[str(self.user.id)]])

    def test_a_pass_with_errors_keeps_the_marks(self):
        failing = ingestion.IngestionStats()
        failing.errors.append("task/task:1: boom")
        with mock.patch.object(opensearch_reindex, "ingest_all", return_value=failing):
            # The ingestion error is logged at ERROR by ingestion itself in
            # production; here it is only the stats, so the command exits 0.
            self._run()
        self.assertEqual(_marks(SearchAclDirty.PROJECT), {str(self.project.project_id)})

    def test_a_dry_run_keeps_the_marks(self):
        self._run("--dry-run")
        self.assertTrue(SearchAclDirty.objects.exists())

    def test_an_entity_type_subset_keeps_the_marks(self):
        self._run("--entity-types", "task")
        self.assertTrue(SearchAclDirty.objects.exists())

    def test_a_full_pass_also_releases(self):
        call_command("opensearch_reindex", stdout=mock.Mock())
        self.assertFalse(SearchAclDirty.objects.exists())


class TestMayRelease(TestCase):
    def _scopes(self):
        return DirtyScopes(project_ids=frozenset({1}), rows=((1, timezone.now()),))

    def test_needs_every_acl_type(self):
        stats = ingestion.IngestionStats()
        everything = sorted(opensearch_reindex._ACL_ENTITY_TYPES)
        self.assertTrue(
            opensearch_reindex._may_release(self._scopes(), stats, {"entity_types": everything})
        )
        for missing in everything:
            subset = [t for t in everything if t != missing]
            self.assertFalse(
                opensearch_reindex._may_release(self._scopes(), stats, {"entity_types": subset}),
                missing,
            )

    def test_nothing_claimed_means_nothing_to_release(self):
        self.assertFalse(
            opensearch_reindex._may_release(DirtyScopes(), ingestion.IngestionStats(), {})
        )
