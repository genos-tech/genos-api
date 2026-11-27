"""The official starter skill of a dashboard made by a skill, `dashboard-page` (MIP-2409).

Its text (`scripts/official_inapp/dashboard-page.md`, built into the catalog) restates the data
shape that `skill_dashboard.DIRECTIVE` gives the model on every run. What matters: the two agree
(block types, key names, limits, the example), the skill is in the dashboard picker for a member,
and a dashboard made from it with a stubbed model gives a page.
"""

from __future__ import annotations

import json
import re

from django.test import SimpleTestCase, override_settings

from origin.models.skills.skill_models import TOOL_LEVEL_READ
from origin.search_engine.agent.tools import app_write_tools
from origin.search_engine.agent.tools import base as tool_base
from origin.services import schedules
from origin.services.dashboard_page import STATUSES
from origin.services.schedule_jobs import skill_dashboard
from origin.services.skills import official, text
from origin.tests.test_schedule_dashboard_skill import DashBase
from origin.tests.test_skills_api import LIST
from origin.tests.test_skills_official import official_skills, sync_command

SLUG = "dashboard-page"


def spec() -> official.Spec:
    return next(s for s in official.load_catalog() if s.slug == SLUG)


def shape_section() -> str:
    """The skill's words about the answer, without its example."""
    return spec().body_markdown.split("## The answer")[1].split("```json")[0]


def example() -> str:
    return re.search(r"```json\n(.*?)\n```", spec().body_markdown, re.DOTALL).group(1)


class TextAgreesWithTheDirectiveTests(SimpleTestCase):
    def test_it_is_a_read_only_template_that_makes_dashboards(self):
        got = spec()
        self.assertEqual(got.tags, [text.TEMPLATE_TAG, "general", "dashboard"])
        self.assertEqual(got.tool_level, TOOL_LEVEL_READ)

    def test_it_names_exactly_the_blocks_the_server_draws(self):
        named = re.findall(r'^- "([a-z]+)"[,:]', shape_section().split("Block types.")[1], re.M)
        self.assertEqual(sorted(named), sorted(skill_dashboard.BLOCK_LIMITS))

    def test_every_key_and_value_it_quotes_is_one_the_directive_quotes(self):
        quoted = set(re.findall(r'"([A-Za-z_]+)"', shape_section()))
        known = set(re.findall(r'"([A-Za-z_]+)"', skill_dashboard.DIRECTIVE))
        known |= set(STATUSES)  # the directive lists the statuses as bare words
        self.assertEqual(sorted(quoted - known), [])

    def test_its_limits_are_the_servers(self):
        section = shape_section()
        lines = {
            m.group(1): m.group(0) for m in re.finditer(r'^- "([a-z]+)".*$', section, re.MULTILINE)
        }
        expected = {
            "stats": [skill_dashboard.MAX_STATS],
            "blocks": [skill_dashboard.MAX_BLOCKS],
            "bars": [skill_dashboard.MAX_BARS],
            "trend": [skill_dashboard.MAX_POINTS],
            "meters": [skill_dashboard.MAX_BLOCK_ROWS],
            "workload": [skill_dashboard.MAX_PEOPLE],
            "timeline": [skill_dashboard.MAX_BLOCK_ROWS],
            "attention": [skill_dashboard.MAX_GROUPS, 3],
            "table": [skill_dashboard.MAX_COLUMNS, skill_dashboard.MAX_TABLE_ROWS],
            "list": [skill_dashboard.MAX_BLOCK_ROWS],
        }
        for key, limits in expected.items():
            with self.subTest(key=key):
                found = [int(n) for n in re.findall(r"(?:at most|,) (\d+)", lines[key])]
                self.assertEqual(found[: len(limits)], limits, lines[key])

    def test_the_server_draws_every_part_of_the_example(self):
        wanted = json.loads(example())
        got = skill_dashboard.parse_dashboard(example(), default_title="d")
        self.assertEqual(got["title"], wanted["title"])
        self.assertEqual(got["subtitle"], wanted["subtitle"])
        self.assertEqual(got["stats"], wanted["stats"])
        self.assertEqual(got["note"], wanted["note"])
        self.assertEqual(
            [(b["type"], b["heading"]) for b in got["blocks"]],
            [(b["type"], b["heading"]) for b in wanted["blocks"]],
        )
        status, trend, attention, timeline = got["blocks"]
        self.assertEqual(status["counts"], wanted["blocks"][0]["counts"])
        self.assertEqual(trend["points"], wanted["blocks"][1]["points"])
        self.assertEqual(attention["groups"][0]["rows"], wanted["blocks"][2]["groups"][0]["rows"])
        self.assertEqual(timeline["rows"], wanted["blocks"][3]["rows"])

    def test_the_tools_it_names_are_reads_the_run_has(self):
        named = set(re.findall(r"`([a-z]+(?:_[a-z0-9]+)+)`", spec().body_markdown))
        tools = named & set(tool_base.REGISTRY)
        self.assertIn("list_tasks", tools)
        self.assertEqual(tools & {t.name for t in app_write_tools()}, set())
        params = {
            p
            for t in tool_base.REGISTRY.values()
            for p in (t.parameters_schema or {}).get("properties", {})
        }
        self.assertEqual(named - set(tool_base.REGISTRY) - params, set())


@override_settings(GENOS_SKILLS=True, GENOS_SKILLS_PUBLIC=True)
class DashboardFromTheStarterTests(DashBase):
    def setUp(self):
        super().setUp()
        sync_command("--apply")
        self.starter = official_skills().get(slug=SLUG)

    def test_a_member_finds_it_among_the_skills_that_make_dashboards(self):
        self.as_user(self.user)
        res = self.client.get(f"{LIST}?team_id={self.team_id}&tab=runnable")
        self.assertEqual(res.status_code, 200, res.content)
        row = next(r for r in res.json()["skills"] if r["slug"] == SLUG)
        self.assertIn("dashboard", row["tags"])
        self.assertIn("template", row["tags"])

    def test_a_dashboard_made_from_it_gives_a_page(self):
        self.agent.answer = example()
        sched = self.dash(self.starter)
        self.assertEqual(schedules._execute(sched)[0], "ok")
        self.assertEqual(self.notes().get().title, "My tasks")  # the answer's title
        told = self.system_extra()
        self.assertIn("# Dashboard page", told)
        self.assertIn(skill_dashboard.DIRECTIVE, told)
