"""The gate of the MIP-578 parity batch: `AGENT_PARITY_TOOLS` (MIP-1561).

The flag ships OFF, and with it off everything the model or an MCP client is shown is what it was,
byte for byte. The committed fingerprint golden (`test_agent_fingerprint_golden`) is the proof over
the whole surface; this file pins the mechanism behind it and what the flag publishes when it is on:

  * the registry itself carries none of the parameters, so nothing can leak by reading it;
  * off: the three tools' declarations are the registry's own text, in-app and over MCP, and every
    other tool is the same with the flag on;
  * on: the declarations carry the parameters and one sentence per tool, `create_task`'s
    "Status is always created as 'Open'" is replaced and not contradicted, and the MCP catalog says
    the same;
  * a parameter that arrives while the flag is off (a model that invents one, an MCP client holding
    yesterday's tool list) is ignored by the tool, as an unknown argument always was.
"""

from __future__ import annotations

import copy
import json
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from apis import settings as production
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import tool_flag_params, tool_flag_text
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.parity import PARITY_FLAG, ignore_unless_parity
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.create_task import (
    CREATE_TASK,
    STATUS_SENTENCE,
    STATUS_SENTENCE_ON,
)
from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.views.public.mcp import tools as mcp

from .test_base import BaseAPITestCase
from .test_mcp_endpoint import McpBase

ON = {PARITY_FLAG: True}
OFF = {PARITY_FLAG: False}
THREE = ("create_task", "update_task", "create_task_plan")
# The tools MIP-1562 added to the same flag, pinned in `test_agent_parity_comment_gate`.
COMMENT_TOOLS = ("update_comment", "create_todo_item")
CREATE_NEW = {"status", "reporter_id", "links", "template"}
UPDATE_NEW = {"links", "reporter_id"}
ROW_NEW = {"status", "template"}


def declared(**flags):
    with mock.patch.dict(settings.SEARCH_ENGINE, flags):
        return {d.name: d for d in _build_tool_declarations()}


def canon(declaration):
    return json.dumps(
        [declaration.name, declaration.description, declaration.parameters_schema],
        sort_keys=True,
        ensure_ascii=False,
    )


class FlagDefaultTests(SimpleTestCase):
    def test_it_ships_off(self):
        self.assertIs(production.SEARCH_ENGINE["AGENT_PARITY_TOOLS"], False)

    def test_the_test_suite_runs_with_it_off(self):
        self.assertFalse(settings.SEARCH_ENGINE.get(PARITY_FLAG))


class RegistryIsUntouchedTests(SimpleTestCase):
    def test_the_registry_schemas_carry_none_of_the_parameters(self):
        self.assertFalse(CREATE_NEW & set(REGISTRY["create_task"].parameters_schema["properties"]))
        self.assertFalse(UPDATE_NEW & set(REGISTRY["update_task"].parameters_schema["properties"]))
        rows = REGISTRY["create_task_plan"].parameters_schema["properties"]["tasks"]["items"]
        self.assertFalse(ROW_NEW & set(rows["properties"]))

    def test_laying_the_parameters_over_does_not_mutate_the_registry(self):
        before = {n: copy.deepcopy(REGISTRY[n].parameters_schema) for n in THREE}
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            for name in THREE:
                tool_flag_params.with_flagged_params(name, REGISTRY[name].parameters_schema)
        self.assertEqual({n: REGISTRY[n].parameters_schema for n in THREE}, before)

    def test_no_added_name_is_already_in_the_base_schema(self):
        # An addition that shadowed a real parameter would replace it while the flag is on.
        for addition in tool_flag_params.ADDITIONS:
            props = REGISTRY[addition.tool].parameters_schema["properties"]
            self.assertFalse(set(addition.properties) & set(props), addition.tool)
            for array_name, row_props in addition.nested:
                row = props[array_name]["items"]["properties"]
                self.assertFalse(set(row_props) & set(row), addition.tool)


class OffIsByteIdenticalTests(SimpleTestCase):
    def test_the_three_tools_are_the_registrys_own_text(self):
        off = declared(**OFF)
        for name in THREE:
            with self.subTest(tool=name):
                self.assertEqual(off[name].description, REGISTRY[name].description)

    def test_the_schemas_with_the_flag_off_are_the_registrys_schemas(self):
        # `declared_schema` rewrites ids as strings for the in-app model; only that. The flag adds
        # nothing, so the declared properties are the registry's.
        off = declared(**OFF)
        for name in THREE:
            with self.subTest(tool=name):
                self.assertEqual(
                    set(off[name].parameters_schema["properties"]),
                    set(REGISTRY[name].parameters_schema["properties"]),
                )

    def test_with_the_flag_off_the_helper_hands_back_the_same_object(self):
        for name in THREE:
            schema = REGISTRY[name].parameters_schema
            with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
                self.assertIs(tool_flag_params.with_flagged_params(name, schema), schema)

    def test_every_other_tool_is_the_same_with_the_flag_on(self):
        off, on = declared(**OFF), declared(**ON)
        self.assertEqual(list(off), list(on))
        for name in off:
            if name in THREE + COMMENT_TOOLS:
                continue
            with self.subTest(tool=name):
                self.assertEqual(canon(off[name]), canon(on[name]))

    def test_switching_it_on_and_off_again_leaves_nothing_behind(self):
        first = {n: canon(d) for n, d in declared(**OFF).items()}
        declared(**ON)
        again = {n: canon(d) for n, d in declared(**OFF).items()}
        self.assertEqual(first, again)

    def test_the_mcp_catalog_with_the_flag_off_is_the_registrys(self):
        new = {"create_task": CREATE_NEW, "update_task": UPDATE_NEW, "create_task_plan": set()}
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            for tool in mcp.CATALOG:
                if tool.registry_name in THREE:
                    with self.subTest(tool=tool.name):
                        self.assertEqual(mcp.shared_flag_text(tool), "")
                        props = set(mcp.input_schema(tool)["properties"])
                        base = set(REGISTRY[tool.registry_name].parameters_schema["properties"])
                        self.assertEqual(props - base, set())
                        self.assertFalse(new[tool.registry_name] - base & props)


class OnPublishesTests(SimpleTestCase):
    def setUp(self):
        self.off, self.on = declared(**OFF), declared(**ON)

    def test_create_task_gains_four_parameters_and_keeps_the_rest(self):
        off_props = self.off["create_task"].parameters_schema["properties"]
        on_props = self.on["create_task"].parameters_schema["properties"]

        self.assertEqual(set(on_props) - set(off_props), CREATE_NEW)
        self.assertEqual({k: on_props[k] for k in off_props}, off_props)
        self.assertEqual(
            on_props["status"]["enum"], ["Open", "WIP", "Blocked", "Pending", "Closed"]
        )
        self.assertNotIn("Deleted", on_props["status"]["enum"])
        self.assertEqual(on_props["links"]["type"], "ARRAY")
        self.assertEqual(on_props["links"]["items"]["required"], ["url"])

    def test_update_task_gains_links_and_reporter_id(self):
        off_props = self.off["update_task"].parameters_schema["properties"]
        on_props = self.on["update_task"].parameters_schema["properties"]

        self.assertEqual(set(on_props) - set(off_props), UPDATE_NEW)
        self.assertEqual({k: on_props[k] for k in off_props}, off_props)

    def test_each_plan_task_gains_status_and_template(self):
        off_rows = self.off["create_task_plan"].parameters_schema["properties"]["tasks"]["items"]
        on_rows = self.on["create_task_plan"].parameters_schema["properties"]["tasks"]["items"]

        self.assertEqual(set(on_rows["properties"]) - set(off_rows["properties"]), ROW_NEW)
        self.assertEqual(on_rows["required"], off_rows["required"])
        self.assertNotIn("Deleted", on_rows["properties"]["status"]["enum"])

    def test_one_sentence_per_tool_is_added(self):
        for name in THREE:
            with self.subTest(tool=name):
                base = self.off[name].description.replace(STATUS_SENTENCE, STATUS_SENTENCE_ON)
                self.assertTrue(self.on[name].description.startswith(base))
                self.assertGreater(len(self.on[name].description), len(base))

    def test_create_task_no_longer_says_a_task_is_always_created_open(self):
        # An appended sentence would sit after "Status is always created as 'Open'" and the model
        # would keep obeying the first.
        self.assertIn(STATUS_SENTENCE, CREATE_TASK.description)
        self.assertIn(STATUS_SENTENCE, self.off["create_task"].description)
        self.assertNotIn("always created", self.on["create_task"].description)
        self.assertIn(STATUS_SENTENCE_ON, self.on["create_task"].description)

    def test_every_replacement_names_text_the_tool_really_says(self):
        for name, flag, old, new in tool_flag_text.REPLACEMENTS:
            with self.subTest(tool=name):
                self.assertIn(old, REGISTRY[name].description)
                self.assertNotEqual(old, new)
                self.assertEqual(flag, PARITY_FLAG)

    def test_the_additions_are_terse(self):
        # Every character is billed on every call; the batch is measured as a whole in
        # `test_agent_prefix_budget` and `test_agent_all_flags_on`.
        grown = sum(len(canon(self.on[n])) - len(canon(self.off[n])) for n in THREE)
        self.assertLess(grown, 2200)

    def test_over_mcp_the_catalog_says_the_same(self):
        by_name = {t.registry_name: t for t in mcp.CATALOG}
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            create = by_name["create_task"]
            props = mcp.input_schema(create)["properties"]
            self.assertTrue(CREATE_NEW <= set(props))
            self.assertEqual(props["links"]["type"], "array")
            self.assertEqual(props["status"]["type"], "string")
            self.assertIn("`links`", mcp.description_for(create))
            # MCP words create_task itself and never said "always Open", so the in-app
            # REPLACEMENT has nothing to swap there and nothing contradicts the added sentence.
            self.assertNotIn("always created", mcp.description_for(create))
            self.assertNotIn(STATUS_SENTENCE, mcp.description_for(create))
            update = mcp.input_schema(by_name["update_task"])["properties"]
            self.assertTrue(UPDATE_NEW <= set(update))
            rows = mcp.input_schema(by_name["create_task_plan"])["properties"]["tasks"]["items"]
            self.assertTrue(ROW_NEW <= set(rows["properties"]))
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            self.assertNotIn("links", mcp.input_schema(by_name["create_task"])["properties"])
            self.assertNotIn("`links`", mcp.description_for(by_name["create_task"]))


class IgnoreUnlessParityTests(SimpleTestCase):
    def test_off_it_drops_the_names_and_the_row_keys(self):
        args = {"title": "T", "status": "Closed", "tasks": [{"title": "A", "status": "WIP"}, 3]}
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            out = ignore_unless_parity(args, ("status",), rows=("tasks", ("status", "template")))
        self.assertEqual(out, {"title": "T", "tasks": [{"title": "A"}, 3]})
        self.assertIn("status", args, "the caller's own arguments are never changed")

    def test_off_with_nothing_to_drop_it_is_the_same_object(self):
        args = {"title": "T"}
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            self.assertIs(ignore_unless_parity(args, ("status",), rows=("tasks", ("x",))), args)

    def test_on_it_is_the_same_object(self):
        args = {"title": "T", "status": "Closed"}
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            self.assertIs(ignore_unless_parity(args, ("status",)), args)


class ToolsIgnoreWhenOffTests(BaseAPITestCase):
    """The tool applies the same gate as the declaration."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code="WEB",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def test_create_task_is_what_it_was(self):
        out = CREATE_TASK.run(
            {
                "title": "T",
                "project_id": self.project.project_id,
                "status": "Closed",
                "reporter_id": str(self.user2.id),
                "links": ["https://example.com"],
                "template": "bug",
            },
            self.ctx,
        )

        task = TaskMaster.objects.get(task_id=out["task_id"])
        self.assertEqual(task.status, "Open")
        self.assertEqual(task.reporter_id, self.user.id)
        self.assertIsNone(task.links)
        self.assertEqual(blocks_to_markdown(task.content).strip(), "")
        self.assertNotIn("links", out)
        self.assertNotIn("reporter_id", out)

    def test_update_task_is_what_it_was(self):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="T", status="Open"
        )

        out = UPDATE_TASK.run(
            {
                "task_id": task.task_id,
                "links": ["https://example.com"],
                "reporter_id": str(self.user2.id),
            },
            self.ctx,
        )

        task.refresh_from_db()
        self.assertEqual(out["changed_fields"], [])
        self.assertIsNone(task.links)
        self.assertEqual(task.reporter_id, self.user.id)

    def test_create_task_plan_is_what_it_was(self):
        out = CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "tasks": [{"title": "A", "status": "Closed", "template": "bug"}],
            },
            self.ctx,
        )

        self.assertEqual(TaskMaster.objects.get(task_id=out["tasks"][0]["task_id"]).status, "Open")
        self.assertNotIn("status", out["tasks"][0])


class OverMcpTests(McpBase):
    def setUp(self):
        super().setUp()
        self._key()

    def _create(self, **extra):
        return self._tool_text(
            self._call(
                "create_task",
                {"title": "From MCP", "project_id": self.project.project_id, **extra},
            )
        )

    def test_with_the_flag_off_the_new_arguments_are_refused_and_nothing_is_created(self):
        before = TaskMaster.objects.count()

        text, is_error = self._create(status="Closed")

        self.assertTrue(is_error)
        self.assertIn("does not take `status`", text)
        self.assertEqual(TaskMaster.objects.count(), before)

    def test_with_the_flag_on_a_task_is_made_with_them(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            text, is_error = self._create(
                status="WIP",
                reporter_id=str(self.user2.id),
                links=[{"url": "https://example.com/spec", "title": "Spec"}],
                template="bug",
            )

        self.assertFalse(is_error, text)
        task = TaskMaster.objects.get(title="From MCP")
        self.assertEqual(task.status, "WIP")
        self.assertEqual(task.reporter_id, self.user2.id)
        self.assertEqual(task.links[0]["url"], "https://example.com/spec")
        self.assertTrue(task.content)

    def test_a_link_key_the_schema_does_not_define_is_refused(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            text, is_error = self._create(links=[{"url": "https://example.com", "href": "x"}])

        self.assertTrue(is_error)

    def test_update_task_takes_links_and_reporter_with_the_flag_on(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            text, is_error = self._tool_text(
                self._call(
                    "update_task",
                    {
                        "task_id": "WRD-7",
                        "links": [{"url": "https://example.com/spec"}],
                        "reporter_id": str(self.user2.id),
                    },
                )
            )

        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.reporter_id, self.user2.id)
        self.assertEqual(self.task.links[0]["title"], "https://example.com/spec")

    def test_a_read_only_key_cannot_use_them(self):
        from origin.models.common.api_key_models import SCOPE_READ  # noqa: PLC0415

        self._key(scope=SCOPE_READ)
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            text, is_error = self._create(status="WIP")

        self.assertTrue(is_error)
        self.assertFalse(TaskMaster.objects.filter(title="From MCP").exists())
