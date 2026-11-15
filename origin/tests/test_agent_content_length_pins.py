"""Flag pins for what the agent loop sends the model (MIP-905: AGENT_CONTENT_LENGTH and
AGENT_BODY_ON_USER_MODEL).

Their contract is "flag off = what `origin/main` sends, byte for byte", and for the length
flag also "flag on + a Standard member = the same bytes". This module pins that with sha256
goldens recorded on PRISTINE `origin/main` (09f52388, a clean worktree with none of the change)
and pins the on-states against them.

THE RECORDING (how `GOLDEN` below was made; run it again after anything that moves the prompt on
main, and compare, rather than editing a hash to make a test pass):

    git -C <genos-api> worktree add --detach <wt> origin/main
    cp origin/tests/test_agent_content_length_pins.py <wt>/origin/tests/
    # this module imports nothing the change adds (`agent.content_length` is imported lazily,
    # inside the on-state tests only), so it runs on code that has never heard of the flags
    RECORD_GOLDEN=1 python manage.py test origin.tests.test_agent_content_length_pins.FlagOffPins --noinput
    # prints  GOLDEN <scenario> = "<sha256>"  for every scenario; paste them below

What is hashed, per scenario: EVERY model call of one `_drive_loop` run, each as what the
provider adapter is handed: the system instruction (date line off, ids normalised), the full
message list, every tool declaration (name, description, parameter schema), the
`GenerationParams` (the output cap and the thinking budget) and the `model_override`. A flag
that leaks into the off state in the prompt, the tool surface, a request parameter, or a call
that should not exist changes a hash.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import re
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from origin.models.common.team_models import TeamMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.llm.choice import LlmChoice, reset_llm_choice, set_llm_choice
from origin.search_engine.llm.types import AgentMessage, FunctionCall

# sha256 of every model call of one run, per scenario, recorded on pristine origin/main
# (see the module docstring for the recipe).
# MIP-1533: the plan-bearing scenarios (all but `digest-like`) were re-recorded after the
# `create_task_plan` declaration gained its body nudge (tool description + `content_markdown`).
GOLDEN: dict[str, str] = {
    "plain": "686a8bf3ffe810cb56eae326ba8ebea4804024392a9e058bf6b56071046358bb",
    "plain-ja": "a6737ba19e81b7a233d008f34ee6635de1f7479b389fa76291afdc7a016319e6",
    "effort-low/gemini": "0ea5dac2b5b81ba6330a598b1e3c27e380ee69b7b918d234cc000c489d6dee44",
    "effort-low-thinking/gemini": "4e3ab24144dbad4bb5f3e9141fbd861cb7601ced68b22e884f90f65b0f3f187d",
    "effort-medium/gemini": "686a8bf3ffe810cb56eae326ba8ebea4804024392a9e058bf6b56071046358bb",
    "digest-like": "264c650056a9a57553446df29282232cb5d8aeb714ecdc197d82bef074f6f6cd",
    "two-tier-write": "8cd611641eb86bb62e567112953e4359da58e949db4289fd20aa366a026f6ea6",
}

PLAN_BODY = "### Summary\nShip it.\n\n### Acceptance criteria\n- done\n"
TASK_WITH_BODY = {"title": "T", "project_id": 1, "content_text": PLAN_BODY}

# Flags every scenario runs under. The date line is off: a date in the hash would make the pin
# depend on the day it runs.
NO_DATE = {"AGENT_INJECT_CURRENT_DATE": False}
EFFORT = {"AGENT_EFFORT_LEVELS": True}
TWO_TIER = {"AGENT_EFFORT_LEVELS": True, "AGENT_TWO_TIER_LOOP": True}


class RecordingClient:
    """A scripted model that records, per call, everything the provider adapter is handed."""

    def __init__(self, script):
        self._script = list(script)
        self.calls: list[dict] = []

    def generate_step(
        self,
        messages,
        tools,
        system_instruction,
        *,
        model_override=None,
        usage_sink=None,
        params=None,
    ):
        self.calls.append(
            {
                "system": system_instruction,
                "messages": copy.deepcopy(messages),
                "tools": [(t.name, t.description, t.parameters_schema) for t in tools],
                "params": dataclasses.asdict(params) if params is not None else None,
                "model_override": model_override,
            }
        )
        step = self._script.pop(0) if self._script else [("All done.", None)]
        yield from step


def answer(text="All done."):
    return [(text, None)]


def call(name, args):
    return [(None, FunctionCall(name=name, args=dict(args)))]


class FakeApprovalTool:
    """An approval-gated stand-in for a real write tool that declares the REAL schema and
    description, so the loop declares exactly what production does."""

    requires_approval = True

    def __init__(self, name):
        real = REGISTRY[name]
        self.name = name
        self.description = real.description
        self.parameters_schema = real.parameters_schema
        self.calls: list[dict] = []

    def run(self, args, ctx):
        self.calls.append(dict(args))
        return {"__summary__": "ok"}


def normalised(text):
    return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", text)


def transcript(calls: list[dict]) -> list[dict]:
    out = []
    for c in calls:
        out.append(
            {
                "system": normalised(c["system"]),
                "messages": [
                    json.loads(normalised(json.dumps(dataclasses.asdict(m), default=str)))
                    for m in c["messages"]
                ],
                "tools": c["tools"],
                "params": c["params"],
                "model_override": c["model_override"],
            }
        )
    return out


def digest_of(calls: list[dict]) -> str:
    blob = json.dumps(transcript(calls), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class LoopBase(TestCase):
    """One `_drive_loop` run against a recording client, flags and choice chosen per test."""

    flags: dict = {}

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(
            username="cl-asker", email="cl-asker@example.com", password="x"
        )
        self.team = TeamMaster.objects.create(
            team_name="T", team_email="t@example.com", owner=self.user
        )
        self.enterContext(override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **NO_DATE}))
        self.fakes: dict[str, FakeApprovalTool] = {}

    def settings_with(self, **flags):
        return override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **NO_DATE, **flags})

    def run_loop(
        self,
        script,
        *,
        flags=None,
        choice: LlmChoice | None = None,
        locale: str = "",
        disabled_tools: set[str] | None = None,
        fake_writes=("create_task",),
        query: str = "write the spec",
    ) -> RecordingClient:
        client = RecordingClient(script)
        events: list[dict] = []
        patches = [
            mock.patch.object(controller, "get_model_client", return_value=client),
            mock.patch.dict(
                REGISTRY, {n: self.fakes.setdefault(n, FakeApprovalTool(n)) for n in fake_writes}
            ),
        ]
        token = set_llm_choice(choice) if choice is not None else None
        try:
            with self.settings_with(**(flags or {})):
                for p in patches:
                    p.start()
                try:
                    controller._drive_loop(
                        messages=[AgentMessage(role="user", text=query)],
                        ctx=ToolContext(
                            team_id=str(self.team.team_id), user_id=str(self.user.id), locale=locale
                        ),
                        emit=events.append,
                        run_id=None,
                        starting_step=0,
                        seen_sources_by_id={},
                        disabled_tools=disabled_tools,
                    )
                finally:
                    for p in reversed(patches):
                        p.stop()
        finally:
            if token is not None:
                reset_llm_choice(token)
        self.events = events
        return client


def writes_disabled() -> set[str]:
    return {t.name for t in REGISTRY.values() if t.requires_approval}


# name -> a function (LoopBase) -> RecordingClient. Same scenarios in the off pins and in the
# on-with-Standard pins, so "the same bytes" is a comparison of like with like.
def _plain(t):
    return t.run_loop([answer()])


def _plain_ja(t):
    return t.run_loop([answer()], locale="ja")


def _effort_low_gemini(t):
    return t.run_loop(
        [answer()],
        flags=EFFORT,
        choice=LlmChoice(provider="gemini", model="gemini-3.5-flash-lite", effort="low"),
    )


def _effort_low_thinking_gemini(t):
    # AGENT_THINKING_BUDGETS on: Low pins thinking off (budget 0) next to its 4096 cap.
    return t.run_loop(
        [answer()],
        flags={**EFFORT, "AGENT_THINKING_BUDGETS": True},
        choice=LlmChoice(provider="gemini", model="gemini-3.5-flash-lite", effort="low"),
    )


def _effort_medium_gemini(t):
    return t.run_loop(
        [answer()],
        flags=EFFORT,
        choice=LlmChoice(provider="gemini", model="gemini-3.6-flash", effort="medium"),
    )


def _digest_like(t):
    return t.run_loop([answer("Nothing to report.")], disabled_tools=writes_disabled())


def _two_tier_write(t):
    # Medium two-tier loop: the planning model proposes a write with a body. (AGENT_BODY_ON_USER_MODEL
    # off: it is executed as it came back; the member's model is never asked.)
    return t.run_loop(
        [call("create_task", TASK_WITH_BODY)],
        flags=TWO_TIER,
        choice=LlmChoice(provider="gemini", model="gemini-3.6-flash", effort="medium"),
    )


SCENARIOS = {
    "plain": _plain,
    "plain-ja": _plain_ja,
    "effort-low/gemini": _effort_low_gemini,
    "effort-low-thinking/gemini": _effort_low_thinking_gemini,
    "effort-medium/gemini": _effort_medium_gemini,
    "digest-like": _digest_like,
    "two-tier-write": _two_tier_write,
}


class FlagOffPins(LoopBase):
    """Every flag at its default (off): exactly what origin/main sends."""

    def pin(self, key: str, client: RecordingClient):
        digest = digest_of(client.calls)
        if os.environ.get("RECORD_GOLDEN"):
            print(f'GOLDEN {key} = "{digest}"')
            return
        self.assertEqual(digest, GOLDEN[key], f"what the model is sent changed: {key}")

    def test_pins(self):
        for key, scenario in SCENARIOS.items():
            with self.subTest(scenario=key):
                self.pin(key, scenario(self))
