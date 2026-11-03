"""@/# mentions narrow the audience of a collected answer (MIP-980).

When an ask carries structured mentions, the server resolves them for the ASKER
and appends a "USER-PROVIDED REFERENCES" block with each mentioned object's
canonical title to the system prompt, and persists them on `AgentRun.mentions`.
A mention is not a tool result, so it is no "source": the audience of a
collected answer (the intersection of its sources' audiences) never covered it.
A first-turn run that was handed a PRIVATE note by mention, whose answer quoted
its title and which also read one team-visible object, was collected for that
object's wider audience and shown anonymously to teammates who cannot see the
note. Same class as MIP-978 (scoped asks), at title level.

The fix is in the one rule, `shared_answers.judge_run`: the persisted mentions
are audited like sources the run had read. These tests pin it per mention kind
and for every consumer of the rule (the chunker, the "answers shared with your
team" list, the re-collection after an opt-in), plus the command that applies it
to answers collected before. No LLM and no OpenSearch: same in-memory index as
test_shared_answers.
"""

import dataclasses
import json
from datetime import date
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMembers
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine import purge, shared_answers
from origin.search_engine.agent.mentions import (
    mention_audit_sources,
    parse_mentions,
    resolve_mentions,
)
from origin.search_engine.agent.source_visibility import shareable_acl_for_run
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.chunkers.spotlight_answer_chunker import (
    iter_spotlight_answer_chunks,
    iter_spotlight_answer_chunks_for_runs,
)
from origin.search_engine.ingestion import _meta_fingerprint, ingest_entity_stream
from origin.search_engine.management.commands import rejudge_team_answers as command
from origin.search_engine.models import AgentRun, RagChunk
from origin.search_engine.shared_answers import COLLECT, SKIP, WITHHELD, judge_run

from .test_shared_answers import SHARED_URL, SharedAnswersTestCase

User = get_user_model()

PRIVATE_TITLE = "PRIVATE-NOTE-TITLE: Q3 reorg shortlist"


class MentionsTestCase(SharedAnswersTestCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=self.tid, user_id=self.uid)
        self.both = sorted([self.uid, self.uid2])
        # A third teammate, who is in neither the team-visible project nor the
        # asker's private objects.
        self.user3 = User.objects.create_user(
            username="thirduser", email="third@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        self.uid3 = str(self.user3.id)
        # A private note: owner only, no folder, no grants.
        self.private_note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title=PRIVATE_TITLE, body=[]
        )

    # ---- builders ---------------------------------------------------------

    def mentioned_run(self, entries, **kwargs):
        """A first-turn standalone run that read the team-visible task and was
        handed `entries` by mention. The persisted JSON comes from the REAL
        resolver (`as_json`), so a key the rule reads wrongly cannot pass."""
        resolved = resolve_mentions(parse_mentions(entries), self.ctx)
        self.assertEqual(len(resolved), len(entries), "a mention failed to resolve")
        run = self.make_run(None, **self._run_kwargs(kwargs))
        AgentRun.objects.filter(pk=run.pk).update(mentions=[m.as_json() for m in resolved])
        run.refresh_from_db()
        return run

    def raw_mentions_run(self, raw, **kwargs):
        """The same run with `AgentRun.mentions` set to `raw`, whatever it is."""
        run = self.make_run(None, **self._run_kwargs(kwargs))
        AgentRun.objects.filter(pk=run.pk).update(mentions=raw)
        run.refresh_from_db()
        return run

    @staticmethod
    def _run_kwargs(kwargs):
        kwargs.setdefault("query", "What is on the shortlist?")
        kwargs.setdefault("answer", f"It is the note {PRIVATE_TITLE!r}. See [task:1].")
        return kwargs

    def judge(self, run):
        run.refresh_from_db()
        return judge_run(run, opted_out=set())

    def audience(self, run):
        verdict = self.judge(run)
        self.assertEqual(verdict.state, COLLECT)
        return verdict.acl

    def doc_acl(self, run):
        return sorted(self.index.docs[f"spotlight_answer:{run.run_id}"]["acl_user_ids"])

    def team_docs(self):
        return {
            eid.split(":", 1)[1] for eid in self.index.docs if eid.startswith("spotlight_answer:")
        }

    def listed_ids(self):
        res = self.client.get(SHARED_URL)
        self.assertEqual(res.status_code, 200)
        return {a["run_id"] for a in res.data["answers"]}

    # ---- mentionable objects ----------------------------------------------

    def note_entry(self, note):
        return {"type": "note", "note_type": 1, "note_id": note.note_id}

    def team_folder_note(self):
        """A note in a PUBLIC Team Notes folder: its index ACL is the owner plus
        a `team:<id>` sentinel, which the intersection has to expand."""
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Handbook",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )
        return PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Handbook", body=[], folder_id=folder.folder_id
        )

    def private_project(self):
        """A project only the asker belongs to."""
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Private Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.get_or_create(project=project, attendee=self.user)
        return project

    def channel(self, kind, members):
        channel = Channel.objects.create(team=self.team, kind=kind, title="a chat")
        for user in members:
            ChannelMember.objects.create(channel=channel, user=user)
        return channel

    def chat_entry(self, channel):
        return {"type": "chat", "chat_type": int(channel.kind), "chat_id": str(channel.id)}

    def todo(self):
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=date(2026, 11, 3)
        )
        return ToDoItem.objects.create(group=group, title="Tell Dana about the layoffs")

    def mention_group(self):
        group = MentionGroupMaster.objects.create(
            team=self.team, group_name="design-crew", created_by=self.user
        )
        for user in (self.user, self.user2):
            MentionGroupMembers.objects.create(
                team=self.team, group=group, user=user, added_by=self.user
            )
        return group

    def guest(self):
        """An external collaborator: a project member with NO `TeamMembers` row."""
        guest = User.objects.create_user(username="guest", email="guest@example.com", password="x")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=guest)
        return guest


# ---------------------------------------------------------------------------
# The leak, reproduced
# ---------------------------------------------------------------------------


class ReproductionTests(MentionsTestCase):
    """Both failed on main, before the fix:
    `AssertionError: '<teammate>' unexpectedly found in ['<asker>', '<teammate>']`."""

    def test_a_private_note_mention_does_not_leak_its_title_to_a_teammate(self):
        run = self.mentioned_run([self.note_entry(self.private_note)])

        verdict = self.judge(run)

        self.assertEqual(verdict.state, COLLECT)
        self.assertNotIn(self.uid2, verdict.acl)

    def test_no_chunk_carries_the_private_title_to_a_teammate(self):
        self.mentioned_run([self.note_entry(self.private_note)])

        leaked = [
            chunk
            for entity in iter_spotlight_answer_chunks()
            for chunk in entity.chunks
            if PRIVATE_TITLE in chunk.answer_text and self.uid2 in chunk.acl_user_ids
        ]

        self.assertEqual(leaked, [])


# ---------------------------------------------------------------------------
# The rule, per mention kind
# ---------------------------------------------------------------------------


class NoteMentionTests(MentionsTestCase):
    def test_a_private_note_narrows_the_audience_to_its_readers(self):
        run = self.mentioned_run([self.note_entry(self.private_note)])

        self.assertEqual(self.audience(run), [self.uid])

    def test_a_team_visible_note_leaves_the_audience_alone(self):
        run = self.mentioned_run([self.note_entry(self.team_folder_note())])

        self.assertEqual(self.audience(run), self.both)

    def test_a_task_note_follows_its_projects_members(self):
        private = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.private_project(),
            task=self.task,
            title="n",
            owner=self.user,
        )
        shared = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=self.task, title="n", owner=self.user
        )
        narrow = self.mentioned_run([{"type": "note", "note_type": 2, "note_id": private.note_id}])
        wide = self.mentioned_run([{"type": "note", "note_type": 2, "note_id": shared.note_id}])

        self.assertEqual(self.audience(narrow), [self.uid])
        self.assertEqual(self.audience(wide), self.both)

    def test_a_note_deleted_since_is_withheld(self):
        run = self.mentioned_run([self.note_entry(self.private_note)])
        self.private_note.delete()

        self.assertEqual(self.judge(run).state, WITHHELD)


class TaskMentionTests(MentionsTestCase):
    def test_a_private_task_narrows_and_a_team_visible_one_does_not(self):
        private = TaskMaster.objects.create(
            team=self.team, title="Layoffs", assignee=self.user, reporter=self.user
        )
        narrow = self.mentioned_run([{"type": "task", "task_id": private.task_id}])
        wide = self.mentioned_run([{"type": "task", "task_id": self.task.task_id}])

        self.assertEqual(self.audience(narrow), [self.uid])
        self.assertEqual(self.audience(wide), self.both)


class ProjectMentionTests(MentionsTestCase):
    def test_a_private_project_narrows_and_a_public_one_does_not(self):
        private = self.private_project()
        narrow = self.mentioned_run([{"type": "project", "project_id": private.project_id}])
        wide = self.mentioned_run([{"type": "project", "project_id": self.project.project_id}])

        self.assertEqual(self.audience(narrow), [self.uid])
        self.assertEqual(self.audience(wide), self.both)


class ChatMentionTests(MentionsTestCase):
    def test_a_dm_and_a_private_channel_narrow(self):
        dm = self.channel(ChannelKind.DM, [self.user, self.user3])
        gm = self.channel(ChannelKind.GM, [self.user, self.user3])

        for channel in (dm, gm):
            with self.subTest(kind=channel.kind):
                run = self.mentioned_run([self.chat_entry(channel)])
                self.assertEqual(self.audience(run), [self.uid])

    def test_a_channel_everyone_on_the_run_can_read_does_not(self):
        everyone = self.channel(ChannelKind.GM, [self.user, self.user2, self.user3])
        pm = Channel.objects.filter(kind=ChannelKind.PM, project=self.project).first()
        if pm is None:
            pm = Channel.objects.create(
                team=self.team, kind=ChannelKind.PM, project=self.project, title="pm"
            )

        for channel in (everyone, pm):
            with self.subTest(kind=channel.kind):
                run = self.mentioned_run([self.chat_entry(channel)])
                self.assertEqual(self.audience(run), self.both)


class TodoMentionTests(MentionsTestCase):
    """The audience logic already says a to-do is the asker's alone
    (`source_acl_user_ids`: `{asker}`), and a fetched to-do has always been
    collected on that basis: the verdict is COLLECT for the asker only, which
    leaks nothing. (A tombstone would purge the answer; this keeps it for its
    owner.)"""

    def test_a_todo_mention_makes_the_answer_the_askers_alone(self):
        item = self.todo()
        run = self.mentioned_run(
            [{"type": "todo", "item_id": item.item_id}],
            answer="You wrote 'Tell Dana about the layoffs'. See [task:1].",
        )

        self.assertEqual(self.audience(run), [self.uid])
        chunks = [c for e in iter_spotlight_answer_chunks() for c in e.chunks]
        self.assertEqual([c.acl_user_ids for c in chunks], [[self.uid]])
        self.assertNotIn(self.uid2, chunks[0].acl_user_ids)


class NameMentionTests(MentionsTestCase):
    """A user and a mention group are names the whole team can see, so they
    narrow nothing among team members. Evidence: `_resolve_group` ("tenancy is
    the whole ACL", `MentionGroupMaster` has no visibility column) and
    `_resolve_user` (a team membership is all it needs); `source_visibility`
    has no `user` / `group` source at all, so adding one as a source would
    fail closed and withhold every such run."""

    def test_a_user_mention_does_not_narrow(self):
        run = self.mentioned_run([{"type": "user", "user_id": self.uid2}])

        self.assertEqual(self.audience(run), self.both)

    def test_a_group_mention_does_not_narrow(self):
        run = self.mentioned_run([{"type": "group", "group_id": self.mention_group().group_id}])

        self.assertEqual(self.audience(run), self.both)

    def test_a_name_is_not_shown_to_a_guest_who_cannot_see_the_directory(self):
        # A guest reaches the shared project's task through ProjectMembers, but
        # has no TeamMembers row and is refused the roster and mention groups.
        guest = self.guest()
        group = self.mention_group()
        plain = self.make_run(None, query="plain")
        by_user = self.mentioned_run([{"type": "user", "user_id": self.uid2}])
        by_group = self.mentioned_run([{"type": "group", "group_id": group.group_id}])

        self.assertEqual(self.audience(plain), sorted([self.uid, self.uid2, str(guest.id)]))
        self.assertEqual(self.audience(by_user), self.both)
        self.assertEqual(self.audience(by_group), self.both)


class MalformedMentionTests(MentionsTestCase):
    def test_what_cannot_be_classified_is_withheld(self):
        good = {"kind": "task", "label": "T", "task_id": self.task.task_id}
        cases = {
            "unknown kind": [{"kind": "wormhole", "label": "x", "id": 1}],
            "no kind": [{"label": "x", "task_id": self.task.task_id}],
            "kind not a string": [{"kind": ["task"], "task_id": self.task.task_id}],
            "not a dict": ["task:1"],
            "None entry": [None],
            "task without id": [{"kind": "task", "label": "T"}],
            "task with a blank id": [{"kind": "task", "task_id": " "}],
            "task with a junk id": [{"kind": "task", "task_id": "abc"}],
            "note without id": [{"kind": "note", "note_type_label": "personal"}],
            "note with an unknown type": [
                {"kind": "note", "note_type_label": "wiki", "note_id": 1}
            ],
            "chat without id": [{"kind": "chat", "chat_type_label": "dm"}],
            "chat with an unknown type": [
                {"kind": "chat", "chat_type_label": "fax", "chat_id": "x"}
            ],
            "project without id": [{"kind": "project", "label": "P"}],
            "todo without id": [{"kind": "todo", "label": "t"}],
            "user without id": [{"kind": "user", "label": "Dana"}],
            "group without id": [{"kind": "group", "label": "design-crew"}],
            "one bad among good": [good, {"kind": "wormhole"}],
            "the bad one first": [{"kind": "wormhole"}, good],
            "a dict, not a list": {"kind": "task", "task_id": self.task.task_id},
            "a string": "task",
            "a number": 7,
        }
        for name, raw in cases.items():
            with self.subTest(name):
                run = self.raw_mentions_run(raw)
                self.assertEqual(self.judge(run).state, WITHHELD)
        # The control: the same shape, well formed, is collected.
        self.assertEqual(self.judge(self.raw_mentions_run([good])).state, COLLECT)

    def test_the_classifier_says_none_for_them(self):
        self.assertIsNone(mention_audit_sources([{"kind": "wormhole"}], team_id=self.tid))
        self.assertIsNone(mention_audit_sources({"kind": "task"}, team_id=self.tid))
        self.assertEqual(mention_audit_sources(None, team_id=self.tid), [])
        self.assertEqual(mention_audit_sources([], team_id=self.tid), [])


class CombinationTests(MentionsTestCase):
    def test_every_mention_counts_not_just_the_first_or_the_last(self):
        visible = {"type": "task", "task_id": self.task.task_id}
        private = self.note_entry(self.private_note)
        project = {"type": "project", "project_id": self.project.project_id}

        for name, entries in {
            "private last": [visible, project, private],
            "private first": [private, visible, project],
            "private in the middle": [visible, private, project],
        }.items():
            with self.subTest(name):
                self.assertEqual(self.audience(self.mentioned_run(entries)), [self.uid])

    def test_a_mention_the_run_also_read_is_still_audited(self):
        # The run fetched `self.task` AND was handed the private note.
        run = self.mentioned_run(
            [{"type": "task", "task_id": self.task.task_id}, self.note_entry(self.private_note)]
        )

        self.assertEqual(self.audience(run), [self.uid])

    def test_two_private_objects_of_different_audiences_leave_their_overlap(self):
        a = self.channel(ChannelKind.GM, [self.user, self.user2])  # asker + teammate
        b = self.channel(ChannelKind.GM, [self.user, self.user3])  # asker + other
        both_run = self.mentioned_run([self.chat_entry(a), self.chat_entry(b)])
        only_a = self.mentioned_run([self.chat_entry(a)])

        self.assertEqual(self.audience(both_run), [self.uid])
        self.assertEqual(self.audience(only_a), self.both)

    def test_the_verdict_keeps_the_tool_sources_as_the_chunks_provenance(self):
        run = self.mentioned_run([self.note_entry(self.private_note)])

        verdict = self.judge(run)

        self.assertEqual([s["entity_id"] for s in verdict.sources], [f"task:{self.task.task_id}"])


class UnchangedRulesTests(MentionsTestCase):
    def test_a_run_without_mentions_is_unchanged(self):
        plain = self.make_run(None, query="plain")
        empty = self.raw_mentions_run([], query="empty")
        null_like = self.make_run(None, query="default")

        for run in (plain, empty, null_like):
            verdict = self.judge(run)
            self.assertEqual(verdict.state, COLLECT)
            self.assertEqual(verdict.acl, self.both)
            self.assertEqual(
                [s["entity_id"] for s in verdict.sources], [f"task:{self.task.task_id}"]
            )

    def test_a_mention_alone_never_makes_a_run_collectable(self):
        # No tool source: withheld before and after. A mention must not widen
        # what is collected (the next full reindex would create new chunks).
        run = self.mentioned_run([self.note_entry(self.team_folder_note())], cites_task=False)

        self.assertEqual(self.judge(run).state, WITHHELD)

    def test_the_other_rules_still_come_first(self):
        item = self.todo()
        entry = {"type": "todo", "item_id": item.item_id}
        scoped = self.mentioned_run([entry], query="scoped")
        AgentRun.objects.filter(pk=scoped.pk).update(
            session=self.make_session(note_type=1, note_id=4242)
        )
        first = self.mentioned_run([entry], query="first")
        session = self.make_session()
        AgentRun.objects.filter(pk=first.pk).update(session=session)
        follow_up = self.mentioned_run([entry], query="second")
        AgentRun.objects.filter(pk=follow_up.pk).update(session=session)
        errored = self.mentioned_run([entry], query="errored", status="error")
        withdrawn = self.mentioned_run([entry], query="withdrawn")
        AgentRun.objects.filter(pk=withdrawn.pk).update(withheld_from_team_answers=True)

        self.assertEqual(self.judge(scoped).state, WITHHELD)  # MIP-978
        self.assertEqual(self.judge(first).state, COLLECT)
        self.assertEqual(self.judge(follow_up).state, SKIP)
        self.assertEqual(self.judge(errored).state, SKIP)
        self.assertEqual(self.judge(withdrawn).state, WITHHELD)
        withdrawn.refresh_from_db()
        self.assertEqual(judge_run(withdrawn, opted_out={self.uid}).state, WITHHELD)

    def test_shareable_acl_for_run_agrees_with_judge_run(self):
        runs = [
            self.mentioned_run([self.note_entry(self.private_note)]),
            self.mentioned_run([{"type": "user", "user_id": self.uid2}]),
            self.raw_mentions_run([{"kind": "wormhole"}]),
            self.make_run(None),
        ]
        for run in runs:
            run.refresh_from_db()
            self.assertEqual(shareable_acl_for_run(run), judge_run(run, opted_out=set()).acl)


# ---------------------------------------------------------------------------
# Every consumer of the rule
# ---------------------------------------------------------------------------


class ConsumerTests(MentionsTestCase):
    def history(self):
        self.private = self.mentioned_run([self.note_entry(self.private_note)], query="private")
        self.wide = self.mentioned_run(
            [{"type": "user", "user_id": self.uid2}], query="a name only"
        )
        self.unknown = self.raw_mentions_run([{"kind": "wormhole"}], query="unknown")
        self.plain = self.make_run(None, query="plain")

    def test_the_chunker_narrows_withholds_and_leaves_the_rest(self):
        self.history()

        chunks = {
            e.entity_id.split(":", 1)[1]: e.chunks[0] if e.chunks else None
            for e in iter_spotlight_answer_chunks()
        }

        self.assertEqual(chunks[str(self.private.run_id)].acl_user_ids, [self.uid])
        self.assertEqual(chunks[str(self.wide.run_id)].acl_user_ids, self.both)
        self.assertIsNone(chunks[str(self.unknown.run_id)])  # a tombstone
        self.assertEqual(chunks[str(self.plain.run_id)].acl_user_ids, self.both)

    def test_the_list_is_exactly_what_the_chunker_collects(self):
        self.history()
        # A teammate's run with a private mention of THEIR own: not in my list.
        mine = self.collected(self.user)

        self.assertEqual(self.listed_ids(), mine)
        self.assertEqual(mine, {str(r.run_id) for r in (self.private, self.wide, self.plain)})

    def test_the_list_goes_through_judge_run(self):
        self.history()
        with mock.patch.object(
            shared_answers, "judge_run", wraps=shared_answers.judge_run
        ) as judge:
            self.assertNotIn(str(self.unknown.run_id), self.listed_ids())
        self.assertEqual(judge.call_count, 4)

    def test_the_re_collection_honours_the_mentions(self):
        private = self.mentioned_run(
            [self.note_entry(self.private_note)], query="private", index=False
        )
        unknown = self.raw_mentions_run([{"kind": "wormhole"}], query="unknown", index=False)
        plain = self.make_run(None, query="plain", index=False)

        shared_answers.reshare_team_lane_for_user(self.uid)

        self.assertEqual(self.team_docs(), {str(private.run_id), str(plain.run_id)})
        self.assertNotIn(str(unknown.run_id), self.team_docs())
        self.assertEqual(self.doc_acl(private), [self.uid])
        self.assertEqual(self.doc_acl(plain), self.both)

    def test_a_reindex_pass_narrows_a_copy_collected_before_the_rule(self):
        run = self.mentioned_run([self.note_entry(self.private_note)])  # stand-in: both
        self.assertEqual(self.doc_acl(run), self.both)

        from origin.search_engine import ingestion

        ingestion.ingest_all(entity_types=["spotlight_answer"])

        self.assertEqual(self.doc_acl(run), [self.uid])

    def test_the_sweep_cannot_re_audit_mentions(self):
        # `still_shareable` is the DB-only half of the rule. Said plainly so a
        # change of that is a decision: it keeps a mention run, which is why
        # `rejudge_team_answers` exists.
        run = self.mentioned_run([self.note_entry(self.private_note)])

        purge.sweep_orphans()

        self.assertIn(str(run.run_id), self.team_docs())
        self.assertEqual(self.doc_acl(run), self.both)


# ---------------------------------------------------------------------------
# python manage.py rejudge_team_answers
# ---------------------------------------------------------------------------


class CommandTests(MentionsTestCase):
    def setUp(self):
        super().setUp()
        self.client_mock = mock.MagicMock()
        self.client_mock.ping.return_value = True
        patcher = mock.patch.object(command, "get_client", return_value=self.client_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def collected_before_the_rule(self, run):
        """Index `run` as a reindex did before MIP-980: the real chunk, built
        with its mentions not yet persisted (so a wide audience), with its real
        hashes in `RagChunk`."""
        mentions = run.mentions
        AgentRun.objects.filter(pk=run.pk).update(mentions=[])
        run.refresh_from_db()
        stats = ingest_entity_stream(iter_spotlight_answer_chunks_for_runs([run]))
        self.assertEqual(stats.errors, [])
        AgentRun.objects.filter(pk=run.pk).update(mentions=mentions)
        run.refresh_from_db()

    def history(self):
        """Runs as they stood in the index before the rule, with mentions now
        persisted. `index=False`: the chunks are the real ones."""
        self.private = self.mentioned_run(
            [self.note_entry(self.private_note)], query="private", index=False
        )
        self.todo_run = self.mentioned_run(
            [{"type": "todo", "item_id": self.todo().item_id}], query="todo", index=False
        )
        self.visible = self.mentioned_run(
            [{"type": "note", "note_type": 1, "note_id": self.team_folder_note().note_id}],
            query="visible",
            index=False,
        )
        self.name = self.mentioned_run(
            [{"type": "user", "user_id": self.uid2}], query="name", index=False
        )
        self.unknown = self.raw_mentions_run([{"kind": "wormhole"}], query="unknown", index=False)
        self.plain = self.make_run(None, query="plain", index=False)
        self.untracked = self.mentioned_run(
            [self.note_entry(self.private_note)], query="never collected", index=False
        )
        for run in (
            self.private,
            self.todo_run,
            self.visible,
            self.name,
            self.unknown,
            self.plain,
        ):
            self.collected_before_the_rule(run)
        self.bulk.reset_mock()
        self.embed.reset_mock()
        self.edges = {
            run.run_id: dict(self.index.docs[f"spotlight_answer:{run.run_id}"])
            for run in (self.private, self.visible, self.plain)
        }

    def run_command(self, *args):
        out = StringIO()
        call_command("rejudge_team_answers", *args, stdout=out)
        text = out.getvalue()
        return text, json.loads(text[: text.rindex("}") + 1])

    EXPECTED_DRY_RUN = {
        "mode": "dry-run",
        "runs_with_mentions": 6,  # private, todo, visible, name, unknown, untracked
        "tracked_team_chunks": 5,  # all but `untracked`
        "to_purge": 1,  # unknown kind
        "to_rewrite": 2,  # private note, to-do: the stored ACL is wider
        "audience_narrowed_by_mentions": 2,
        "unchanged": 2,  # team-visible note, user name
    }

    def test_it_is_a_dry_run_by_default_and_prints_counts_only(self):
        self.history()
        before = {k: dict(v) for k, v in self.index.docs.items()}

        text, report = self.run_command()

        self.assertEqual(report, self.EXPECTED_DRY_RUN)
        self.assertEqual(self.run_command("--dry-run")[1], report)
        # Nothing written or deleted, the index never contacted.
        self.bulk.assert_not_called()
        self.embed.assert_not_called()
        self.client_mock.ping.assert_not_called()
        self.assertEqual(self.index.docs, before)
        # Counts only: no id, no question, no answer, no title, no user.
        for run in AgentRun.objects.all():
            self.assertNotIn(str(run.run_id), text)
            self.assertNotIn(run.query, text)
        for secret in (PRIVATE_TITLE, "layoffs", "Handbook", self.uid, self.uid2, "Dana"):
            self.assertNotIn(secret, text)

    def test_apply_narrows_purges_and_leaves_everything_else(self):
        self.history()
        wide_before = self.doc_acl(self.private)
        self.assertEqual(wide_before, self.both)

        text, report = self.run_command("--apply")

        self.assertEqual(
            report,
            {
                **self.EXPECTED_DRY_RUN,
                "mode": "apply",
                "to_purge_remaining": 0,
                "to_rewrite_remaining": 0,
                "write_errors": 0,
            },
        )
        self.client_mock.ping.assert_called()
        # Narrowed in place: the private note's answer and the to-do's.
        self.assertEqual(self.doc_acl(self.private), [self.uid])
        self.assertEqual(self.doc_acl(self.todo_run), [self.uid])
        # Purged: the unclassifiable one (document and tracking row).
        self.assertNotIn(f"spotlight_answer:{self.unknown.run_id}", self.index.docs)
        self.assertFalse(
            RagChunk.objects.filter(entity_id=f"spotlight_answer:{self.unknown.run_id}").exists()
        )
        # Untouched: byte for byte, the team-visible, the name-only and the plain.
        for run in (self.visible, self.plain):
            self.assertEqual(
                self.index.docs[f"spotlight_answer:{run.run_id}"], self.edges[run.run_id]
            )
        self.assertTrue(
            RagChunk.objects.filter(entity_id=f"spotlight_answer:{self.name.run_id}").exists()
        )
        # The narrowing is a metadata update: the text did not change, so
        # nothing was embedded (no LLM / embedding call), and the private
        # `conversation` lane was never in play.
        self.embed.assert_not_called()
        self.assertNotIn(PRIVATE_TITLE, text)

    def test_the_stored_acl_changes_the_chunks_fingerprint(self):
        # Why a metadata update is enough: `acl_user_ids` is part of the
        # fingerprint ingestion compares, so the narrowed chunk is rewritten.
        run = self.mentioned_run([self.note_entry(self.private_note)], index=False)
        narrowed = next(iter_spotlight_answer_chunks_for_runs([run])).chunks[0]
        wide = dataclasses.replace(narrowed, acl_user_ids=self.both)

        self.assertEqual(narrowed.acl_user_ids, [self.uid])
        self.assertNotEqual(_meta_fingerprint(narrowed), _meta_fingerprint(wide))

    def test_it_is_idempotent(self):
        self.history()
        self.run_command("--apply")
        self.bulk.reset_mock()

        _, again = self.run_command("--apply")
        _, dry = self.run_command()

        for report in (again, dry):
            self.assertEqual(report["to_purge"], 0)
            self.assertEqual(report["to_rewrite"], 0)
            self.assertEqual(report["tracked_team_chunks"], 4)
            self.assertEqual(report["unchanged"], 4)
        self.bulk.assert_not_called()
        self.client_mock.ping.assert_called_once()  # only the first apply had work

    def test_runs_without_mentions_are_never_looked_at(self):
        self.history()
        # Their stored chunk is wide and would be "narrower" if anyone judged it.
        empty = self.raw_mentions_run([], query="empty list", index=False)
        self.collected_before_the_rule(empty)
        self.bulk.reset_mock()

        _, report = self.run_command("--apply")

        self.assertEqual(report["runs_with_mentions"], 6)
        self.assertIn(f"spotlight_answer:{empty.run_id}", self.index.docs)
        self.assertEqual(self.doc_acl(empty), self.both)
        self.assertEqual(self.doc_acl(self.plain), self.both)

    def test_nothing_to_do_does_not_need_the_index(self):
        self.make_run(None)
        self.client_mock.ping.return_value = False

        _, report = self.run_command("--apply")

        self.assertEqual(report["runs_with_mentions"], 0)
        self.bulk.assert_not_called()

    def test_apply_refuses_when_opensearch_is_unreachable(self):
        self.history()
        self.client_mock.ping.return_value = False
        before = {k: dict(v) for k, v in self.index.docs.items()}

        with self.assertRaises(CommandError):
            self.run_command("--apply")

        self.bulk.assert_not_called()
        self.assertEqual(self.index.docs, before)

    def test_apply_fails_loudly_when_a_write_did_not_land(self):
        self.history()
        self.bulk.side_effect = ConnectionError("opensearch went down mid-way")

        with self.assertRaises(CommandError):
            self.run_command("--apply")

        # Still wide, still tracked ... and a second run finishes the job.
        self.assertEqual(self.doc_acl(self.private), self.both)
        self.bulk.side_effect = self.index.bulk
        _, report = self.run_command("--apply")
        self.assertEqual(report["to_purge_remaining"], 0)
        self.assertEqual(report["to_rewrite_remaining"], 0)
        self.assertEqual(self.doc_acl(self.private), [self.uid])
        self.assertNotIn(f"spotlight_answer:{self.unknown.run_id}", self.index.docs)

    def test_dry_run_and_apply_cannot_be_combined(self):
        with self.assertRaises(CommandError):
            call_command("rejudge_team_answers", "--dry-run", "--apply", stdout=StringIO())
