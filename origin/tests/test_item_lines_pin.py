"""Characterization pins for `get_item_line` and `read_events` (MIP-1281, phase 0).

These tests say what the line reader produces TODAY, on the 12-step fixture
(`fixtures/sequence_scenario.py`) in every shape it is built in, read as each of the three people,
plus one "rich" story that adds what the 12 steps never touch (a reaction, a merged PR, a `#` chip
in another chat and in another task's comment, a thread reaction, a child task, a blocker, the
member's own markers). The expected values live in `fixtures/item_lines_pins.json`, recorded on
`origin/main` BEFORE the reader was split into one reader per source (`item_lines.py`): the split
must change no byte of any of it, nor the number of queries a line costs.

What is pinned, per scenario and viewer:

  * `item_lines(...)`: the whole rendered result (a refusal is pinned as its exception and text);
  * `read_events(...)`: every field of every `Ev` and the capped flag (the function MIP-1084 counts
    a day with);
  * the number of queries `item_lines` issues.

Normalised before comparing, because these differ from one run to the next: every instant becomes
its offset from the story's base (`+540m`), user ids become A/B/C, the task id becomes `TASK`, and a
row's primary key (inside `Ev.key`) is dropped, keeping its kind. A test that the keys are unique
guards the part the pin drops.

Re-record (only on a deliberate change of what a line says): `PIN_RECORD=1 python manage.py test
origin.tests.test_item_lines_pin`, then read the diff of the JSON file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageReaction,
)
from origin.models.task.task_delivery_models import TaskDelivery
from origin.models.task.task_models import (
    TaskCommentMentionFact,
    TaskCommentReactionFact,
    TaskComments,
    TaskDependency,
    TaskMaster,
)
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.services import item_lines
from origin.tests.fixtures.sequence_scenario import SequenceScenario, paragraph

PINS = Path(__file__).parent / "fixtures" / "item_lines_pins.json"
RECORD = bool(os.environ.get("PIN_RECORD"))
_ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")


def compact(node):
    """Every list of events becomes one JSON line per event: the pin file stays readable and a
    failing diff shows the event, not a page of braces. Lossless."""
    if isinstance(node, dict):
        return {
            k: (
                [
                    json.dumps(e, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                    for e in v
                ]
                if k == "events" and isinstance(v, list) and all(isinstance(e, dict) for e in v)
                else compact(v)
            )
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [compact(v) for v in node]
    return node


def digest(node) -> str:
    return hashlib.sha256(
        json.dumps(node, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def chip_inline(task_id) -> dict:
    """A `#` task chip as the editor writes it (the id as text)."""
    return {"type": "hashTask", "props": {"taskId": str(task_id)}}


VARIANTS = {
    "v1_flat": dict(client_on_project=True, chat_shape="flat"),
    "v2_flat": dict(client_on_project=False, chat_shape="flat"),
    "v1_threaded": dict(client_on_project=True, chat_shape="threaded"),
    "v2_threaded": dict(client_on_project=False, chat_shape="threaded"),
    "v1_flat_c_in_gm": dict(client_on_project=True, chat_shape="flat", c_in_gm=True),
    "v2_flat_announce": dict(client_on_project=False, chat_shape="flat", announce=True),
    "v1_threaded_announce_c_in_gm": dict(
        client_on_project=True, chat_shape="threaded", announce=True, c_in_gm=True
    ),
}


def _load() -> dict:
    return json.loads(PINS.read_text()) if PINS.exists() else {}


def _save(pins: dict) -> None:
    PINS.write_text(json.dumps(pins, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


class Pinned(SequenceScenario):
    maxDiff = None
    """Builds a story, reads it as A, B and C, and compares with the recorded pins."""

    # -- normalising ----------------------------------------------------------- #

    def norm(self, value, seq):
        users = {str(u.id): name for name, u in seq.people.items()}
        task_id = seq.task.task_id

        def walk(node, key=None):
            if isinstance(node, datetime):
                return f"+{int((node - seq.base).total_seconds())}s"
            if isinstance(node, uuid.UUID):
                node = str(node)
            if isinstance(node, str):
                if node in users:
                    return users[node]
                if _ISO.match(node):
                    try:
                        return walk(datetime.fromisoformat(node))
                    except ValueError:
                        return node
                for uid, name in users.items():
                    node = node.replace(uid, name)
                return node.replace(f"Task {task_id} ", "Task TASK ")
            if isinstance(node, bool) or node is None:
                return node
            if isinstance(node, int):
                return "TASK" if key == "task_id" and node == task_id else node
            if isinstance(node, dict):
                return {str(k): walk(v, k) for k, v in node.items()}
            if isinstance(node, (list, tuple)):
                rows = [walk(v, key) for v in node]
                if key == "people":
                    # People with the same number of events are ordered by their (random) user
                    # id: the order is not a property of the story, so it is pinned sorted.
                    rows.sort(key=lambda r: json.dumps(r, sort_keys=True))
                return rows
            return str(node)

        return compact(walk(value))

    def ev_row(self, ev, seq):
        return self.norm(
            {
                "at": ev.at,
                "source": ev.source,
                "key_kind": ev.key[0],
                "what": ev.what,
                "summary": ev.summary,
                "actor": ev.actor,
                "label": ev.label,
                "automated": ev.automated,
                "text": ev.text,
                "via": ev.via,
                "mentions": ev.mentions,
                "extra": ev.extra,
                "ai_app": ev.ai_app,
            },
            seq,
        )

    # -- reading --------------------------------------------------------------- #

    def snapshot(self, seq, user, *, related=item_lines.RELATED_MAX):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))
        since, until = seq.base, seq.minutes(24 * 60)
        now = seq.minutes(24 * 60 + 5)
        out: dict = {}
        with CaptureQueriesContext(connection) as queries:
            try:
                full = item_lines.item_lines(
                    ctx, seq.task.task_id, since, until, limit=100, related_limit=related, now=now
                )
                out["item_lines"] = self.norm(json.loads(json.dumps(full, default=str)), seq)
            except ToolError as exc:
                out["item_lines"] = self.norm({"error": type(exc).__name__, "text": str(exc)}, seq)
        out["queries"] = len(queries)
        access = item_lines.LabelAccess(ctx)
        tasks = access.tasks([seq.task.task_id])
        if tasks:
            by_task, capped = item_lines.read_events(ctx, access, tasks, since, until)
            events = by_task[seq.task.task_id]
            keys = [e.key for e in events]
            self.assertEqual(len(keys), len(set(keys)), "an event told twice")
            out["read_events"] = compact(
                {"capped": capped, "events": [self.ev_row(e, seq) for e in events]}
            )
        else:
            out["read_events"] = None
        out["line_for_item_sha"] = digest(
            self.norm(
                json.loads(
                    json.dumps(
                        self.safe(item_lines.line_for_item, ctx, seq, since, until), default=str
                    )
                ),
                seq,
            )
        )
        out["related_lines_sha"] = digest(
            self.norm(
                json.loads(
                    json.dumps(
                        self.safe(item_lines.related_lines, ctx, seq, since, until), default=str
                    )
                ),
                seq,
            )
        )
        return out

    @staticmethod
    def safe(fn, ctx, seq, since, until):
        try:
            return fn(ctx, seq.task.task_id, since, until)
        except ToolError as exc:
            return {"error": type(exc).__name__, "text": str(exc)}

    def check(self, name, seq, *, viewers=("a", "b", "c")):
        got = {who: self.snapshot(seq, getattr(seq, who)) for who in viewers}
        if RECORD:
            pins = _load()
            pins[name] = got
            _save(pins)
            return
        want = _load().get(name)
        self.assertIsNotNone(want, f"no pin recorded for {name}: run with PIN_RECORD=1 on main")
        for who in viewers:
            for part, value in got[who].items():
                self.assertEqual(value, want[who][part], f"{name} / {who} / {part}")


class TestTheTwelveStepStory(Pinned):
    def test_v1_flat(self):
        self.check("v1_flat", self.build_sequence(**VARIANTS["v1_flat"]))

    def test_v2_flat(self):
        self.check("v2_flat", self.build_sequence(**VARIANTS["v2_flat"]))

    def test_v1_threaded(self):
        self.check("v1_threaded", self.build_sequence(**VARIANTS["v1_threaded"]))

    def test_v2_threaded(self):
        self.check("v2_threaded", self.build_sequence(**VARIANTS["v2_threaded"]))

    def test_v1_flat_with_the_dev_in_the_chat(self):
        self.check("v1_flat_c_in_gm", self.build_sequence(**VARIANTS["v1_flat_c_in_gm"]))

    def test_v2_flat_with_the_announcement(self):
        self.check("v2_flat_announce", self.build_sequence(**VARIANTS["v2_flat_announce"]))

    def test_v1_threaded_with_the_announcement_and_the_dev_in_the_chat(self):
        self.check(
            "v1_threaded_announce_c_in_gm",
            self.build_sequence(**VARIANTS["v1_threaded_announce_c_in_gm"]),
        )


class TestTheRichStory(Pinned):
    """The 12 steps plus what they never touch, written straight to the tables."""

    def rich(self):
        seq = self.build_sequence(client_on_project=True, chat_shape="flat", c_in_gm=True)
        task = seq.task
        project = seq.project

        def stamp(model, pk_filter, field, minutes):
            model.objects.filter(**pk_filter).update(**{field: seq.minutes(minutes)})

        # a reaction by B to C's progress comment (step 7)
        comment7 = seq.comments[7]
        reaction = TaskCommentReactionFact.objects.create(
            team=self.team,
            task=task,
            comment_id=comment7.comment_id,
            reaction_id=1,
            reaction_emoji="👍",
            sender=seq.b,
        )
        stamp(TaskCommentReactionFact, {"pk": reaction.pk}, "ts_created_at", 11 * 60 + 40)
        # C mentions B in a comment
        mention = TaskCommentMentionFact.objects.create(
            team=self.team, task=task, comment_id=comment7.comment_id, mentioned_user=seq.b
        )
        self.assertTrue(mention.pk)
        # a merged PR and an open one
        TaskDelivery.objects.create(
            team=self.team,
            task=task,
            repo_owner="o",
            repo_name="r",
            branch="fix-export",
            pr_url="https://github.com/o/r/pull/7",
            pr_number=7,
            pr_title="Fix the export",
            state="merged",
            merged_at=seq.minutes(15 * 60 + 30),
            source="webhook",
        )
        TaskDelivery.objects.create(
            team=self.team,
            task=task,
            repo_owner="o",
            repo_name="r",
            branch="draft",
            pr_url="https://github.com/o/r/pull/8",
            pr_number=8,
            pr_title="Draft",
            state="open",
            source="webhook",
        )
        # a reaction by A to B's reply in the thread (step 3b)
        reply = seq.messages["3b"]
        mr = MessageReaction.objects.create(message=reply, user=seq.a, emoji="🎉")
        stamp(MessageReaction, {"pk": mr.pk}, "ts_created_at", 9 * 60 + 25)
        # a chip in ANOTHER chat (B and C), and in a task comment elsewhere
        other = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Dev room")
        for member in (seq.b, seq.c):
            ChannelMember.objects.create(channel=other, user=member)
        chip_message = Message.objects.create(
            channel=other,
            sender=seq.c,
            seq=1,
            body=paragraph("see ", chip_inline(task.task_id)),
            body_text=f"see #{task.display_id}",
        )
        stamp(Message, {"pk": chip_message.pk}, "ts_sent_at", 13 * 60)
        elsewhere = TaskMaster.objects.create(
            team=self.team,
            project=project,
            reporter=seq.b,
            assignee=seq.c,
            title="Another job",
            status="Open",
            project_task_number=50,
        )
        pointing = TaskComments.objects.create(
            task=elsewhere,
            sender=seq.c,
            comment_id=1,
            comment_body=paragraph("blocked by ", chip_inline(task.task_id)),
        )
        stamp(TaskComments, {"pk": pointing.pk}, "ts_sent_at", 13 * 60 + 30)
        # a child task with a comment, a blocker, and the task it blocks
        child = TaskMaster.objects.create(
            team=self.team,
            project=project,
            reporter=seq.b,
            assignee=seq.c,
            title="Child step",
            status="Open",
            project_task_number=51,
            parent_task_id=task.task_id,
        )
        blocker = TaskMaster.objects.create(
            team=self.team,
            project=project,
            reporter=seq.b,
            assignee=seq.c,
            title="Blocker",
            status="WIP",
            project_task_number=52,
        )
        TaskDependency.objects.create(blocker_task=blocker, blocked_task=task)
        for number, owner in ((1, child), (2, blocker)):
            made = TaskComments.objects.create(
                task=owner,
                sender=seq.c,
                comment_id=number,
                comment_body=paragraph(f"note on {owner.title}"),
            )
            stamp(TaskComments, {"pk": made.pk}, "ts_sent_at", 12 * 60 + number)
        # the member's own AI-app markers: B reads the task twice and the child once
        with self.frozen(seq.minutes(12 * 60 + 40)):
            self.mcp_tool(seq.b, "get_task", task_id=task.task_id)
        with self.frozen(seq.minutes(12 * 60 + 50)):
            self.mcp_tool(seq.b, "get_task", task_id=child.task_id)
        task.refresh_from_db()
        return seq

    def test_the_rich_story(self):
        seq = self.rich()
        self.check("rich", seq)

    def test_the_rich_story_without_neighbours(self):
        seq = self.rich()
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(seq.b.id))
        out = item_lines.item_lines(
            ctx,
            seq.task.task_id,
            seq.base,
            seq.minutes(24 * 60),
            related_limit=0,
            now=seq.minutes(24 * 60 + 5),
        )
        got = self.norm(json.loads(json.dumps(out, default=str)), seq)
        name = "rich_no_related_b"
        if RECORD:
            pins = _load()
            pins[name] = got
            _save(pins)
            return
        self.assertEqual(got, _load()[name])


class TestAnEditorLessNoteVersion(Pinned):
    """Design check (a) of MIP-1281: what does a line say about a note version with no editor?

    `keep_current_state` writes such a row just before an agent or MCP edit (a copy of what the
    edit replaces), and a version whose editor's account was deleted has none either. The line
    reader does not look at the editor: it tells the row like any other, as "edited the note",
    with no `by`. (The new timeline skips these rows on the note anchor and credits nobody on a
    task anchor; this test pins what the OLD line does, so the reader split cannot change it.)"""

    def test_it_is_told_as_edited_the_note_with_nobody_credited(self):
        from origin.views.utils.note_version import keep_current_state  # noqa: PLC0415

        seq = self.build_sequence(upto=9)
        note_id = seq.note["note_id"]
        from origin.models.note.task_note_models import TaskNoteMaster  # noqa: PLC0415

        note = TaskNoteMaster.objects.get(note_id=note_id)
        # The note's text now differs from its newest version, as it is when an edit that wrote
        # no version of its own (an agent) is about to replace it.
        note.body = paragraph("changed behind the versions' back")
        note.save()
        with self.frozen(seq.minutes(14 * 60 + 20)):
            row = keep_current_state(
                team=self.team,
                note_type=2,
                note_id=note_id,
                title=note.title,
                body=note.body,
            )
        self.assertIsNone(row.editor_id)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(seq.b.id))
        out = item_lines.item_lines(
            ctx,
            seq.task.task_id,
            seq.base,
            seq.minutes(24 * 60),
            related_limit=0,
            now=seq.minutes(24 * 60 + 5),
        )
        notes = [e for e in out["line"]["events"] if e["source"] == "note"]
        self.assertEqual(len(notes), 3)  # C's two versions, and the editor-less copy
        orphan = notes[-1]
        self.assertEqual(orphan["summary"], "edited the note")
        self.assertEqual(orphan["what"], "note_edit")
        self.assertNotIn("by", orphan)
        self.assertNotIn("mine", orphan)
        self.assertIn("by", notes[0])
