"""`python manage.py agent_fingerprint`: what the in-app agent's model is shown, as sha256 lines.

Run it on `origin/main` and on the stack under test with every flag OFF: the two outputs must be
equal, line by line. A line that differs names the surface that moved.

    python manage.py agent_fingerprint                 # one line per surface
    python manage.py agent_fingerprint --dump out_dir  # also write each surface's text (diff -r)

SELF-CONTAINED ON PURPOSE. It imports only modules that exist on `origin/main` (nothing from
`agent/evals/group/`), so the same file can be copied into a main worktree and run there:

    cp origin/search_engine/management/commands/agent_fingerprint.py <main worktree>/<same path>

Surfaces (one line each):

  tools.<shape>              the tool declarations, neutral, then exactly as each provider adapter
                             builds its request (Gemini / Claude / OpenAI), for the full surface and
                             the read-level one. Captured by running the REAL adapter's
                             `generate_step` against a recording SDK client, so an adapter change
                             moves the line too.
  prompt.<shape>             the system instruction, the tool names and the first user turn that
                             `run_agent` hands the model, for: plain ask, Japanese locale, ask with
                             @mentions, and each agency level (read / act / organize). Captured by
                             running the real loop against a recording adapter.
  prompt.thread_ask.template /
  prompt.note_ask.template   the literal text of the system_extra templates in `agent_views.py`
                             (expression holes elided). The thread and note asks are built inline in
                             the view, which needs a summary call and a worker thread, so the
                             template TEXT is fingerprinted instead of an assembled prompt.
  result.<tool>              the text a fixed set of read tools returns on a fixed fixture, built
                             inside a transaction that is ROLLED BACK (nothing persists), with the
                             clock frozen and ids pinned.

NOT covered: prompts and declarations built by other entry points (summaries, digest, schedules),
and tool results of tools not listed in READ_TOOLS. Add a line here when an item changes a new one.

No model call is made: the adapters' SDK clients and the loop's model client are recorders, and the
spend recorder is detached so no ledger row is written.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import logging
import re
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

FROZEN = datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC)
TODAY_LINE = "Today's date is 2026-11-02 (Monday) in the UTC timezone."

# Fixture ids: high, fixed, and rolled back at the end.
_ID_BASE = 990000
USER_A = uuid.UUID("00000000-0000-4000-8000-0000f1a60001")
USER_B = uuid.UUID("00000000-0000-4000-8000-0000f1a60002")
TEAM = uuid.UUID("00000000-0000-4000-8000-0000f1a6aa01")
CHANNEL = uuid.UUID("00000000-0000-4000-8000-0000f1a6cc01")
MSG_ROOT = uuid.UUID("00000000-0000-4000-8000-0000f1a60e01")
MSG_REPLY = uuid.UUID("00000000-0000-4000-8000-0000f1a60e02")
MSG_OTHER = uuid.UUID("00000000-0000-4000-8000-0000f1a60e03")
PROJECT_ID = _ID_BASE + 1
TASK_ID = _ID_BASE + 2
TASK2_ID = _ID_BASE + 3
NOTE_ID = _ID_BASE + 4

#: Read tools whose result text is fingerprinted, and their arguments (filled from the fixture).
READ_TOOLS = (
    "get_current_user",
    "list_projects",
    "list_tasks",
    "fetch_task",
    "get_task_history",
    "fetch_chat_thread",
    "list_my_chats",
    "fetch_note",
    "get_team_members",
    "get_project_summary",
)


class _Captured(Exception):
    """Raised by a recording SDK client once it has the request."""


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1, default=str)


@contextmanager
def _quiet() -> Any:
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(logging.NOTSET)


@contextmanager
def _no_ledger() -> Any:
    """Detach the spend recorder: a captured call must not write a ledger row."""
    from origin.search_engine.llm import spend  # noqa: PLC0415

    previous = spend._recorder
    spend.set_recorder(None)
    try:
        yield
    finally:
        spend.set_recorder(previous)


# --------------------------------------------------------------------------- #
# tools.*: the declarations, as each adapter sends them                        #
# --------------------------------------------------------------------------- #


def _capture_gemini(tools: list[Any]) -> Any:
    from origin.search_engine.llm import gemini_client  # noqa: PLC0415
    from origin.search_engine.llm.types import AgentMessage  # noqa: PLC0415

    seen: dict[str, Any] = {}

    class Models:
        def generate_content_stream(self, *, model: str, contents: Any, config: Any) -> Any:
            seen["model"] = model
            seen["config"] = config.model_dump(mode="json", exclude_none=True)
            seen["contents"] = [c.model_dump(mode="json", exclude_none=True) for c in contents]
            raise _Captured

    class Client:
        models = Models()

    with mock.patch.object(gemini_client, "_get_client", return_value=Client()):
        try:
            list(
                gemini_client.GeminiClient().generate_step(
                    [AgentMessage(role="user", text="q")], tools, "SYSTEM"
                )
            )
        except _Captured:
            pass
    return seen


def _capture_claude(tools: list[Any]) -> Any:
    from origin.search_engine.llm import claude_client  # noqa: PLC0415
    from origin.search_engine.llm.types import AgentMessage  # noqa: PLC0415

    seen: dict[str, Any] = {}

    class Messages:
        def stream(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            raise _Captured

    class Client:
        messages = Messages()

    with mock.patch.object(claude_client, "_get_client", return_value=Client()):
        try:
            list(
                claude_client.ClaudeClient().generate_step(
                    [AgentMessage(role="user", text="q")], tools, "SYSTEM"
                )
            )
        except _Captured:
            pass
    return {k: (None if type(v).__name__ == "NotGiven" else v) for k, v in seen.items()}


def _capture_openai(tools: list[Any]) -> Any:
    from origin.search_engine.llm import openai_client  # noqa: PLC0415
    from origin.search_engine.llm.types import AgentMessage  # noqa: PLC0415

    seen: dict[str, Any] = {}

    class Completions:
        def create(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            raise _Captured

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    with mock.patch.object(openai_client, "_get_client", return_value=Client()):
        try:
            list(
                openai_client.OpenAIClient().generate_step(
                    [AgentMessage(role="user", text="q")], tools, "SYSTEM"
                )
            )
        except _Captured:
            pass
    return seen


def _tool_surfaces() -> dict[str, str]:
    from origin.search_engine.agent.controller import _build_tool_declarations  # noqa: PLC0415
    from origin.search_engine.agent.tool_tiers import disabled_tools_for_level  # noqa: PLC0415

    out: dict[str, str] = {}
    with _quiet(), _no_ledger():
        for label, disabled in (("full", None), ("read", disabled_tools_for_level("read"))):
            tools = _build_tool_declarations(disabled)
            out[f"tools.neutral.{label}"] = _canon(
                [
                    {"name": t.name, "description": t.description, "schema": t.parameters_schema}
                    for t in tools
                ]
            )
            out[f"tools.gemini.{label}"] = _canon(_capture_gemini(tools))
            out[f"tools.claude.{label}"] = _canon(_capture_claude(tools))
            out[f"tools.openai.{label}"] = _canon(_capture_openai(tools))
    return out


# --------------------------------------------------------------------------- #
# prompt.*: what run_agent hands the model                                     #
# --------------------------------------------------------------------------- #


def _capture_run(
    query: str, ctx: Any, *, system_extra: str | None, disabled: set[str] | None
) -> str:
    from origin.search_engine import llm  # noqa: PLC0415
    from origin.search_engine.agent import controller  # noqa: PLC0415

    captured: list[dict[str, Any]] = []

    class Recorder:
        def generate_step(
            self, messages: Any, tools: Any, system_instruction: str, **kwargs: Any
        ) -> Any:
            captured.append(
                {
                    "system_instruction": system_instruction,
                    "tools": [t.name for t in tools],
                    "messages": [dataclasses.asdict(m) for m in messages],
                    "params": repr(kwargs.get("params")),
                }
            )
            return iter([("ok", None)])

    with (
        mock.patch.object(llm, "_build_adapter", lambda provider: Recorder()),
        mock.patch.object(controller, "_current_date_line", return_value=TODAY_LINE),
    ):
        controller.run_agent(
            query,
            ctx,
            lambda _event: None,
            run_id=None,
            system_extra=system_extra,
            disabled_tools=disabled,
        )
    if not captured:
        raise CommandError(f"run_agent made no model call for {query!r}")
    return _canon(captured[0])


def _mentions() -> list[Any]:
    from origin.search_engine.agent.mentions import ResolvedMention  # noqa: PLC0415

    return [
        ResolvedMention(kind="user", label="Ben Manager", user_id=str(USER_B)),
        ResolvedMention(
            kind="task",
            label="Fix the export that fails",
            task_id=TASK_ID,
            display_id="FPT-1",
            project_id=str(PROJECT_ID),
        ),
        ResolvedMention(kind="project", label="Fingerprint Project", project_id=str(PROJECT_ID)),
    ]


def _prompt_surfaces() -> dict[str, str]:
    from origin.search_engine.agent.mentions import build_mention_system_extra  # noqa: PLC0415
    from origin.search_engine.agent.tool_tiers import (  # noqa: PLC0415
        READ_LEVEL_SYSTEM_EXTRA,
        disabled_tools_for_level,
    )
    from origin.search_engine.agent.tools import ToolContext  # noqa: PLC0415

    ctx = ToolContext(team_id=str(TEAM), user_id=str(USER_A))
    ja = ToolContext(team_id=str(TEAM), user_id=str(USER_A), locale="ja")
    out: dict[str, str] = {}
    with _quiet(), _no_ledger():
        out["prompt.plain"] = _capture_run("What is open?", ctx, system_extra=None, disabled=None)
        out["prompt.plain.ja"] = _capture_run("What is open?", ja, system_extra=None, disabled=None)
        mention_extra = build_mention_system_extra(_mentions())
        out["prompt.mentions"] = _capture_run(
            "Status of @task?", ctx, system_extra=mention_extra, disabled=None
        )
        for level in ("organize", "act", "read"):
            extra = READ_LEVEL_SYSTEM_EXTRA if level == "read" else None
            out[f"prompt.level.{level}"] = _capture_run(
                "What is open?", ctx, system_extra=extra, disabled=disabled_tools_for_level(level)
            )
    out.update(_template_surfaces())
    return out


def _template_surfaces() -> dict[str, str]:
    """The literal text of the thread / note `system_extra` templates in agent_views.py."""
    from origin.search_engine import agent_views  # noqa: PLC0415

    source = Path(agent_views.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: dict[str, list[str]] = {"thread": [], "note": []}

    def literal(node: ast.AST) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return literal(node.left) + literal(node.right)
        return "{}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "system_extra" for t in node.targets
        ):
            text = literal(node.value)
            if "<thread_summary>" in text:
                found["thread"].append(text)
            elif "<note_summary>" in text:
                found["note"].append(text)
    return {
        "prompt.thread_ask.template": "\n----\n".join(found["thread"]) or "(not found)",
        "prompt.note_ask.template": "\n----\n".join(found["note"]) or "(not found)",
    }


# --------------------------------------------------------------------------- #
# result.*: read tools on a fixed fixture, rolled back                         #
# --------------------------------------------------------------------------- #


def _build_fixture() -> None:
    from django.contrib.auth import get_user_model  # noqa: PLC0415

    from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message  # noqa: PLC0415
    from origin.models.common.team_models import TeamMaster, TeamMembers  # noqa: PLC0415
    from origin.models.note.common_note_models import NotePermissionMaster  # noqa: PLC0415
    from origin.models.note.personal_note_models import PersonalNoteMaster  # noqa: PLC0415
    from origin.models.project.prj_models import ProjectMaster, ProjectMembers  # noqa: PLC0415
    from origin.models.task.task_models import TaskComments, TaskMaster  # noqa: PLC0415

    user_model = get_user_model()
    if (
        TeamMaster.objects.filter(team_id=TEAM).exists()
        or TaskMaster.objects.filter(task_id=TASK_ID).exists()
    ):
        raise CommandError("the fingerprint fixture ids are already in use in this database")
    a = user_model.objects.create_user(
        email="fingerprint-a@genos.app",
        username="Alice Fingerprint",
        password="x",
        id=USER_A,
        is_demo=True,
        timezone="UTC",
    )
    b = user_model.objects.create_user(
        email="fingerprint-b@genos.app",
        username="Ben Fingerprint",
        password="x",
        id=USER_B,
        is_demo=True,
        timezone="UTC",
    )
    team = TeamMaster.objects.create(
        team_id=TEAM,
        team_name="Fingerprint Team",
        team_email="fingerprint-team@genos.app",
        owner=a,
        is_demo=True,
    )
    for member in (a, b):
        TeamMembers.objects.create(team=team, attendee=member)
    project = ProjectMaster.objects.create(
        project_id=PROJECT_ID,
        team=team,
        project_name="Fingerprint Project",
        code="FPT",
        owner=a,
        project_system_user=a,
    )
    for member in (a, b):
        ProjectMembers.objects.create(team=team, project=project, attendee=member)
    task = TaskMaster.objects.create(
        task_id=TASK_ID,
        team=team,
        project=project,
        reporter=a,
        assignee=b,
        title="Fix the export that fails",
        status="WIP",
        priority="High",
        project_task_number=1,
    )
    TaskMaster.objects.create(
        task_id=TASK2_ID,
        team=team,
        project=project,
        reporter=b,
        assignee=a,
        title="Write the release note",
        status="Open",
        project_task_number=2,
    )
    TaskComments.objects.create(
        task=task,
        sender=b,
        comment_id=1,
        comment_body=[
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Please check the CSV export too", "styles": {}}
                ],
            }
        ],
    )
    channel = Channel.objects.create(
        id=CHANNEL, team=team, kind=ChannelKind.GM, title="Fingerprint chat"
    )
    for member in (a, b):
        ChannelMember.objects.create(channel=channel, user=member)

    def say(pk: uuid.UUID, seq: int, sender: Any, text: str, parent: Any = None) -> Any:
        return Message.objects.create(
            id=pk,
            channel=channel,
            sender=sender,
            seq=seq,
            parent=parent,
            body_text=text,
            body=[{"type": "paragraph", "content": [{"type": "text", "text": text, "styles": {}}]}],
        )

    root = say(MSG_ROOT, 1, a, "The export button does nothing")
    say(MSG_REPLY, 2, b, "Looking at it now", parent=root)
    say(MSG_OTHER, 3, b, "Release is on Friday")
    note = PersonalNoteMaster.objects.create(
        note_id=NOTE_ID,
        team=team,
        owner=a,
        title="Export notes",
        body=[
            {
                "id": "b1",
                "type": "paragraph",
                "props": {},
                "content": [{"type": "text", "text": "Cause: a missing permission", "styles": {}}],
                "children": [],
            }
        ],
    )
    NotePermissionMaster.objects.create(
        team=team, user=a, note_id=note.note_id, note_type=1, role_id=1
    )


_VOLATILE = [
    # Rows made by signals (the project's PM channel) get a random uuid4; the fixture's own ids are fixed.
    (
        re.compile(
            r"\b(?!00000000-0000-4000-8000-0000f1a6)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
        ),
        "<uuid>",
    ),
    (re.compile(r'"body_hash": "[0-9a-f]+"'), '"body_hash": "<hash>"'),
    (
        re.compile(r"\b(\d{4}-\d{2}-\d{2})T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?"),
        r"\1T<time>",
    ),
]


def _normalise(text: str) -> str:
    for pattern, repl in _VOLATILE:
        text = pattern.sub(repl, text)
    return text


def _result_surfaces() -> dict[str, str]:
    from origin.search_engine.agent.tools import REGISTRY, ToolContext  # noqa: PLC0415
    from origin.search_engine.agent.tools.base import ToolError  # noqa: PLC0415

    args = {
        "get_current_user": {},
        "list_projects": {},
        "list_tasks": {},
        "fetch_task": {"task_id": TASK_ID},
        "get_task_history": {"task_id": TASK_ID},
        "fetch_chat_thread": {
            "chat_type": "gm",
            "chat_id": str(CHANNEL),
            "thread_id": str(MSG_ROOT),
        },
        "list_my_chats": {},
        "fetch_note": {"note_type": "personal", "note_id": NOTE_ID},
        "get_team_members": {},
        "get_project_summary": {"project_id": PROJECT_ID},
    }
    out: dict[str, str] = {}
    with transaction.atomic():
        try:
            with (
                mock.patch("django.utils.timezone.now", return_value=FROZEN),
                _quiet(),
                _no_ledger(),
            ):
                _build_fixture()
                ctx = ToolContext(team_id=str(TEAM), user_id=str(USER_A))
                for name in READ_TOOLS:
                    tool = REGISTRY.get(name)
                    if tool is None:
                        out[f"result.{name}"] = "(tool not registered)"
                        continue
                    try:
                        text = _canon(tool.run(args[name], ctx))
                    except ToolError as exc:
                        text = f"ToolError: {exc}"
                    except Exception as exc:  # noqa: BLE001 — recorded as the surface's text
                        text = f"{type(exc).__name__}: {exc}"
                    out[f"result.{name}"] = _normalise(text)
        finally:
            transaction.set_rollback(True)
    return out


# --------------------------------------------------------------------------- #


class Command(BaseCommand):
    help = "Print sha256 lines of what the in-app agent's model is shown (declarations, prompts, tool results)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--dump", help="Directory to write each surface's text into (for diff -r)."
        )
        parser.add_argument(
            "--only",
            action="append",
            default=[],
            help="Only surfaces whose name starts with this. Repeatable.",
        )
        parser.add_argument(
            "--no-results",
            action="store_true",
            help="Skip the tool-result surfaces (they need the database).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        surfaces: dict[str, str] = {}
        surfaces.update(_tool_surfaces())
        surfaces.update(_prompt_surfaces())
        if not options["no_results"]:
            surfaces.update(_result_surfaces())
        if options["only"]:
            surfaces = {
                k: v for k, v in surfaces.items() if any(k.startswith(p) for p in options["only"])
            }
        dump = Path(options["dump"]) if options.get("dump") else None
        if dump:
            dump.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        for name in sorted(surfaces):
            text = surfaces[name]
            sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
            digest.update(f"{name} {sha}\n".encode())
            self.stdout.write(f"{name:<40} {sha} {len(text.encode('utf-8')):>8}")
            if dump:
                (dump / f"{name}.txt").write_text(text, encoding="utf-8")
        self.stdout.write(f"{'ALL':<40} {digest.hexdigest()}")
