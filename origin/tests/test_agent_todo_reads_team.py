"""The to-do reads stay in the team the call runs in (MIP-907).

A day's group is per user AND team, so someone in two teams has two "todays".
The reads filtered on the user alone: `list_today_todos` took `.first()` of
both and `list_uncompleted_todos` mixed them, and the ids they handed back
were then refused by `update_todo_item` as another team's.
"""

from datetime import timedelta

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.list_today_todos import LIST_TODAY_TODOS
from origin.search_engine.agent.tools.list_uncompleted_todos import LIST_UNCOMPLETED_TODOS
from origin.services.user_time import today_for_user_id

from .test_base import BaseAPITestCase


class TodoReadsTeamTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.other_team = TeamMaster.objects.create(
            team_name="Second team", team_email="second@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)
        self.today = today_for_user_id(str(self.user.id))

    def _ctx(self, team):
        return ToolContext(team_id=str(team.team_id), user_id=str(self.user.id))

    def _todo(self, team, title, *, days_ago=0, user=None):
        group, _ = ToDoGroup.objects.get_or_create(
            team=team,
            user=user or self.user,
            local_date=self.today - timedelta(days=days_ago),
        )
        return ToDoItem.objects.create(group=group, title=title)

    def test_today_returns_only_the_calling_teams_day(self):
        mine = self._todo(self.team, "First team")
        theirs = self._todo(self.other_team, "Second team")

        # Both directions: whichever group `.first()` happened to pick, one
        # of these would have failed.
        for team, expected in ((self.team, mine), (self.other_team, theirs)):
            with self.subTest(team=team.team_name):
                result = LIST_TODAY_TODOS.run({}, self._ctx(team))
                self.assertEqual([i["item_id"] for i in result["items"]], [expected.item_id])
                self.assertEqual(result["group_id"], expected.group_id)

    def test_today_is_empty_in_a_team_with_nothing_today(self):
        self._todo(self.other_team, "Second team")
        result = LIST_TODAY_TODOS.run({}, self._ctx(self.team))
        self.assertEqual(result["items"], [])
        self.assertIsNone(result["group_id"])

    def test_uncompleted_returns_only_the_calling_teams_items(self):
        mine = self._todo(self.team, "First team", days_ago=1)
        theirs = self._todo(self.other_team, "Second team", days_ago=1)

        for team, expected in ((self.team, mine), (self.other_team, theirs)):
            with self.subTest(team=team.team_name):
                result = LIST_UNCOMPLETED_TODOS.run({}, self._ctx(team))
                self.assertEqual([i["item_id"] for i in result["items"]], [expected.item_id])

    def test_the_other_teams_items_do_not_use_up_the_cap(self):
        # The cap is applied in the query, so unfiltered rows would push this
        # team's to-dos off the page and report `truncated` for them.
        for n in range(60):
            self._todo(self.other_team, f"Second team {n}", days_ago=3)
        mine = self._todo(self.team, "First team", days_ago=1)

        result = LIST_UNCOMPLETED_TODOS.run({}, self._ctx(self.team))

        self.assertEqual([i["item_id"] for i in result["items"]], [mine.item_id])
        self.assertFalse(result["truncated"])

    def test_a_group_with_no_team_is_left_out(self):
        # Matches REST, which reads by `team_id`, and `update_todo_item`,
        # which refuses such an item.
        group = ToDoGroup.objects.create(team=None, user=self.user, local_date=self.today)
        ToDoItem.objects.create(group=group, title="Orphan")

        self.assertEqual(LIST_TODAY_TODOS.run({}, self._ctx(self.team))["items"], [])
        self.assertEqual(LIST_UNCOMPLETED_TODOS.run({}, self._ctx(self.team))["items"], [])
