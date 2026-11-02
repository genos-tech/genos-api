"""The in-app agent takes the names people use (MIP-881).

`task_id` used to be an integer, so "close WRD-5" sent the model to a full
search to learn the number. These pin what replaced that: the argument is
declared as a reference, and the loop resolves it once, on arrival, so
everything after it (approval card, persisted step, resume, session cache) sees
one canonical id, while the tools keep receiving integers.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller, prompts, references
from origin.search_engine.agent.chip_syntax import CHIP_POINTER, CHIP_SYNTAX_HINT
from origin.search_engine.agent.prompts import AGENT_SYSTEM_PROMPT, AGENT_SYSTEM_PROMPT_REGISTRY
from origin.search_engine.agent.tools import REGISTRY, ToolContext, ToolError
from origin.search_engine.llm.types import AgentMessage, FunctionCall
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp.tools import CATALOG, input_schema

REFERENCE_ARGS = ("task_id", "parent_task_id", "milestone_id", "note_id")


def _se(**overrides: Any) -> dict[str, Any]:
    from django.conf import settings as dj_settings

    return {**dj_settings.SEARCH_ENGINE, **overrides}


class _Scripted:
    """Plays one model turn per call."""

    def __init__(self, *turns: Any) -> None:
        self.turns = list(turns)

    def generate_step(self, messages: Any, tools: Any, system_instruction: Any, **_: Any) -> Any:
        self.messages = list(messages)
        yield from self.turns.pop(0)


def _call(name: str, **args: Any) -> list[tuple[None, FunctionCall]]:
    return [(None, FunctionCall(name=name, args=args))]


DONE = [("done", None)]


class DeclarationTests(SimpleTestCase):
    def _declared(self) -> dict[str, Any]:
        return {d.name: d.parameters_schema for d in controller._build_tool_declarations()}

    def test_every_reference_argument_is_declared_as_a_string(self) -> None:
        for name, schema in self._declared().items():
            for arg, prop in (schema.get("properties") or {}).items():
                if arg in REFERENCE_ARGS:
                    self.assertEqual(prop["type"], "STRING", f"{name}.{arg}")

    def test_a_task_reference_says_which_forms_it_takes(self) -> None:
        prop = self._declared()["update_task"]["properties"]["task_id"]
        self.assertIn("PRJ-123", prop["description"])
        self.assertNotIn("Numeric", prop["description"])

    def test_a_reference_inside_a_batch_is_declared_too(self) -> None:
        schema = self._declared()["update_tasks_bulk"]
        item = schema["properties"]["updates"]["items"]["properties"]
        self.assertEqual(item["task_id"]["type"], "STRING")
        self.assertIn("display id", item["milestone_id"]["description"])

    def test_a_milestone_keeps_its_own_wording_and_gains_the_hint(self) -> None:
        prop = self._declared()["delete_milestone"]["properties"]["milestone_id"]
        self.assertIn("display id", prop["description"])
        self.assertIn(
            REGISTRY["delete_milestone"]
            .parameters_schema["properties"]["milestone_id"]["description"]
            .rstrip(),
            prop["description"],
        )

    def test_the_registry_schema_is_left_alone(self) -> None:
        self._declared()
        # MCP builds its own declaration from this, and the tools read it.
        schema = REGISTRY["fetch_task"].parameters_schema
        self.assertEqual(schema["properties"]["task_id"]["type"], "INTEGER")

    def test_a_tool_that_is_not_published_is_declared_as_it_is(self) -> None:
        schema = {"type": "OBJECT", "properties": {"task_id": {"type": "INTEGER"}}}
        self.assertIs(references.declared_schema("no_such_tool", schema), schema)

    def test_each_tool_has_one_catalog_entry(self) -> None:
        # `_catalog` keys on the registry name. Two entries for one tool would
        # silently take the rules of whichever came last.
        names = [t.registry_name for t in CATALOG]
        self.assertEqual(len(names), len(set(names)))


class RefsBase(McpBase):
    def setUp(self) -> None:
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _loop(
        self, client: _Scripted, session_id: str | None = None, **settings_overrides: Any
    ) -> tuple[list[dict], Any, list]:
        events: list[dict] = []
        persisted: list[dict] = []
        with (
            override_settings(SEARCH_ENGINE=_se(**settings_overrides)),
            patch.object(controller, "get_model_client", return_value=client),
            patch.object(
                controller, "_persist_step", side_effect=lambda *a, **kw: persisted.append(kw)
            ),
        ):
            pause = controller._drive_loop(
                messages=[AgentMessage(role="user", text="q")],
                ctx=self.ctx,
                emit=events.append,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
                session_id=session_id,
            )
        return events, pause, persisted

    def _of_type(self, events: list[dict], kind: str) -> list[dict]:
        return [e for e in events if e.get("type") == kind]


class ResolveCallTests(RefsBase):
    def test_a_display_id_becomes_the_task_id(self) -> None:
        args = references.resolve_call("fetch_task", {"task_id": "wrd-7"}, self.ctx.team_id)
        self.assertEqual(args, {"task_id": self.task.task_id})

    def test_a_number_costs_no_query(self) -> None:
        with self.assertNumQueries(0):
            args = references.resolve_call("fetch_task", {"task_id": 12}, self.ctx.team_id)
        self.assertEqual(args, {"task_id": 12})

    def test_a_blank_that_means_something_stays_blank(self) -> None:
        args = references.resolve_call(
            "update_task", {"task_id": "WRD-7", "parent_task_id": ""}, self.ctx.team_id
        )
        self.assertEqual(args["parent_task_id"], "")

    def test_a_reference_inside_a_batch_is_resolved(self) -> None:
        args = references.resolve_call(
            "update_tasks_bulk",
            {"updates": [{"task_id": "WRD-7", "priority": "High", "rationale": "r"}]},
            self.ctx.team_id,
        )
        self.assertEqual(args["updates"][0]["task_id"], self.task.task_id)

    def test_the_arguments_passed_in_are_not_changed(self) -> None:
        given = {"task_id": "WRD-7"}
        references.resolve_call("fetch_task", given, self.ctx.team_id)
        self.assertEqual(given, {"task_id": "WRD-7"})

    def test_a_tool_that_is_not_published_passes_through(self) -> None:
        self.assertEqual(
            references.resolve_call("nope", {"task_id": "WRD-7"}, "t"), {"task_id": "WRD-7"}
        )

    def test_a_display_id_from_another_team_names_nothing(self) -> None:
        other_team = TeamMaster.objects.create(team_name="Other team", owner=self.user)
        other_project = ProjectMaster.objects.create(
            team=other_team,
            project_name="Theirs",
            code="THR",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=other_team, project=other_project, attendee=self.user)
        TaskMaster.objects.create(
            team=other_team,
            project=other_project,
            reporter=self.user,
            title="Theirs",
            status="Open",
            project_task_number=1,
        )
        with self.assertRaises(ToolError):
            references.resolve_call("fetch_task", {"task_id": "THR-1"}, self.ctx.team_id)


class LoopTests(RefsBase):
    def test_a_read_by_display_id_runs_with_the_real_id(self) -> None:
        client = _Scripted(_call("fetch_task", task_id="WRD-7"), DONE)
        events, pause, persisted = self._loop(client)
        self.assertIsNone(pause)
        self.assertEqual(self._of_type(events, "tool_call_error"), [])
        self.assertEqual(len(self._of_type(events, "tool_call_result")), 1)
        self.assertEqual(persisted[0]["arguments_json"], {"task_id": self.task.task_id})

    def test_the_model_is_told_back_the_call_as_it_wrote_it(self) -> None:
        client = _Scripted(_call("fetch_task", task_id="WRD-7"), DONE)
        self._loop(client)
        echoed = [m.function_call for m in client.messages if m.function_call is not None]
        self.assertEqual(echoed[0].args, {"task_id": "WRD-7"})

    def test_a_reference_that_names_nothing_is_a_tool_error_the_model_can_fix(self) -> None:
        client = _Scripted(_call("fetch_task", task_id="WRD-999"), DONE)
        ran: list[Any] = []
        fake = SimpleNamespace(
            name="fetch_task",
            run=lambda args, ctx: ran.append(args) or {},
            requires_approval=False,
            description="d",
            parameters_schema={"type": "OBJECT", "properties": {}},
        )
        with patch.dict(controller.REGISTRY, {"fetch_task": fake}):
            events, _pause, _persisted = self._loop(client)
        self.assertEqual(ran, [])
        errors = self._of_type(events, "tool_call_error")
        self.assertEqual(len(errors), 1)
        self.assertIn("WRD-999", errors[0]["error"])
        told = [m for m in client.messages if m.function_response_name == "fetch_task"]
        self.assertIn("WRD-999", told[0].function_response["error"])

    def test_a_write_by_display_id_pauses_with_the_real_id(self) -> None:
        client = _Scripted(_call("update_task", task_id="WRD-7", status="Closed"))
        events, pause, persisted = self._loop(client)
        self.assertIsNotNone(pause)
        pending = self._of_type(events, "tool_call_pending_approval")
        self.assertEqual(len(pending), 1)
        # The card shows what a person recognises, the step keeps the key.
        self.assertEqual(pending[0]["arguments"]["task_id"], "WRD-7")
        self.assertEqual(persisted[0]["arguments_json"]["task_id"], self.task.task_id)

    def test_a_write_to_a_task_that_is_not_there_reaches_no_approval_card(self) -> None:
        client = _Scripted(_call("update_task", task_id="WRD-999", status="Closed"), DONE)
        events, pause, _persisted = self._loop(client)
        self.assertIsNone(pause)
        self.assertEqual(self._of_type(events, "tool_call_pending_approval"), [])
        self.assertEqual(len(self._of_type(events, "tool_call_error")), 1)

    def test_a_bad_reference_does_not_cost_its_neighbour_in_the_batch(self) -> None:
        turn = [
            (None, FunctionCall(name="fetch_task", args={"task_id": "WRD-999"})),
            (None, FunctionCall(name="fetch_task", args={"task_id": "WRD-7"})),
        ]
        events, _pause, _persisted = self._loop(_Scripted(turn, DONE), RAG_PARALLEL_TOOLS=True)
        self.assertEqual(len(self._of_type(events, "tool_call_error")), 1)
        self.assertEqual(len(self._of_type(events, "tool_call_result")), 1)

    def test_the_session_cache_keys_on_the_real_id(self) -> None:
        # Otherwise "WRD-7" and the id of the same task are two cache entries.
        client = _Scripted(_call("fetch_task", task_id="WRD-7"), DONE)
        with (
            patch.object(controller.tool_cache, "enabled", return_value=True),
            patch.object(controller.tool_cache, "get_cached", return_value=None) as get_cached,
        ):
            self._loop(client, session_id="s1")
        self.assertEqual(
            get_cached.call_args.args, ("s1", "fetch_task", {"task_id": self.task.task_id})
        )

    def test_a_parallel_batch_runs_with_real_ids(self) -> None:
        second = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Second",
            status="Open",
            project_task_number=8,
        )
        turn = [
            (None, FunctionCall(name="fetch_task", args={"task_id": "WRD-7"})),
            (None, FunctionCall(name="fetch_task", args={"task_id": "WRD-8"})),
        ]
        seen: list[Any] = []

        def run(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
            seen.append(args["task_id"])
            return {"__summary__": "ok"}

        fake = SimpleNamespace(
            name="fetch_task",
            run=run,
            requires_approval=False,
            description="d",
            parameters_schema={"type": "OBJECT", "properties": {}},
        )
        with patch.dict(controller.REGISTRY, {"fetch_task": fake}):
            events, _pause, _persisted = self._loop(_Scripted(turn, DONE), RAG_PARALLEL_TOOLS=True)
        self.assertEqual(self._of_type(events, "tool_call_error"), [])
        self.assertCountEqual(seen, [self.task.task_id, second.task_id])


class ChipSyntaxTests(SimpleTestCase):
    """Nine copies of 600 characters became one, in the prompt."""

    def _declarations(self) -> str:
        return json.dumps(
            [d.parameters_schema for d in controller._build_tool_declarations()],
            ensure_ascii=False,
        )

    def test_no_declaration_repeats_the_syntax(self) -> None:
        self.assertNotIn(CHIP_SYNTAX_HINT.strip()[:40], self._declarations())

    def test_every_body_argument_still_says_it_takes_chips(self) -> None:
        self.assertEqual(self._declarations().count(CHIP_POINTER.strip()), 10)

    def test_the_prompt_the_model_reads_says_it_once(self) -> None:
        for prompt in (AGENT_SYSTEM_PROMPT, AGENT_SYSTEM_PROMPT_REGISTRY):
            self.assertEqual(prompt.count(CHIP_SYNTAX_HINT.strip()), 1)

    def test_mcp_keeps_the_full_text_because_it_has_no_such_prompt(self) -> None:
        tool = next(t for t in CATALOG if t.name == "update_task")
        self.assertIn(CHIP_SYNTAX_HINT.strip()[:40], json.dumps(input_schema(tool)))
        self.assertNotIn(CHIP_POINTER.strip(), json.dumps(input_schema(tool)))

    def test_the_registry_schema_keeps_the_full_text(self) -> None:
        schema = REGISTRY["add_comment"].parameters_schema
        self.assertIn(CHIP_SYNTAX_HINT.strip()[:40], json.dumps(schema))


class CheatSheetTests(SimpleTestCase):
    def test_the_hand_written_write_list_names_every_write_tool(self) -> None:
        # It had drifted: both to-do writes were never on it, and production
        # serves this list.
        for tool in REGISTRY.values():
            if tool.requires_approval:
                self.assertIn(tool.name, prompts._WRITE_TOOLS_LEGACY_BLOCK)

    def test_every_write_tool_has_guidance_on_when_to_use_it(self) -> None:
        sheet = AGENT_SYSTEM_PROMPT.split("WRITE tools (require user approval", 1)[0]
        for name in ("create_todo_item", "update_todo_item", "delete_todo_item"):
            self.assertIn(name, sheet)
        for name in ("manage_milestone", "delete_milestone"):
            self.assertIn(name, sheet)

    def test_the_sheet_tells_the_model_a_display_id_is_an_id(self) -> None:
        self.assertIn("IS its id", AGENT_SYSTEM_PROMPT)
