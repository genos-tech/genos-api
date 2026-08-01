"""What a guest can reach through search and the agent.

The REST-side roster scoping is undone if the agent will simply recite
the team for you, so this is the other half of the same requirement. Two
distinct leaks:

  * `get_team_members` — the shortest path from "who is on this team?"
    to a full roster with emails, rendered as prose, which makes it
    harder to notice than a JSON list, not easier.
  * the `team:<team_id>` ACL sentinel in `_build_filter` — it means "any
    member of this team", and a public Team Notes folder grants EDITOR
    to every team member. Matching it would hand a guest the team wiki.

Also covers the two entry points that never checked `team_id` against
the caller at all — a `high` finding from ACL_AUDIT.md, folded in here
because it lives under `search_engine/` and that path triggers billed
agent-evals: one change, one eval run.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.get_team_members import GET_TEAM_MEMBERS
from origin.search_engine.search import _build_filter, _is_guest_cached
from origin.services.member_roles import GUEST
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class GuestAgentBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        _is_guest_cached.cache_clear()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Client Scope", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

        self.bystander = User.objects.create_user(
            username="agentbystander", email="agentbystander@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.bystander)

        self.guest = User.objects.create_user(
            username="agentguest", email="agentguest@agency.example", password="pw"
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.guest, member_role=GUEST
        )

    def tearDown(self):
        _is_guest_cached.cache_clear()
        super().tearDown()


class TestGuestRosterTool(GuestAgentBase):
    def _members_for(self, user):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))
        return {m["email"] for m in GET_TEAM_MEMBERS.run({}, ctx)["members"]}

    def test_guest_asking_the_agent_gets_only_shared_people(self):
        emails = self._members_for(self.guest)
        self.assertIn(self.user.email, emails)
        self.assertNotIn(self.bystander.email, emails)

    def test_a_member_still_gets_the_whole_roster(self):
        emails = self._members_for(self.user2)
        self.assertIn(self.bystander.email, emails)
        self.assertIn(self.user.email, emails)


class TestGuestSearchAcl(GuestAgentBase):
    def _acl_terms(self, user):
        flt = _build_filter(str(self.team.team_id), str(user.id), None, None, None)
        for clause in flt:
            if "terms" in clause and "acl_user_ids" in clause["terms"]:
                return clause["terms"]["acl_user_ids"]
        raise AssertionError("no acl_user_ids clause in the filter")

    def test_guest_does_not_match_the_team_sentinel(self):
        """The sentinel means "any member of this team"; a guest is not
        one, and public Team Notes folders ride on it."""
        terms = self._acl_terms(self.guest)
        self.assertIn(str(self.guest.id), terms)
        self.assertNotIn(f"team:{self.team.team_id}", terms)

    def test_a_member_still_matches_the_team_sentinel(self):
        terms = self._acl_terms(self.user2)
        self.assertIn(f"team:{self.team.team_id}", terms)

    def test_the_guest_verdict_is_cached_per_team_and_user(self):
        self._acl_terms(self.guest)
        self._acl_terms(self.user2)
        info = _is_guest_cached.cache_info()
        self.assertGreaterEqual(info.currsize, 2)


class TestTeamIdIsVerifiedAtTheEntryPoints(GuestAgentBase):
    """`team_id` arrives in the request body. Until now neither endpoint
    checked it against the caller, and `_build_filter` then derived its
    `team:` sentinel from that same untrusted string."""

    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="agentout", email="agentout@example.com", password="pw"
        )
        self.outsider_team = TeamMaster.objects.create(
            team_name="Agent Outsider", team_email="agentout@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.outsider_team, attendee=self.outsider)

    def test_search_refuses_a_foreign_team(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            "/api/v2/search/",
            {"query": "anything", "team_id": str(self.team.team_id)},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_agent_ask_refuses_a_foreign_team(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            "/api/v2/agent/ask/",
            {"query": "anything", "team_id": str(self.team.team_id)},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_a_guest_is_admitted_to_their_own_team(self):
        """A guest belongs to the team through a project, so they must
        pass the entry gate and be narrowed inside it — not refused.

        `search` is stubbed because this test is about the GATE. The two
        tests above return 404 before reaching it, but this one passes
        the gate by design and would otherwise run a real query against
        OpenSearch — which exists in the docker dev stack and not in CI.
        """
        self.authenticate(self.guest)
        with patch("origin.search_engine.views.search", return_value={"results": []}) as stub:
            res = self.client.post(
                "/api/v2/search/",
                {"query": "anything", "team_id": str(self.team.team_id)},
                format="json",
            )
        self.assertEqual(res.status_code, 200)
        # The point of the assertion: the request got PAST the gate.
        stub.assert_called_once()
