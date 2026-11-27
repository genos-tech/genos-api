"""The two official catch-up skills, `unread-summary` and `comments-waiting-on-me` (MIP-2485).

What matters: each skill's text names only tools and arguments a read-only run has, both are
templates and neither makes dashboards, and a run of each through the real agent loop (the model
scripted) is offered the tool it names, runs it on the member's own data, and answers.
"""

from __future__ import annotations

import re
import threading
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.models.chat.unified_models import Activity
from origin.models.skills.skill_models import TOOL_LEVEL_READ
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.llm.types import FunctionCall
from origin.search_engine.agent.tools import base as tool_base
from origin.search_engine.agent.tools.list_my_unread import LIST_MY_UNREAD
from origin.search_engine.agent.tools.list_my_waiting_comments import (
    LIST_MY_WAITING_COMMENTS_INAPP,
)
from origin.services.skills import official, text
from origin.services.task_comment_fanout import fan_out_task_comment
from origin.tests.test_skills_api import LIST
from origin.tests.test_skills_official import official_skills, sync_command
from origin.tests.test_unread_digest import UnreadBase

UNREAD = "unread-summary"
WAITING = "comments-waiting-on-me"
FLAG = "AGENT_INAPP_WAITING_COMMENTS_TOOL"
TOOLS = {
    UNREAD: {"list_my_unread", "fetch_chat_thread"},
    WAITING: {"list_my_waiting_comments", "fetch_task"},
}


def spec(slug: str) -> official.Spec:
    return next(s for s in official.load_catalog() if s.slug == slug)


def known_tools() -> dict:
    flagged = {name: tool for name, (_, tool) in tool_base.FLAGGED_REGISTRY.items()}
    return {**flagged, **tool_base.REGISTRY}


class TextAgreesWithTheToolsTests(SimpleTestCase):
    def test_both_are_read_only_templates_and_not_dashboard_skills(self):
        for slug in (UNREAD, WAITING):
            got = spec(slug)
            self.assertEqual(got.tags, [text.TEMPLATE_TAG, "general"], slug)
            self.assertEqual(got.tool_level, TOOL_LEVEL_READ, slug)

    def test_each_names_its_tools_and_only_their_arguments(self):
        # Result fields the text reads, checked against the results by the run tests below.
        fields = {"events_omitted", "your_last_comment", "next_offset", "comments_omitted"}
        fields |= {"truncated", "your_ai_app_comment"}
        for slug, expected in TOOLS.items():
            with self.subTest(skill=slug):
                body = spec(slug).body_markdown
                named = set(re.findall(r"`([a-z]+(?:_[a-z0-9]+)*)`", body)) - fields
                tools = known_tools()
                self.assertEqual(named & set(tools), expected)
                arguments = {
                    p for n in expected for p in tools[n].parameters_schema.get("properties", {})
                }
                self.assertEqual(named - expected - arguments, set())
                self.assertFalse(any(tools[n].requires_approval for n in expected))

    def test_the_numbers_it_gives_are_inside_the_tools_limits(self):
        unread = spec(UNREAD).body_markdown
        self.assertIn("limit 100", unread)
        self.assertIn("31 days back at most", unread)
        waiting = spec(WAITING).body_markdown
        self.assertIn("limit 50", waiting)
        self.assertIn("(90 at most)", waiting)
        self.assertIn("last 30 days", waiting)

    def test_the_link_forms_are_the_apps_citation_tokens(self):
        self.assertIn("(chat:<place>:<chat_id>:thread:<thread_id>)", spec(UNREAD).body_markdown)
        self.assertIn("(task:<task_id>)", spec(WAITING).body_markdown)
        for slug in (UNREAD, WAITING):
            self.assertNotIn("http", spec(slug).body_markdown.split("Source:")[0], slug)


class ScriptedModel:
    """Calls the named tool once, then answers."""

    def __init__(self, tool: str, answer: str):
        self.turns = [[(None, FunctionCall(name=tool, args={}))], [(answer, None)]]
        self.declared: list[str] = []
        self.system = ""
        self.results: list = []

    def generate_step(self, messages, tools, system_instruction, **_):
        self.declared = [t.name for t in tools]
        self.system = system_instruction
        self.results = [m.function_response for m in messages if m.role == "tool_response"]
        yield from self.turns.pop(0)


@override_settings(GENOS_SKILLS=True, GENOS_SKILLS_PUBLIC=True)
class RunTests(UnreadBase):
    def setUp(self):
        super().setUp()
        sync_command("--apply")
        task = self.make_task("Ship the page")
        self.task_comment(task, "Can you approve the copy today?")
        self.send(self.channel([self.user, self.user2], title="Design"), "Which logo?")

    def run_skill(self, slug: str, model: ScriptedModel):
        from rest_framework.test import APIClient  # noqa: PLC0415

        client = APIClient()
        client.force_authenticate(user=self.user)
        captured: dict = {}
        events: list[dict] = []

        def fake_stream(worker, **kwargs):
            captured["worker"] = worker
            return iter([b""])

        skill = official_skills().get(slug=slug)
        with (
            mock.patch("origin.search_engine.agent_views._stream_ndjson", side_effect=fake_stream),
            mock.patch(
                "origin.search_engine.agent_views.check_remaining", return_value=(True, 0, None)
            ),
            mock.patch.object(controller, "get_model_client", return_value=model),
        ):
            response = client.post(
                "/api/v2/agent/ask/",
                {
                    "query": f"/{slug}",
                    "team_id": str(self.team.team_id),
                    "skill": {"id": str(skill.id)},
                },
                format="json",
            )
            self.assertEqual(response.status_code, 200, getattr(response, "content", b""))
            captured["worker"](events.append, threading.Event())
        return events

    def result_of(self, model, events) -> dict:
        self.assertEqual([e for e in events if e["type"] == "tool_call_error"], [])
        self.assertTrue([e for e in events if e["type"] == "done"], events)
        self.assertEqual(len(model.results), 1)
        return model.results[0]

    def test_unread_summary_runs_on_the_members_unread_and_marks_nothing(self):
        before = Activity.objects.filter(recipient=self.user, is_read=False).count()
        model = ScriptedModel(LIST_MY_UNREAD.name, "2 unread.")
        result = self.result_of(model, self.run_skill(UNREAD, model))
        self.assertIn("# Summarize unread messages", model.system)
        self.assertTrue(TOOLS[UNREAD] <= set(model.declared))
        self.assertNotIn("create_task", model.declared)
        chat = next(i for i in result["items"] if i["type"] == "chat")
        for key in ("chat_id", "title", "place", "events_omitted"):
            self.assertIn(key, chat)
        self.assertIn("truncated", result)
        self.assertEqual(
            Activity.objects.filter(recipient=self.user, is_read=False).count(), before
        )

    def test_comments_waiting_runs_with_its_tool_and_writes_nothing(self):
        tasks = list(TaskMaster.objects.values_list("task_id", "status", "ts_updated_at"))
        model = ScriptedModel(LIST_MY_WAITING_COMMENTS_INAPP.name, "1 waits.")
        with mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: True}):
            result = self.result_of(model, self.run_skill(WAITING, model))
        self.assertIn("# Comments waiting on me", model.system)
        self.assertTrue(TOOLS[WAITING] <= set(model.declared))
        self.assertNotIn("add_comment", model.declared)
        row = result["tasks"][0]
        for key in ("task_id", "display_id", "comments", "comments_omitted"):
            self.assertIn(key, row)
        self.assertEqual(
            list(TaskMaster.objects.values_list("task_id", "status", "ts_updated_at")), tasks
        )

    def test_both_are_in_a_members_skill_list(self):
        self.client.force_authenticate(user=self.user)
        res = self.client.get(f"{LIST}?team_id={self.team.team_id}&tab=runnable")
        self.assertEqual(res.status_code, 200, res.content)
        rows = {r["slug"]: r for r in res.json()["skills"]}
        for slug in (UNREAD, WAITING):
            self.assertIn("template", rows[slug]["tags"])
            self.assertNotIn("dashboard", rows[slug]["tags"])
