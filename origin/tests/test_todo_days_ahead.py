"""MIP-2456: `list_uncompleted_todos` stopped at today, so no tool listed a to-do set for tomorrow.

`days_ahead` is behind `AGENT_TODO_DAYS_AHEAD` (default off).
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.list_uncompleted_todos import LIST_UNCOMPLETED_TODOS
from origin.services.user_time import today_for_user_id
from origin.tests.test_agent_all_flags_on import settings_with
from origin.views.public.mcp import tools as mcp_tools

ON = {"AGENT_TODO_DAYS_AHEAD": True}
OFF = {"AGENT_TODO_DAYS_AHEAD": False}


class DaysAheadTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username="ahead", email="ahead@example.com", password="x"
        )
        self.team = TeamMaster.objects.create(
            team_name="Ahead", team_email="ahead-team@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user)
        self.today = today_for_user_id(self.user.id)
        self.ctx = ToolContext(user_id=str(self.user.id), team_id=str(self.team.team_id))
        for offset, title in ((-1, "yesterday"), (0, "today"), (1, "tomorrow"), (3, "later")):
            self.todo(title, offset)

    def todo(self, title, offset, user=None, team=None):
        group, _ = ToDoGroup.objects.get_or_create(
            user=user or self.user,
            team=team or self.team,
            local_date=self.today + timedelta(days=offset),
        )
        return ToDoItem.objects.create(group=group, title=title)

    def listed(self, flags, **args):
        with settings_with(flags):
            out = LIST_UNCOMPLETED_TODOS.run(args, self.ctx)
        return [i["title"] for i in out["items"]], out["to_date"]

    def day(self, offset):
        return (self.today + timedelta(days=offset)).isoformat()

    def test_the_default_still_stops_at_today(self):
        for flags in (OFF, ON):
            self.assertEqual(self.listed(flags), (["yesterday", "today"], self.day(0)))

    def test_days_ahead_includes_that_many_days_after_today(self):
        self.assertEqual(
            self.listed(ON, days_ahead=1), (["yesterday", "today", "tomorrow"], self.day(1))
        )
        self.assertEqual(self.listed(ON, days_ahead=999)[1], self.day(365))
        for junk in (-3, "soon", None):
            self.assertEqual(self.listed(ON, days_ahead=junk)[1], self.day(0))

    def test_with_the_flag_off_the_argument_is_ignored(self):
        self.assertEqual(self.listed(OFF, days_ahead=5), (["yesterday", "today"], self.day(0)))

    def test_another_member_and_another_team_stay_out(self):
        other = CustomUser.objects.create_user(
            username="other", email="other@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=other)
        elsewhere = TeamMaster.objects.create(
            team_name="Else", team_email="else@example.com", owner=self.user
        )
        self.todo("theirs", 1, user=other)
        self.todo("my other team", 1, team=elsewhere)
        self.assertEqual(self.listed(ON, days_ahead=1)[0], ["yesterday", "today", "tomorrow"])

    def test_both_surfaces_offer_it_only_while_the_flag_is_on(self):
        for flags, offered in ((OFF, False), (ON, True)):
            with settings_with(flags):
                inapp = next(
                    d for d in _build_tool_declarations() if d.name == "list_uncompleted_todos"
                )
                mcp = next(t for t in mcp_tools.CATALOG if t.name == "list_uncompleted_todos")
                schema = mcp_tools.input_schema(mcp)
                text = mcp_tools.description_for(mcp)
            self.assertEqual("days_ahead" in inapp.parameters_schema["properties"], offered)
            self.assertEqual("days_ahead" in inapp.description, offered)
            self.assertEqual("days_ahead" in schema["properties"], offered)
            self.assertEqual("days_ahead" in text, offered)
