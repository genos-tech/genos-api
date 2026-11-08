"""The roster carries the team's own name, whatever the browser sends (MIP-1221).

A session restored from the refresh cookie has no team name in the browser, so
`getTeamMembers?team_name=` went out empty (or "undefined") and every row of the
roster, and with it the profile card's team line, came back blank.
"""

from origin.tests.cross_team_fixtures import CrossTeamTestCase

TEAM_MEMBERS = "/api/v2/team/getTeamMembers/"


class RosterTeamNameTests(CrossTeamTestCase):
    def _roster(self, **query):
        self.authenticate(self.a_owner)
        res = self.client.get(
            TEAM_MEMBERS,
            {"team_id": str(self.team_a.team_id), "user_id": str(self.a_owner.id), **query},
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res.data["data"]["members"]

    def test_no_team_name_in_the_request_still_names_the_team(self):
        for member in self._roster():
            self.assertEqual(member["teamName"], self.team_a.team_name)

    def test_a_blank_or_undefined_name_is_replaced_by_the_real_one(self):
        for sent in ("", "undefined"):
            for member in self._roster(team_name=sent):
                self.assertEqual(member["teamName"], self.team_a.team_name, sent)

    def test_a_name_the_browser_does_send_costs_no_extra_query(self):
        """The endpoint has a pinned query budget: a normal request must not pay for the lookup."""
        for member in self._roster(team_name=self.team_a.team_name):
            self.assertEqual(member["teamName"], self.team_a.team_name)
