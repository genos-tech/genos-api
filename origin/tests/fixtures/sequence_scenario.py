"""Ken's 12-step example, built through the real funnels (MIP-1269).

    1. user-A (client) asked X in the GM chat
    2. user-B (PM) responded to A about X
    3. A asked B to handle issue Y due to X
    4. B created task-A for Y
    5. user-C (dev) got assigned to task-A
    6. C started handling it
    7. C commented progress
    8. B replied to a comment
    9. C summarized the progress and comments in a task note
   10. C closed task-A with a comment
   11. B noticed that task-A had been closed
   12. A noticed that issue Y was solved

No AI and no network: every row is written by the code a person's click (REST) or a connected
AI app (MCP) reaches, so history rows, `via`, the thread link, mentions and note versions are
produced the real way. Time is frozen per step (`django.utils.timezone.now` is patched while a
step runs), so a timestamp is `base + minutes` whatever the hour the suite runs at.

ENTRY POINTS (test C, MIP-1271, reuses these)::

    class MyCase(SequenceScenario):
        def test_x(self):
            seq = self.build_sequence(client_on_project=False, chat_shape="flat")
            seq.task            # the TaskMaster of Y
            seq.step_at[5]      # the instant of step 5
            self.line_as(seq.a, seq)          # `get_item_line` over MCP as A -> (json, is_error)
            self.briefing_as(seq.b, seq, at=seq.minutes(17 * 60))   # `build_briefing` as B
            self.events_for(seq, seq.c)       # {step: event dict} found on C's line
            seq.token[7]        # the unique text of step 7's comment: find it in any output
            ground_truth(seq)   # the story as facts in time order: who did what, when (the
                                # answer key for a case: what an answer must contain, in order)

`build_sequence` options:

  client_on_project   True (V1): A is a member of the project. False (V2): A is a client who is
                      only in the GM chat, on no project.
  chat_shape          "flat": steps 1-3 are top-level chat messages and B opens a thread on A's
                      request (step 3) to create the task. "threaded": steps 1-3 are one thread
                      (A's question is the root) and the task is created from it.
  c_in_gm             True: the dev C is in the GM chat too. Default False (a client chat).
  upto                Stop after this step (one of `ORDER`: 1, 2, 3, "3b", 4, ..., 11, "12a", 12),
                      to read a surface AS OF that moment: a briefing is current-state aware (a
                      closed task is no longer "open work"), so "what did C's briefing say right
                      after the assignment" needs a story that stops there.
  texts               `{step: text}` overrides what a step says (1, 2, 3, "3b", 7, 8, 10, "12a"),
                      e.g. Japanese. The token is added in front, so lookups still work.
  announce            True: after the close B tells A in the task's thread, with an @mention
                      (the only thing that can reach a client who is not on the project).

OBSERVER EFFECT. An MCP read of the task writes the reader's own seen marker at the REAL time
(the clock is frozen only while a step is built). So the FIRST `line_as` of a member shows
`last_seen_at` as of the story (step 11 for B, 12 for A); every later read moves it to now and
`since_seen` restarts. Read what depends on it first, or build a fresh story.

Every text step carries a unique token (`seq.token[n]`, e.g. `S07`), so an output is searched by
what it says, never by its position.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.api_key_models import SCOPE_WRITE, ApiKey, generate_key, hash_key
from origin.models.common.team_models import TeamMembers
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.services import briefing
from origin.tests.test_mcp_endpoint import McpBase

User = get_user_model()
JST = ZoneInfo("Asia/Tokyo")

# One working day, in minutes after the local midnight three days ago.
SCHEDULE = {
    1: 9 * 60,
    2: 9 * 60 + 5,
    3: 9 * 60 + 12,
    "3b": 9 * 60 + 18,  # B answers in the thread: "I will open a task"
    4: 9 * 60 + 20,
    "4card": 9 * 60 + 20,  # the task card the app posts in the thread (+30 s)
    5: 9 * 60 + 30,
    6: 10 * 60,
    7: 11 * 60 + 30,
    8: 12 * 60 + 15,
    9: 14 * 60,  # the note is created ...
    "9b": 14 * 60 + 10,  # ... and edited once more
    10: 16 * 60,  # the comment; the close is 30 s later
    11: 16 * 60 + 30,
    "12a": 16 * 60 + 45,  # B tells A (announce=True only)
    12: 17 * 60,
}

# The order the story is built in (`build_sequence(upto=...)` stops after one of these).
ORDER = (1, 2, 3, "3b", 4, 5, 6, 7, 8, 9, 10, 11, "12a", 12)

# The text of every step that says something.
TEXT = {
    1: "X is broken: the export button does nothing",
    2: "Looking at X now, it is the new export code",
    3: "Can you handle Y, the export that fails, because of X?",
    "3b": "On it, I will open a task for Y",
    7: "Progress: reproduced Y, the cause is a missing permission",
    8: "Thanks, please also check the CSV export",
    10: "Fixed and shipped, Y is solved",
    "12a": "Y is solved, the task is closed",
}
NOTE_V1 = "Summary of Y: reproduced, root cause a missing permission, comments reviewed"
NOTE_V2 = "Summary of Y: fixed, CSV export checked, shipped"


def paragraph(text: str, *inlines: dict) -> list[dict]:
    return [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": text, "styles": {}}, *inlines],
        }
    ]


def mention_chip(user) -> dict:
    return {"type": "mention", "props": {"userId": str(user.id), "user": user.username}}


@dataclass
class Sequence:
    """What `build_sequence` built: the people, the rows and the instants."""

    a: Any
    b: Any
    c: Any
    project: ProjectMaster
    gm: Channel
    base: datetime
    client_on_project: bool
    chat_shape: str
    c_in_gm: bool
    announce: bool
    task: TaskMaster = None
    thread_root: Message = None
    messages: dict = field(default_factory=dict)  # 1, 2, 3, "3b", "4card", "12a" -> Message
    comments: dict = field(default_factory=dict)  # 7, 8, 10 -> TaskComments
    note: Any = None
    step_at: dict = field(default_factory=dict)  # step -> instant
    close_at: datetime = None  # the move to Closed: 30 s after step 10's comment
    last_at: datetime = None  # the instant of the last step built
    token: dict = field(default_factory=dict)  # step -> unique text token
    text: dict = field(default_factory=dict)  # step -> what the step says (override: `texts`)

    def minutes(self, minutes: int, seconds: int = 0) -> datetime:
        return self.base + timedelta(minutes=minutes, seconds=seconds)

    @property
    def people(self) -> dict:
        return {"A": self.a, "B": self.b, "C": self.c}

    def who(self, user_id) -> str:
        for name, user in self.people.items():
            if str(user.id) == str(user_id):
                return name
        return "?"


class SequenceScenario(McpBase):
    """A TestCase base: `build_sequence` and the readers. Subclass it, do not run it."""

    def setUp(self):
        super().setUp()
        # McpBase's own project and task are noise for this story.
        TaskMaster.objects.all().delete()
        self.b = self.user  # the PM: the team's owner, so on the project's own record
        self.a = User.objects.create_user(username="client_a", email="a@example.com", password="x")
        self.c = User.objects.create_user(username="dev_c", email="c@example.com", password="x")
        for user in (self.a, self.b, self.c):
            user.timezone = "Asia/Tokyo"
            user.save(update_fields=["timezone"])
        for user in (self.a, self.c):
            TeamMembers.objects.create(team=self.team, attendee=user)
        # Local midnight three days ago: far from "now" in both directions.
        local = timezone.now().astimezone(JST).date() - timedelta(days=3)
        self.base = datetime.combine(local, datetime.min.time(), tzinfo=JST)
        self._raw_keys: dict[str, str] = {}

    # -- time and credentials ---------------------------------------------- #

    @contextmanager
    def frozen(self, at: datetime):
        """`timezone.now()` is `at` inside the block: auto_now_add columns get exactly it."""
        with mock.patch("django.utils.timezone.now", return_value=at):
            yield

    def _rest(self, user):
        self.authenticate(user)

    def _mcp(self, user):
        key = self._raw_keys.get(str(user.id))
        if key is None:
            key = generate_key()
            ApiKey.objects.create(
                user=user,
                team=None,
                name="mcp",
                key_hash=hash_key(key),
                prefix=key[:11],
                scope=SCOPE_WRITE,
            )
            self._raw_keys[str(user.id)] = key
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {key}")

    def mcp_tool(self, user, name: str, **args) -> dict:
        """One MCP tool call as `user`; returns its parsed JSON (`structuredContent`-like)."""
        self._mcp(user)
        text, is_error = self._tool_text(self._call(name, args))
        self.assertFalse(is_error, f"{name}: {text}")
        return json.loads(text[text.index("{") :]) if "{" in text else {}

    # -- the story ----------------------------------------------------------- #

    def _say(self, seq, user, step, *, root=None, mention=None, task=None) -> Message:
        """A chat message as the app sends it: `POST /api/v3/channels/<id>/messages/`."""
        text = f"{seq.token[step]} {seq.text[step]}"
        inlines = [mention_chip(mention)] if mention else []
        metadata = {"taskId": task.task_id} if task else {}
        with self.frozen(seq.minutes(SCHEDULE[step])):
            self._rest(user)
            res = self.client.post(
                f"/api/v3/channels/{seq.gm.id}/messages/",
                {
                    "body": paragraph(text, *inlines),
                    "body_text": text,
                    "parent_id": str(root.id) if root else None,
                    "metadata": metadata,
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.data)
        return Message.objects.get(pk=res.data["id"])

    def build_sequence(
        self,
        *,
        client_on_project: bool = True,
        chat_shape: str = "flat",
        c_in_gm: bool = False,
        announce: bool = False,
        upto: Any = 12,
        texts: dict | None = None,
    ) -> Sequence:
        assert chat_shape in ("flat", "threaded")
        stop = ORDER.index(upto)

        def due(step) -> bool:
            return ORDER.index(step) <= stop

        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Client Portal",
            code="CLP",
            owner=self.b,
            project_system_user=self.b,
        )
        for member in (self.b, self.c, *([self.a] if client_on_project else [])):
            ProjectMembers.objects.create(team=self.team, project=project, attendee=member)
        gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Client chat")
        for member in (self.a, self.b, *([self.c] if c_in_gm else [])):
            ChannelMember.objects.create(channel=gm, user=member)
        seq = Sequence(
            a=self.a,
            b=self.b,
            c=self.c,
            project=project,
            gm=gm,
            base=self.base,
            client_on_project=client_on_project,
            chat_shape=chat_shape,
            c_in_gm=c_in_gm,
            announce=announce,
        )
        seq.text = {**TEXT, **(texts or {})}
        seq.token = {n: f"S{n:02d}" if isinstance(n, int) else f"S{n}" for n in SCHEDULE}
        seq.step_at = {n: seq.minutes(m) for n, m in SCHEDULE.items()}
        seq.last_at = seq.step_at[upto]

        # 1-3: the client chat, before any task exists.
        seq.messages[1] = self._say(seq, self.a, 1)
        if due(2):
            if chat_shape == "threaded":
                seq.thread_root = seq.messages[1]
                seq.messages[2] = self._say(seq, self.b, 2, root=seq.thread_root)
            else:
                seq.messages[2] = self._say(seq, self.b, 2)
        if due(3):
            if chat_shape == "threaded":
                seq.messages[3] = self._say(seq, self.a, 3, root=seq.thread_root)
            else:
                seq.messages[3] = self._say(seq, self.a, 3)
                seq.thread_root = seq.messages[3]  # B opens a thread on A's request
        if due("3b"):
            seq.messages["3b"] = self._say(seq, self.b, "3b", root=seq.thread_root)

        # 4: B creates the task from the thread (`POST /api/v2/task/` with the thread link, as
        # the create form sends it) and the app posts the task card in the thread.
        if due(4):
            self._create_task(seq)
        # 5: B assigns C (an AI app's `assign_task`).  6: C starts (`update_task`).
        if due(5):
            with self.frozen(seq.step_at[5]):
                self.mcp_tool(
                    self.b, "assign_task", task_id=seq.task.task_id, assignee_id=str(self.c.id)
                )
        if due(6):
            with self.frozen(seq.step_at[6]):
                self.mcp_tool(self.c, "update_task", task_id=seq.task.task_id, status="WIP")
        # 7: C comments in the app (`POST /api/v2/task/comment/`).
        if due(7):
            seq.comments[7] = self._comment_rest(seq, self.c, 7)
        # 8: B replies to that comment (an AI app's `add_comment`, pointing at C).
        if due(8):
            with self.frozen(seq.step_at[8]):
                self.mcp_tool(
                    self.b,
                    "add_comment",
                    task_id=seq.task.task_id,
                    body_text=f"{seq.token[8]} [@C](user:{self.c.id}) {seq.text[8]}",
                )
            seq.comments[8] = self._last_comment(seq)
        # 9: C writes the task note (`create_note`, then `update_note`: two versions).
        if due(9):
            with self.frozen(seq.step_at[9]):
                made = self.mcp_tool(
                    self.c,
                    "create_note",
                    note_type="task",
                    project_id=project.project_id,
                    task_id=seq.task.task_id,
                    title="Y: progress summary",
                    content_text=f"{seq.token[9]} {NOTE_V1}",
                )
            seq.note = {"note_id": made["note_id"]}
            with self.frozen(seq.step_at["9b"]):
                self.mcp_tool(
                    self.c,
                    "update_note",
                    note_id=made["note_id"],
                    note_type="task",
                    content_text=f"{seq.token['9b']} {NOTE_V2}",
                )
        # 10: C closes it with a comment: the comment, then the status.
        if due(10):
            with self.frozen(seq.step_at[10]):
                self.mcp_tool(
                    self.c,
                    "add_comment",
                    task_id=seq.task.task_id,
                    body_text=f"{seq.token[10]} {seq.text[10]}",
                )
            seq.comments[10] = self._last_comment(seq)
            seq.close_at = seq.minutes(SCHEDULE[10], 30)
            with self.frozen(seq.close_at):
                self.mcp_tool(self.c, "update_task", task_id=seq.task.task_id, status="Closed")
        # 11: B notices: opens the task in an AI app (a seen marker is all that leaves).
        if due(11):
            with self.frozen(seq.step_at[11]):
                self.mcp_tool(self.b, "get_task", task_id=seq.task.task_id)
        # (announce) B tells A in the task's thread.  12: A looks (V1 only: V2 cannot open it).
        if announce and due("12a"):
            seq.messages["12a"] = self._say(
                seq, self.b, "12a", root=seq.thread_root, mention=self.a
            )
        if due(12) and client_on_project:
            with self.frozen(seq.step_at[12]):
                self.mcp_tool(self.a, "get_task", task_id=seq.task.task_id)
        if seq.task is not None:
            seq.task.refresh_from_db()
        return seq

    def _create_task(self, seq: Sequence) -> None:
        with self.frozen(seq.step_at[4]):
            self._rest(self.b)
            res = self.client.post(
                "/api/v2/task/",
                {
                    "team": str(self.team.team_id),
                    "project": seq.project.project_id,
                    "chat_type": seq.gm.kind,
                    "chat_id": str(seq.gm.id),
                    "thread_id": str(seq.thread_root.id),
                    "assignee": None,
                    "reporter": str(self.b.id),
                    "title": "Fix the export that fails (Y)",
                    "priority": "High",
                    "effort_level": "Moderate",
                    "status": "Open",
                    "content": paragraph("Y: the export fails. Asked by the client in the chat."),
                    "due_date": None,
                    "links": [],
                    "tags": [],
                    "is_init_task": False,
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.data)
        seq.task = TaskMaster.objects.get(task_id=res.data["task"]["task_id"])
        card_text = f"Task {seq.task.display_id} created"
        with self.frozen(seq.minutes(SCHEDULE["4card"], 30)):
            self._rest(self.b)
            card = self.client.post(
                f"/api/v3/channels/{seq.gm.id}/messages/",
                {
                    "body": paragraph(card_text),
                    "body_text": card_text,
                    "parent_id": str(seq.thread_root.id),
                    "metadata": {"taskId": seq.task.task_id},
                },
                format="json",
            )
        self.assertEqual(card.status_code, 201, card.data)
        seq.messages["4card"] = Message.objects.get(pk=card.data["id"])

    @staticmethod
    def _last_comment(seq: Sequence) -> TaskComments:
        return TaskComments.objects.filter(task=seq.task).order_by("-comment_id").first()

    def _comment_rest(self, seq, user, step) -> TaskComments:
        with self.frozen(seq.step_at[step]):
            self._rest(user)
            res = self.client.post(
                "/api/v2/task/comment/",
                {
                    "task_id": seq.task.task_id,
                    "comment_body": paragraph(f"{seq.token[step]} {seq.text[step]}"),
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.data)
        return TaskComments.objects.get(task=seq.task, comment_id=res.data["comment_id"])

    # -- the readers ---------------------------------------------------------- #

    def line_as(self, user, seq, *, include_related=True, limit=100, task_id=None):
        """`get_item_line` over MCP as `user`, the whole story's day. `(json | None, text)`:
        the JSON is None when the tool refused (a task the member cannot open)."""
        self._mcp(user)
        args = {
            "task_id": task_id or seq.task.task_id,
            "since": seq.base.isoformat(),
            "until": seq.minutes(24 * 60).isoformat(),
            "include_related": include_related,
            "limit": limit,
        }
        text, is_error = self._tool_text(self._call("get_item_line", args))
        if is_error:
            return None, text
        return json.loads(text[text.index("{") :]), text

    def timeline_as(self, user, seq, anchor_type="thread", *, anchor_id=None, **kw):
        """`get_timeline` (MIP-1281) over MCP as `user`: `(json | None, text)`. The default anchor
        is the thread the task was made from; `chat` takes the whole story's day as its window.
        The JSON is None when the tool refused."""
        self._mcp(user)
        ids = {"task": seq.task.task_id, "thread": seq.thread_root.id, "chat": seq.gm.id}
        args = {"anchor_type": anchor_type, "anchor_id": str(anchor_id or ids[anchor_type]), **kw}
        if anchor_type == "chat":
            args.setdefault("since", seq.base.isoformat())
            args.setdefault("until", seq.minutes(24 * 60).isoformat())
        text, is_error = self._tool_text(self._call("get_timeline", args))
        if is_error:
            return None, text
        return json.loads(text[text.index("{") :]), text

    def briefing_as(self, user, seq, at=None):
        """`build_briefing` for `user` over the whole day, as of `at` (default: 30 minutes after
        the last step built)."""
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))
        now = at or seq.last_at + timedelta(minutes=30)
        with self.frozen(now):
            return briefing.build_briefing(ctx, since=seq.base.isoformat(), now=now)

    # -- finding steps in an output -------------------------------------------- #

    @staticmethod
    def plain(value):
        """Strip the `<workspace_content>` fence the tools put round teammate text."""
        return re.sub(r"</?workspace_content>", "", json.dumps(value, default=str))

    def events_for(self, seq, user) -> dict:
        """`{step: event}` for what `user`'s line holds, found by what each event is."""
        out, _ = self.line_as(user, seq)
        if out is None:
            return {}
        return locate_steps(seq, out["line"]["events"], viewer=user)


USERNAMES = {"client_a": "A", "testuser": "B", "dev_c": "C"}
# Who did each step (the person the line must name).
ACTOR = {
    1: "A",
    2: "B",
    3: "A",
    "3b": "B",
    4: "B",
    5: "B",
    6: "C",
    7: "C",
    8: "B",
    9: "C",
    "9b": "C",
    10: "C",
    "close": "C",
    11: "B",
    "12a": "B",
    12: "A",
}


def ground_truth(seq: Sequence) -> list[dict]:
    """The story as facts in time order, `[{"step", "who", "at", "fact", "token"}]`: what really
    happened, built from the rows themselves (not from any tool's output), so a test or a case
    can hold an answer or a line against it. Only steps that were built are listed."""
    facts = []

    def add(step, who, at, fact):
        if at is not None:
            facts.append(
                {"step": step, "who": who, "at": at, "fact": fact, "token": seq.token.get(step)}
            )

    for step, who, fact in (
        (1, "A", "asked X in the GM chat"),
        (2, "B", "answered A about X"),
        (3, "A", "asked B to handle Y because of X"),
        ("3b", "B", "said they would open a task"),
    ):
        message = seq.messages.get(step)
        add(step, who, message.ts_sent_at if message else None, fact)
    if seq.task is not None:
        created = TaskActivity.objects.filter(task=seq.task, action_type="created").first()
        add(4, "B", created.ts_created_at if created else None, "created the task for Y")
        for row in TaskActivity.objects.filter(task=seq.task).order_by("ts_created_at"):
            if row.action_type == "assignee_changed":
                add(5, "B", row.ts_created_at, "assigned the task to C")
            elif row.action_type == "status_changed" and row.new_value == "WIP":
                add(6, "C", row.ts_created_at, "started handling it (status WIP)")
            elif row.action_type == "status_changed" and row.new_value == "Closed":
                add("close", "C", row.ts_created_at, "closed the task")
    for step, who, fact in (
        (7, "C", "commented progress"),
        (8, "B", "replied to the comment"),
        (10, "C", "commented that Y is fixed"),
    ):
        comment = seq.comments.get(step)
        add(step, who, comment.ts_sent_at if comment else None, fact)
    if seq.note is not None:
        versions = NoteVersionMaster.objects.filter(
            note_type=2, note_id=seq.note["note_id"]
        ).order_by("version_no")
        for step, version in zip((9, "9b"), versions):
            add(step, "C", version.ts_updated_at, "wrote the progress summary in the task note")
    for step, who, fact in (
        (11, "B", "noticed the task is closed"),
        (12, "A", "looked at the closed task"),
    ):
        if (
            step in seq.step_at
            and (step == 11 or seq.client_on_project)
            and seq.last_at >= seq.step_at[step]
        ):
            add(step, who, seq.step_at[step], fact)
    message = seq.messages.get("12a")
    add(
        "12a", "B", message.ts_sent_at if message else None, "told A in the thread that Y is solved"
    )
    return sorted(facts, key=lambda f: f["at"])


def who_of(event: dict) -> str | None:
    """A, B or C for the `by` of a rendered event (the teammate name sits in a fence)."""
    name = re.sub(r"</?workspace_content>", "", str(event.get("by", ""))).strip()
    return USERNAMES.get(name)


def locate_steps(seq: Sequence, events: list[dict], viewer=None) -> dict:
    """Which steps an ordered list of line events holds, `{step: event + "_index"}`.

    A step that says something is found by its token (`seq.token`). The others by what they are:
    the creation row (4), the assignment (5), the move to WIP (6), the move to Closed (`"close"`),
    the note's two versions (9, "9b": the note's text is not on the line, so by order), and the
    viewer's own AI-app read of the task: B's is step 11, A's is step 12. A step the line does
    not hold is simply absent from the result."""
    found: dict = {}
    claimed: set[int] = set()

    def take(step, predicate):
        for i, event in enumerate(events):
            if i not in claimed and predicate(event):
                found[step] = {**event, "_index": i}
                claimed.add(i)
                return

    for step in TEXT:
        take(step, lambda e, t=seq.token[step]: t in json.dumps(e, default=str))
    take(4, lambda e: e["source"] == "activity" and e["summary"] == "created it")
    take(5, lambda e: e["source"] == "activity" and e["what"] in ("assignee", "assigned_to_you"))
    take(6, lambda e: e["source"] == "activity" and "WIP" in e["summary"])
    take("close", lambda e: e["source"] == "activity" and e["summary"].endswith("Closed"))
    take(9, lambda e: e["source"] == "note")
    take("9b", lambda e: e["source"] == "note")
    if viewer is not None:
        step = 11 if str(viewer.id) == str(seq.b.id) else 12
        take(step, lambda e: e["source"] == "app" and e["what"] == "app_read")
    return found
