"""`kind: resume` cases of the grouped eval: a paused run, resumed through the PRODUCTION path.

`run_resume_case(case, overrides)` is what `agent_eval_group` calls (lazily, from
`case_runner.DefaultCaseRunner._resume`) for every case with `kind: resume`. It answers one
question per case and arm: does a flag change what the model does on the leg that FOLLOWS an
approve or reject, in the right direction, without regressing anything else.

WHAT RUNS FOR REAL
  Both legs go through the production views and loop: `POST /ask/` (the real view builds the
  thread / note summary block, the @/# references block, the earlier turns), the pause on the
  approval-gated write, `POST /decide/` (the real view resumes through `resume_agent` and
  `_drive_loop`), the real write tool on approve (in the scenario's OWN team, deleted at the
  end), and, for an undo case, the production `undo_apply.perform_undo`.

WHAT IS CONTROLLED
  * the FIRST leg's model is scripted: it proposes the case's write and nothing else, so the
    pause is identical and free in every arm. Only the legs after the decision use the model
    under test (`controller.get_model_client`, whatever it returns when the runner is called);
  * the thread / note summary is the case's own text (`summarise_thread` / `summarise_note`
    return it), so there is no summary LLM call and the "fact only the block carries" is known;
  * the conversation recap LLM call and the conversation ingestion (embeddings) are switched
    off; keep `earlier_turns` within the session window so no recap is needed;
  * the quota gate and the HTTP throttle are not what is measured, so they are stepped over;
  * overrides (`SEARCH_ENGINE` keys) apply around the WHOLE scenario, ask included, through the
    same mechanism `agent_eval_compare` uses, because a flag can act at the pause (it decides
    what is kept) as well as at the decision.

THE RESULT is `usage.CaseResult` (`case_id, arm, lang, passed, checks, tools, answer, usage`),
plus `resumed_prompt` (the system instruction and messages of the resumed leg's first model
call, which the prompt checks read), `pending` (a corrected proposal, if the model made one)
and `proposed` (that proposal's tool and truncated arguments, as in every result row). Deterministic checks first; no judge.

CASE SCHEMA (`origin/search_engine/agent/evals/group/<tag>.yaml`, `kind: resume`)

    - id: t977.thread-owner.approve.en
      kind: resume
      lang: en
      tags: [eval-group, t977]
      query: "Make a task for this thread, then tell me who owns it."
      thread:                      # optional: the ask is "about this thread"
        messages: ["Ann: ...", "Priya: ..."]   # the thread's rows (first is the root)
        summary: "THE SUMMARY BLOCK TEXT the first leg carries (stands for the LLM summary)"
      note: {title: ..., body: ..., summary: ...}     # optional: the ask is "about this note"
      tasks: [{key: login, title: "Fix login flow"}]  # optional rows for @/# references
      mentions: [{type: task, task: login}]           # `task` is a key of `tasks`
      earlier_turns: [{q: "...", a: "..."}]           # the session's earlier turns (991)
      proposal: {tool: create_task, args: {title: ..., priority: Low}}   # {project_id} is filled
      decision: approve | reject
      reason: "..."                # reject only (889)
      edited_args: {title: ...}    # approve only (889)
      undo: true                   # then undo the write (production undo), before the follow-up
      follow_up: "is it still there?"   # a further ask in the same session; checks run on it
      expect:
        resumed_prompt_contains: ["..."]      # in the resumed leg's first call (prompt + messages)
        resumed_prompt_excludes: ["..."]
        answer_contains_any: ["..."]  answer_contains_all: [...]  answer_excludes: [...]
        tools_called: [...]  tools_not_called: [...]
        pending_tool: create_task             # the model proposed another write after a reject
        pending_args_contain: {priority: High}
        max_calls: 6

    - scenario: closing_line       # MIP-1283: no resume at all; a task closed in its origin thread
      ...                          # (see `run_closing_line_case`)
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import threading
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest import mock

from origin.search_engine.agent.evals.group import usage as group_usage

log = logging.getLogger(__name__)

# The runner patches process-global seams (the model client, the quota gate, throttles,
# summaries), and the harness may run cases on a thread pool: one resume case at a time.
_RUN_LOCK = threading.Lock()

ASK_URL = "/api/v2/agent/ask/"
DECIDE_URL = "/api/v2/agent/decide/"
SESSION_WINDOW_NOTE = "earlier_turns must fit the session window (no recap is built in an eval)"


# --------------------------------------------------------------------------- #
# The scripted first leg                                                       #
# --------------------------------------------------------------------------- #


# The dummy signature Google documents for function calls that were not produced by the model.
SCRIPTED_CALL_SIGNATURE = base64.urlsafe_b64decode("skip_thought_signature_validator")


class ScriptedFirstLeg:
    """The model of the ask leg: proposes the case's write, then (if the loop asks again)
    stops. It is never asked a second time for a case that pauses on its first call."""

    def __init__(self, tool: str, args: dict[str, Any]):
        self.tool, self.args = tool, dict(args)
        self.calls = 0

    def generate_step(self, messages, tools, system_instruction, **kwargs):
        from origin.search_engine.llm.types import FunctionCall  # noqa: PLC0415

        self.calls += 1
        if self.calls == 1:
            # Gemini 3 rejects a replayed function call that has no thought signature (400
            # "missing a thought_signature"). This call is scripted, not the model's, so it
            # carries Google's documented dummy that skips that validation.
            yield (
                None,
                FunctionCall(
                    name=self.tool,
                    args=dict(self.args),
                    thought_signature=SCRIPTED_CALL_SIGNATURE,
                ),
            )
        else:
            yield ("(no further action)", None)

    def __getattr__(self, name: str) -> Any:  # anything else the loop may probe for
        raise AttributeError(name)


class RecordingClient:
    """Delegates to the model under test and records every call's prompt, untouched."""

    def __init__(self, inner: Any):
        self._inner = inner
        self.calls: list[dict[str, Any]] = []

    def generate_step(self, messages, tools, system_instruction, **kwargs):
        self.calls.append({"system": system_instruction, "messages": copy.deepcopy(messages)})
        return self._inner.generate_step(messages, tools, system_instruction, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def prompt_text(call: dict[str, Any]) -> str:
    """One call's whole prompt as text: the system instruction, then every message."""
    parts = [call.get("system") or ""]
    for m in call.get("messages") or []:
        if m.function_call is not None:
            parts.append(
                f"[call {m.function_call.name} {json.dumps(m.function_call.args, default=str, ensure_ascii=False)}]"
            )
        elif m.function_response_name is not None:
            parts.append(
                f"[result {m.function_response_name} {json.dumps(m.function_response, default=str, ensure_ascii=False)}]"
            )
        else:
            parts.append(f"[{m.role}] {m.text or ''}")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# The scenario world: its own team, torn down at the end                       #
# --------------------------------------------------------------------------- #


class World:
    """A team of its own (never the v1 fixture team) with the rows one case needs."""

    def __init__(self, case: dict[str, Any]):
        from django.contrib.auth import get_user_model  # noqa: PLC0415

        from origin.models.common.team_models import TeamMaster, TeamMembers  # noqa: PLC0415
        from origin.models.project.prj_models import ProjectMaster, ProjectMembers  # noqa: PLC0415
        from origin.search_engine import quota  # noqa: PLC0415

        self.case = case
        tag = uuid.uuid4().hex[:8]
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username=f"evalresume-{tag}", email=f"evalresume-{tag}@example.invalid", password=None
        )
        self.user.tier = "pro"
        self.user.save(update_fields=["tier"])
        quota.invalidate_effective_tier([self.user.id])
        self.other = user_model.objects.create_user(
            username=f"evalresume-other-{tag}",
            email=f"evalresume-other-{tag}@example.invalid",
            password=None,
        )
        self.team = TeamMaster.objects.create(
            team_name=f"eval-resume-{tag}",
            team_email=f"evalresume-{tag}@example.invalid",
            owner=self.user,
        )
        for member in (self.user, self.other):
            TeamMembers.objects.create(team=self.team, attendee=member)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code=f"E{tag[:4].upper()}",
            owner=self.user,
            project_system_user=self.user,
        )
        for member in (self.user, self.other):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        self.tasks: dict[str, Any] = {}
        self.thread_context: dict[str, Any] | None = None
        self.note_context: dict[str, Any] | None = None
        self.summaries: dict[str, str] = {}
        self._build(case)

    def _build(self, case: dict[str, Any]) -> None:
        from origin.models.chat.unified_models import Channel, ChannelMember, Message  # noqa: PLC0415
        from origin.models.note.personal_note_models import PersonalNoteMaster  # noqa: PLC0415
        from origin.models.task.task_models import TaskMaster  # noqa: PLC0415

        for spec in case.get("tasks") or []:
            self.tasks[spec["key"]] = TaskMaster.objects.create(
                team=self.team,
                project=self.project,
                reporter=self.user,
                assignee=self.user,
                title=spec["title"],
                status=spec.get("status", "Open"),
                priority=spec.get("priority", "Low"),
            )
        thread = case.get("thread")
        if thread:
            channel = Channel.objects.create(team=self.team, kind=2, title="eval-thread")
            for member in (self.user, self.other):
                ChannelMember.objects.create(channel=channel, user=member, role="owner")
            root = None
            # Who wrote the first message: `user` (the asker of a resume case) by default;
            # `other` for the closing-line scenario, where the asker is the CLIENT.
            first, second = (
                (self.other, self.user)
                if thread.get("first_sender") == "other"
                else (self.user, self.other)
            )
            for seq, text in enumerate(thread["messages"], start=1):
                msg = Message.objects.create(
                    channel=channel,
                    sender=first if seq % 2 else second,
                    seq=seq,
                    body=_blocks(text),
                    body_text=text,
                    **(
                        {}
                        if root is None
                        else {"parent": root, "thread_root": root, "is_thread_reply": True}
                    ),
                )
                root = root or msg
            self.channel, self.root = channel, root
            self.thread_context = {
                "chat_type": 2,
                "chat_id": str(channel.id),
                "thread_id": str(root.id),
            }
            if thread.get("summary"):
                self.summaries["thread"] = thread["summary"]
        note = case.get("note")
        if note:
            row = PersonalNoteMaster.objects.create(
                team=self.team, owner=self.user, title=note["title"], body=_blocks(note["body"])
            )
            self.note_context = {"note_type": 1, "note_id": row.note_id}
            self.summaries["note"] = note["summary"]

    def mentions(self) -> list[dict[str, Any]]:
        out = []
        for spec in self.case.get("mentions") or []:
            task = self.tasks[spec["task"]]
            out.append({"type": "task", "task_id": task.task_id, "label": task.title})
        return out

    def fill(self, value: Any) -> Any:
        """`{project_id}` / `{task:<key>}` placeholders in a case's proposal arguments."""
        if value == "{project_id}":
            return self.project.project_id
        if isinstance(value, str):
            value = value.replace("{project_id}", str(self.project.project_id))
            for key, task in self.tasks.items():
                value = value.replace("{task:%s}" % key, str(task.task_id))
            return value
        if isinstance(value, dict):
            return {k: self.fill(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.fill(v) for v in value]
        return value

    def teardown(self) -> None:
        """Delete the team and everything under it; best effort, never raises."""
        try:
            from origin.models.chat.unified_models import Channel, Message  # noqa: PLC0415
            from origin.models.common.team_models import TeamMaster  # noqa: PLC0415

            # The ask and decision views wrote AgentRun / AgentSession rows, and their
            # `team_id` is a bare string, not a foreign key: deleting the team leaves them
            # (and their steps, which cascade from the run) behind forever (MIP-1471). Runs
            # first: `AgentRun.session` is SET_NULL, but a session row outliving its runs is
            # the same orphan.
            delete_agent_rows(self.team.pk)
            # `Channel.team` and `Message.channel` are PROTECT: replies, then messages, then
            # channels go first.
            Message.objects.filter(channel__team=self.team, is_thread_reply=True).delete()
            Message.objects.filter(channel__team=self.team).delete()
            Channel.objects.filter(team=self.team).delete()
            TeamMaster.objects.filter(pk=self.team.pk).delete()
            self.user.delete()
            self.other.delete()
        except Exception:  # noqa: BLE001
            log.warning(
                "eval resume: teardown of team %s failed",
                getattr(self.team, "pk", "?"),
                exc_info=True,
            )


def delete_agent_rows(team_id: Any) -> None:
    """Delete the agent history rows an eval team's asks left (they name the team by string).

    `AgentStep`, `AgentLlmCall`, `AgentRunJudgement` and `AgentRunFeedback` cascade from the
    run; the thread / note summary caches carry the team as a bare string too. Accounting rows
    (`AiSpendEvent`, `AiRequestCost`) are history and are kept."""
    from origin.search_engine.models import (  # noqa: PLC0415
        AgentRun,
        AgentSession,
        NoteSummary,
        ThreadSummary,
    )

    team = str(team_id)
    AgentRun.objects.filter(team_id=team).delete()
    AgentSession.objects.filter(team_id=team).delete()
    ThreadSummary.objects.filter(team_id=team).delete()
    NoteSummary.objects.filter(team_id=team).delete()


def _blocks(text: str) -> list[dict[str, Any]]:
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


# --------------------------------------------------------------------------- #
# Driving the production views                                                 #
# --------------------------------------------------------------------------- #


def _locale(case: dict[str, Any]) -> str:
    """The UI locale the case is asked in, as production receives it: the request body's
    `locale` (the harness hands each case's language as `case["locale"]`; a case run directly
    falls back to its own `lang`). `/ask/` resolves it into `ToolContext.locale` and keeps it
    on the run, so `/decide/` resumes the SECOND leg in the same language, and the controller
    appends `language_directive(locale)` as the last line of the system instruction."""
    return str(case.get("locale") or case.get("lang") or "")


def _client(user: Any) -> Any:
    from rest_framework.test import APIClient  # noqa: PLC0415
    from rest_framework_simplejwt.tokens import RefreshToken  # noqa: PLC0415

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
    return client


def _events(response: Any) -> list[dict[str, Any]]:
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.content[:300]!r}")
    text = b"".join(response.streaming_content).decode()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _pending(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    found = [e for e in events if e.get("type") == "tool_call_pending_approval"]
    return found[-1] if found else None


def _answer(events: list[dict[str, Any]]) -> str:
    return "".join(e.get("text", "") for e in events if e.get("type") == "answer_delta")


def _tools(events: list[dict[str, Any]]) -> list[str]:
    return [str(e.get("tool_name")) for e in events if e.get("type") == "tool_call_start"]


def _seed_earlier_turns(world: World, turns: list[dict[str, str]]) -> str | None:
    """A session holding the earlier turns as finished runs, written straight to the table
    (the production ask reads its window from these rows)."""
    if not turns:
        return None
    from origin.search_engine.models import AgentRun, AgentSession  # noqa: PLC0415

    session = AgentSession.objects.create(team_id=str(world.team.pk), user_id=str(world.user.id))
    for turn in turns:
        AgentRun.objects.create(
            team_id=str(world.team.pk),
            user_id=str(world.user.id),
            session=session,
            query=turn["q"],
            final_answer_text=turn["a"],
            status="done",
        )
    return str(session.session_id)


class RecordingFactory:
    """Stands in for `controller.get_model_client`: hands back the model under test (whatever
    `get_model_client` returns when the runner starts) wrapped to record every prompt."""

    def __init__(self, real_get: Any):
        self._real_get = real_get
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        client = RecordingClient(self._real_get(*args, **kwargs))
        client.calls = self.calls  # one list for every client handed out
        return client


@contextmanager
def _bound_to(usage: Any) -> Iterator[None]:
    """Make the views' worker threads spend under THIS capture's request id.

    `/ask/` and `/decide/` run the loop on a bare thread that builds its own spend context from
    `metered.spend_kwargs_for(...)` (a thread inherits no contextvars), so without this every
    call the model under test makes would land in `UNATTRIBUTED`, not on the case. The kwargs are
    the real ones with only `request_id` replaced.
    """
    from origin.search_engine import metered  # noqa: PLC0415

    real = metered.spend_kwargs_for

    def bound(*args: Any, **kwargs: Any) -> dict[str, Any]:
        out = real(*args, **kwargs)
        out["request_id"] = usage.request_id
        return out

    with mock.patch.object(metered, "spend_kwargs_for", side_effect=bound):
        yield


def _common_patches(stack: ExitStack, world: World) -> None:
    """The seams stepped over: a cost, a quota or a throttle, never the subject."""
    from origin.search_engine import agent_views  # noqa: PLC0415
    from origin.search_engine.agent import multi_turn  # noqa: PLC0415

    stack.enter_context(
        mock.patch.object(agent_views, "check_remaining", return_value=(True, 0, None))
    )
    stack.enter_context(
        mock.patch("origin.search_engine.ingestion.ingest_conversation_run", return_value=False)
    )
    stack.enter_context(
        mock.patch.object(multi_turn, "_generate", side_effect=RuntimeError(SESSION_WINDOW_NOTE))
    )
    for view in (agent_views.AgentAskView, agent_views.AgentDecideView):
        stack.enter_context(mock.patch.object(view, "throttle_classes", []))
    if "thread" in world.summaries:
        stack.enter_context(
            mock.patch(
                "origin.search_engine.agent.thread_summary.summarise_thread",
                return_value=(world.summaries["thread"], "eval:fixed"),
            )
        )
    if "note" in world.summaries:
        stack.enter_context(
            mock.patch(
                "origin.search_engine.agent.note_summary.summarise_note",
                return_value=(world.summaries["note"], "eval:fixed"),
            )
        )


# --------------------------------------------------------------------------- #
# Checks                                                                       #
# --------------------------------------------------------------------------- #


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail}


def _contains(haystack: str, needles: list[str]) -> list[str]:
    low = haystack.lower()
    return [n for n in needles if str(n).lower() not in low]


def evaluate(
    expect: dict[str, Any],
    *,
    answer: str,
    tools: list[str],
    resumed_prompt: str,
    follow_up_prompt: str,
    final_prompt: str,
    pending: dict[str, Any] | None,
    calls: int,
) -> list[dict[str, Any]]:
    """The case's `expect:` block, deterministic checks only. Prompt checks come first: they
    say whether the flag reached the model at all, before the answer says what it did."""
    checks: list[dict[str, Any]] = []
    for key, text in (
        ("resumed_prompt", resumed_prompt),
        ("follow_up_prompt", follow_up_prompt),
        ("final_prompt", final_prompt),
    ):
        want = expect.get(f"{key}_contains") or []
        if want:
            missing = _contains(text, want)
            checks.append(
                _check(
                    f"{key}_contains", not missing, f"missing {missing}" if missing else "present"
                )
            )
        unwanted = expect.get(f"{key}_excludes") or []
        if unwanted:
            present = [n for n in unwanted if str(n).lower() in text.lower()]
            checks.append(
                _check(f"{key}_excludes", not present, f"found {present}" if present else "absent")
            )
    if expect.get("answer_contains_all"):
        missing = _contains(answer, expect["answer_contains_all"])
        checks.append(
            _check(
                "answer_contains_all",
                not missing,
                f"missing {missing}" if missing else "all present",
            )
        )
    if expect.get("answer_contains_any"):
        hit = [n for n in expect["answer_contains_any"] if str(n).lower() in answer.lower()]
        checks.append(
            _check(
                "answer_contains_any", bool(hit), f"found {hit}" if hit else "none of them present"
            )
        )
    if expect.get("answer_excludes"):
        found = [n for n in expect["answer_excludes"] if str(n).lower() in answer.lower()]
        checks.append(_check("answer_excludes", not found, f"found {found}" if found else "absent"))
    if expect.get("tools_called"):
        missing = [t for t in expect["tools_called"] if t not in tools]
        checks.append(
            _check(
                "tools_called",
                not missing,
                f"missing {missing}, called {tools}" if missing else f"called {tools}",
            )
        )
    if expect.get("tools_not_called"):
        found = [t for t in expect["tools_not_called"] if t in tools]
        checks.append(
            _check("tools_not_called", not found, f"called {found}" if found else "none called")
        )
    if "pending_tool" in expect:
        want = expect["pending_tool"]
        got = (pending or {}).get("tool_name")
        checks.append(_check("pending_tool", got == want, f"want {want!r}, got {got!r}"))
    if expect.get("pending_args_contain"):
        args = (pending or {}).get("arguments") or {}
        bad = {
            k: (args.get(k), v)
            for k, v in expect["pending_args_contain"].items()
            if str(v).lower() not in str(args.get(k, "")).lower()
        }
        checks.append(
            _check(
                "pending_args_contain", not bad, f"mismatch (got, want) {bad}" if bad else "matched"
            )
        )
    if "no_pending" in expect:
        checks.append(
            _check(
                "no_pending",
                (pending is None) == bool(expect["no_pending"]),
                f"pending={pending is not None}",
            )
        )
    if expect.get("max_calls") is not None:
        checks.append(
            _check("max_calls", calls <= int(expect["max_calls"]), f"{calls} model calls")
        )
    return checks


# --------------------------------------------------------------------------- #
# The runners                                                                  #
# --------------------------------------------------------------------------- #


def _empty(case: dict[str, Any]) -> dict[str, Any]:
    result = group_usage.new_result(str(case.get("id", "")), "", str(case.get("lang", "en")))
    result.update({"resumed_prompt": "", "pending": None})
    return result


def run_resume_case(case: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """One case under one arm's overrides. Never raises: a crash is `error` and a failed result.

    The ask leg and the decision run INSIDE the overrides (a flag may act at the pause).
    `usage` covers the legs after the decision only: the first leg's model is scripted.
    """
    with _RUN_LOCK:
        if case.get("scenario") == "closing_line":
            return _run_closing_line_case(case, overrides)
        return _run_resume_case(case, overrides)


def _run_resume_case(case: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    from origin.search_engine.agent import controller  # noqa: PLC0415
    from origin.search_engine.management.commands.agent_eval_compare import (  # noqa: PLC0415
        _override_search_engine,
    )

    result = _empty(case)
    world: World | None = None
    try:
        with _override_search_engine(dict(overrides or {})):
            world = World(case)
            _drive(case, world, controller, result)
    except Exception as exc:  # noqa: BLE001 - a case that crashed is a failed case, not a dead run
        log.exception("resume case %s crashed", case.get("id"))
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        result["passed"] = False
    finally:
        if world is not None:
            world.teardown()
    return result


def _drive(case: dict[str, Any], world: World, controller: Any, result: dict[str, Any]) -> None:
    from origin.search_engine.agent import undo_apply  # noqa: PLC0415
    from origin.search_engine.models import AgentRun, AgentStep  # noqa: PLC0415

    proposal = case["proposal"]
    real_get = controller.get_model_client
    first_leg = ScriptedFirstLeg(proposal["tool"], world.fill(proposal.get("args") or {}))
    factory = RecordingFactory(real_get)
    client = _client(world.user)
    checks: list[dict[str, Any]] = []

    with ExitStack() as stack:
        # ONE capture and ONE binding around BOTH legs (MIP-1504). With the cost meter on, `/ask/`
        # opens an `AiRequestCost` row under the request id it was bound to and `/decide/`
        # REJOINS that id; bound only around the decision, the resumed leg's calls were charged
        # to the ask's random id, landed in UNATTRIBUTED, and the row said calls=0.
        usage = stack.enter_context(group_usage.capture_usage())
        stack.enter_context(_bound_to(usage))
        _common_patches(stack, world)
        # ---- first leg: scripted, free, identical in every arm -------------------------
        payload: dict[str, Any] = {
            "query": case["query"],
            "team_id": str(world.team.team_id),
            "locale": _locale(case),
        }
        if world.thread_context:
            payload["thread_context"] = world.thread_context
        if world.note_context:
            payload["note_context"] = world.note_context
        if case.get("mentions"):
            payload["mentions"] = world.mentions()
        session_id = _seed_earlier_turns(world, case.get("earlier_turns") or [])
        if session_id:
            payload["session_id"] = session_id
        with mock.patch.object(controller, "get_model_client", return_value=first_leg):
            events = _events(client.post(ASK_URL, payload, format="json"))
        pending = _pending(events)
        checks.append(
            _check(
                "paused_on_the_write",
                pending is not None,
                "first leg paused" if pending else f"no pause: {events[-3:]}",
            )
        )
        if pending is None:
            result["checks"] = checks
            return
        run = AgentRun.objects.get(run_id=pending["run_id"])
        resume_context = run.resume_context

        # ---- the legs after the decision: the model under test --------------------------
        stack.enter_context(mock.patch.object(controller, "get_model_client", side_effect=factory))
        tools: list[str] = []
        answer = ""
        body: dict[str, Any] = {
            "run_id": pending["run_id"],
            "approval_token": pending["approval_token"],
            "decision": case.get("decision", "approve"),
        }
        if case.get("reason"):
            body["reason"] = case["reason"]
        if case.get("edited_args"):
            body["edited_args"] = world.fill(case["edited_args"])
        events = _events(client.post(DECIDE_URL, body, format="json"))
        tools += _tools(events)
        answer = _answer(events)
        resumed_calls = list(factory.calls)
        resumed_prompt = prompt_text(resumed_calls[0]) if resumed_calls else ""
        new_pending = _pending(events)
        follow_up_prompt = ""
        if case.get("undo"):
            step = AgentStep.objects.get(run__run_id=run.run_id, tool_name=proposal["tool"])
            try:
                done = undo_apply.perform_undo(run=run, step_id=step.step_id, user=world.user)
                checks.append(_check("undo_performed", True, str(done.get("action"))))
            except Exception as exc:  # noqa: BLE001 - UndoRefused and friends
                checks.append(_check("undo_performed", False, f"{type(exc).__name__}: {exc}"[:200]))
        if case.get("follow_up"):
            before = len(factory.calls)
            run.refresh_from_db()
            events = _events(
                client.post(
                    ASK_URL,
                    {
                        "query": case["follow_up"],
                        "team_id": str(world.team.team_id),
                        "locale": _locale(case),
                        "session_id": str(run.session_id),
                    },
                    format="json",
                )
            )
            tools += _tools(events)
            answer = _answer(events)
            new_pending = _pending(events) or new_pending
            if len(factory.calls) > before:
                follow_up_prompt = prompt_text(factory.calls[before])
        final_prompt = prompt_text(factory.calls[-1]) if factory.calls else ""

    if new_pending is not None:
        # The raw arguments of what the model proposed next, from its own step row.
        step = (
            AgentStep.objects.filter(run__run_id=pending["run_id"])
            .order_by("-step_index", "-step_id")
            .first()
        )
        new_pending = {
            **new_pending,
            "arguments": (step.arguments_json if step else None)
            or new_pending.get("arguments")
            or {},
        }
    checks += evaluate(
        case.get("expect") or {},
        answer=answer,
        tools=tools,
        resumed_prompt=resumed_prompt,
        follow_up_prompt=follow_up_prompt,
        final_prompt=final_prompt,
        pending=new_pending,
        calls=len(factory.calls),
    )
    result["checks"] = checks
    result["tools"] = tools
    result["answer"] = answer
    result["usage"] = usage.as_dict()
    result["resumed_prompt"] = resumed_prompt
    result["pending"] = new_pending
    if new_pending is not None:
        from origin.search_engine.agent.evals.group import content_checks  # noqa: PLC0415

        result["proposed"] = content_checks.summarize_proposals(
            [new_pending], group_usage.PROPOSED_ARGS_CHARS
        )
    result["kept_context"] = bool(resume_context)
    result["passed"] = all(c["ok"] for c in checks)


# --------------------------------------------------------------------------- #
# MIP-1283: a task closed in the thread it was made from                       #
# --------------------------------------------------------------------------- #


def _run_closing_line_case(case: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """The client asks the agent, in an ask of her own, whether her request in a thread was
    done. The task made from that thread was closed UNDER THE ARM'S OVERRIDES, so the closing
    line exists only in the arm that has the flag on (the sequence fixture is seeded once and
    cannot show the difference). The model under test is the whole run: no scripted leg.

        - id: t1283.client-asks.en
          kind: resume
          scenario: closing_line
          lang: en
          tags: [eval-group, t1283]
          thread: {messages: ["Client: Please fix the export bug", "PM: On it"]}
          task_title: "Fix the export bug"
          query: "Did my request in chat {chat_id} thread {thread_id} get done?"
          expect: {tools_called: [fetch_chat_thread], answer_contains_any: ["closed"]}
    """
    from origin.search_engine.agent import controller  # noqa: PLC0415
    from origin.search_engine.management.commands.agent_eval_compare import (  # noqa: PLC0415
        _override_search_engine,
    )

    result = _empty(case)
    world: World | None = None
    try:
        with _override_search_engine(dict(overrides or {})):
            world = World({k: v for k, v in case.items() if k not in ("tasks", "note")})
            _drive_closing_line(case, world, controller, result)
    except Exception as exc:  # noqa: BLE001
        log.exception("closing-line case %s crashed", case.get("id"))
        result["error"] = f"{type(exc).__name__}: {exc}"[:300]
        result["passed"] = False
    finally:
        if world is not None:
            world.teardown()
    return result


def _drive_closing_line(
    case: dict[str, Any], world: World, controller: Any, result: dict[str, Any]
) -> None:
    from origin.models.task.task_models import TaskMaster  # noqa: PLC0415

    # World(thread=...) made the channel and the thread: `other` is the client (author of the
    # root), `user` is the PM. The task is the PM's, made from the thread.
    task = TaskMaster.objects.create(
        team=world.team,
        project=world.project,
        reporter=world.user,
        assignee=world.user,
        title=case.get("task_title", "Task"),
        status="Open",
        chat_id=str(world.channel.id),
        thread_id=str(world.root.id),
    )
    if case.get("close", True):
        task.status = "Closed"
        task.save()  # the TaskMaster signal posts the closing line iff the flag is on
        if case.get("reopen_and_close_again"):
            for status in ("WIP", "Closed"):
                task.status = status
                task.save()
    factory = RecordingFactory(controller.get_model_client)
    client = _client(world.other)
    query = (
        case["query"]
        .replace("{chat_id}", str(world.channel.id))
        .replace("{thread_id}", str(world.root.id))
        .replace("{display_id}", task.display_id)
    )
    with ExitStack() as stack:
        _common_patches(stack, world)
        stack.enter_context(mock.patch.object(controller, "get_model_client", side_effect=factory))
        with group_usage.capture_usage() as usage, _bound_to(usage):
            events = _events(
                client.post(
                    ASK_URL,
                    {
                        "query": query,
                        "team_id": str(world.team.team_id),
                        "locale": _locale(case),
                    },
                    format="json",
                )
            )
    answer, tools = _answer(events), _tools(events)
    final_prompt = prompt_text(factory.calls[-1]) if factory.calls else ""
    checks = evaluate(
        case.get("expect") or {},
        answer=answer,
        tools=tools,
        resumed_prompt="",
        follow_up_prompt="",
        final_prompt=final_prompt,
        pending=_pending(events),
        calls=len(factory.calls),
    )
    result["checks"], result["tools"], result["answer"] = checks, tools, answer
    result["usage"] = usage.as_dict()
    result["passed"] = all(c["ok"] for c in checks)
