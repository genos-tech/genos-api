"""Search and Genos, seen from outside the team.

The plan called this phase "mostly verification", and for a project guest it
was: `_build_filter` already required `team_id` AND an `acl_user_ids` match,
and already withheld the `team:<id>` sentinel that stands for "any member of
this team". Cross-team sharing then introduced a person the existing code had
no name for — someone admitted to one CHAT or one NOTE FOLDER and to nothing
else, who holds no `ProjectMembers` row — and both halves of that code asked
the wrong question about them:

* the entry gates asked "member or guest", which is a `ProjectMembers`
  lookup, so a person with an active share was refused search entirely on the
  team whose chat they had just been invited to;
* `_build_filter` asked "is this a guest" to decide the sentinel, and the
  answer for those people was no — so they would have matched every public
  Team Notes folder in a company they do not work for.

Both now ask about membership directly, which is the property the sentinel
actually stands for. These tests pin the outsider cases in both directions:
admitted where they hold something, and narrowed to it.
"""

from unittest.mock import patch

from origin.search_engine.agent.tools.assign_task import ASSIGN_TASK
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.get_team_members import GET_TEAM_MEMBERS
from origin.search_engine.search import _build_filter, _is_member_cached
from origin.services.external_grants import add_external_participants
from origin.tests.cross_team_fixtures import CrossTeamTestCase

SEARCH = "/api/v2/search/"
AGENT_ASK = "/api/v2/agent/ask/"


class ExternalSearchTestCase(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        _is_member_cached.cache_clear()

    def tearDown(self):
        _is_member_cached.cache_clear()
        super().tearDown()

    def chat_only_participant(self):
        """Admit `b_viewer` to a host channel and NOTHING else.

        The shape that used to fall between the two predicates: a real
        participant in team A's data with no project row anywhere.
        """
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        return channel, grant


class EntryGateTests(ExternalSearchTestCase):
    def _search_as(self, user):
        self.authenticate(user)
        # `search` is stubbed: this is about the GATE, and a real query
        # would need the OpenSearch that exists in the dev stack, not CI.
        with patch("origin.search_engine.views.search", return_value={"results": []}) as stub:
            res = self.client.post(
                SEARCH,
                {"query": "anything", "team_id": str(self.team_a.team_id)},
                format="json",
            )
        return res, stub

    def test_a_chat_only_participant_may_search_the_host_team(self):
        self.chat_only_participant()
        res, stub = self._search_as(self.b_viewer)
        self.assertEqual(res.status_code, 200)
        stub.assert_called_once()

    def test_a_folder_only_participant_may_search_the_host_team(self):
        grant = self.active_folder_grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        res, stub = self._search_as(self.b_viewer)
        self.assertEqual(res.status_code, 200)
        stub.assert_called_once()

    def test_a_colleague_who_holds_nothing_is_refused(self):
        """Their team's grant is not their access."""
        self.chat_only_participant()
        res, _ = self._search_as(self.b_editor)
        self.assertEqual(res.status_code, 404)

    def test_a_stranger_is_refused(self):
        self.chat_only_participant()
        res, _ = self._search_as(self.c_owner)
        self.assertEqual(res.status_code, 404)

    def test_the_agent_gate_agrees_with_the_search_gate(self):
        """One predicate, so the two surfaces cannot drift apart."""
        self.chat_only_participant()
        self.authenticate(self.b_editor)
        res = self.client.post(
            AGENT_ASK,
            {"query": "anything", "team_id": str(self.team_a.team_id)},
            format="json",
        )
        self.assertEqual(res.status_code, 404)


class TeamSentinelTests(ExternalSearchTestCase):
    def _acl_terms(self, user):
        flt = _build_filter(str(self.team_a.team_id), str(user.id), None, None, None)
        for clause in flt:
            if "terms" in clause and "acl_user_ids" in clause["terms"]:
                return clause["terms"]["acl_user_ids"]
        raise AssertionError("no acl_user_ids clause in the filter")

    def test_a_chat_only_participant_does_not_match_the_sentinel(self):
        """The case the old `is_guest` question got wrong.

        They hold no project row, so "is this a guest" said no, and the
        sentinel — which grants every public Team Notes folder — was handed
        to someone outside the company.
        """
        self.chat_only_participant()
        terms = self._acl_terms(self.b_viewer)
        self.assertIn(str(self.b_viewer.id), terms)
        self.assertNotIn(f"team:{self.team_a.team_id}", terms)

    def test_a_project_guest_still_does_not_match_it(self):
        grant = self.active_project_grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        terms = self._acl_terms(self.b_viewer)
        self.assertNotIn(f"team:{self.team_a.team_id}", terms)

    def test_a_host_member_still_matches_it(self):
        terms = self._acl_terms(self.a_viewer)
        self.assertIn(f"team:{self.team_a.team_id}", terms)


class ExternalRosterToolTests(ExternalSearchTestCase):
    """The agent's roster tool: prose is not a smaller leak than JSON."""

    def _members_for(self, user):
        ctx = ToolContext(team_id=str(self.team_a.team_id), user_id=str(user.id))
        return {m["email"] for m in GET_TEAM_MEMBERS.run({}, ctx)["members"]}

    def test_a_chat_participant_sees_only_who_is_in_the_chat(self):
        """Previously the whole roster: their visible set was empty, and the
        narrowing was applied only when it was not."""
        self.chat_only_participant()
        emails = self._members_for(self.b_viewer)
        self.assertIn(self.a_owner.email, emails)
        self.assertNotIn(self.a_viewer.email, emails)
        self.assertNotIn(self.a_editor.email, emails)

    def test_a_folder_participant_sees_only_the_folders_people(self):
        grant = self.active_folder_grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        emails = self._members_for(self.b_viewer)
        self.assertNotIn(self.a_viewer.email, emails)

    def test_a_host_member_still_gets_the_whole_roster(self):
        emails = self._members_for(self.a_viewer)
        self.assertIn(self.a_owner.email, emails)
        self.assertIn(self.a_editor.email, emails)


class ExternalAssignmentTests(CrossTeamTestCase):
    """`assign_task` on a shared project.

    The tool asked for an active `TeamMembers` row, which is the one row an
    external collaborator by definition does not have — so the agent could
    describe the shared work and name its participants, then refuse to give
    any of it to them. The REST path never had that limit.
    """

    def setUp(self):
        super().setUp()
        from origin.models.task.task_models import TaskMaster

        self.grant = self.active_project_grant()
        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)
        self.task = TaskMaster.objects.create(
            team=self.team_a,
            project=self.project,
            title="Shared work",
            reporter=self.a_owner,
        )

    def _assign(self, actor, assignee_id):
        ctx = ToolContext(team_id=str(self.team_a.team_id), user_id=str(actor.id))
        return ASSIGN_TASK.run({"task_id": self.task.task_id, "assignee_id": assignee_id}, ctx)

    def test_a_host_member_can_assign_the_external_participant(self):
        out = self._assign(self.a_owner, str(self.b_viewer.id))
        self.assertEqual(out["assignee_id"], str(self.b_viewer.id))

    def test_the_participant_can_assign_a_host_member(self):
        out = self._assign(self.b_viewer, str(self.a_owner.id))
        self.assertEqual(out["assignee_id"], str(self.a_owner.id))

    def test_a_colleague_who_was_never_admitted_cannot_be_assigned(self):
        """Their team holds the grant; the task's audience is the bound."""
        with self.assertRaises(ToolError):
            self._assign(self.a_owner, str(self.b_editor.id))

    def test_a_stranger_cannot_be_assigned(self):
        with self.assertRaises(ToolError):
            self._assign(self.a_owner, str(self.c_owner.id))
