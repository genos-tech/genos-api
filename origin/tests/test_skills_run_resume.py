"""MIP-2042: a skill run that pauses for an approval keeps its ceiling on the resumed leg.

`/decide/` resumes a run on a request of its own and recomputes the caller's tool gates there. A
skill's `tool_level` is a ceiling on the FIRST leg only unless the pause keeps it, so these tests
run the real ask -> pause -> decide round trip (the harness of `test_agent_resume_system_extra`,
only the model and the write tool faked) and compare what the two legs were given.
"""

from __future__ import annotations

from django.test import override_settings

from origin.models.skills.skill_models import Skill
from origin.search_engine import agent_views
from origin.search_engine.agent.tool_tiers import SINGLE_WRITE_TOOLS, disabled_tools_for_level
from origin.services.skills import service
from origin.tests import test_agent_resume_system_extra as base

SECRET = "STEP: SECRET-SKILL-WORDS"


class SkillResumeBase(base.ResumeBase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(GENOS_SKILLS=True))
        self.skill = service.create(
            self.user,
            self.team.team_id,
            service.Fields(
                title="Act skill",
                slug="act-skill",
                description="d",
                tool_level="act",
                body_markdown=SECRET,
            ),
        )

    def run_skill(self):
        run_id, token = self.ask(skill={"id": str(self.skill.id)})
        return run_id, token

    def resumed_kwargs(self):
        return agent_views.resume_agent.call_args.kwargs


class SkillResumeKeepsCeilingTests(SkillResumeBase):
    def test_the_resumed_leg_hides_the_same_tools_and_has_the_block_again(self):
        run_id, token = self.run_skill()
        self.decide_to_the_end(run_id, token)
        first, resumed = self.legs
        self.assertIn("SKILL RUN", first)
        self.assertIn(SECRET, first)
        self.assertEqual(resumed, first)
        hidden = self.resumed_kwargs()["disabled_tools"]
        self.assertTrue(disabled_tools_for_level("act") <= hidden)
        self.assertIn("create_task_plan", hidden)
        self.assertFalse(SINGLE_WRITE_TOOLS & hidden)  # the approved single write still runs
        self.assertEqual(self.tool.calls, [base.PROPOSAL])

    def test_the_run_row_holds_the_reference_and_never_the_text(self):
        run_id, _token = self.run_skill()
        kept = self.row(run_id).resume_context
        self.assertEqual(
            kept["skill"],
            {
                "id": str(self.skill.id),
                "team_id": str(self.team.team_id),
                "version_no": 0,
                "tool_level": "act",
                "outside": False,
            },
        )
        self.assertNotIn("SECRET-SKILL-WORDS", repr(kept))

    def test_a_skill_deleted_while_the_card_waited_is_not_replayed_but_the_ceiling_holds(self):
        run_id, token = self.run_skill()
        service.delete(self.user, self.skill.id)
        self.assertFalse(Skill.objects.filter(pk=self.skill.pk).exists())
        self.decide_to_the_end(run_id, token)
        _first, resumed = self.legs
        self.assertNotIn("SECRET-SKILL-WORDS", resumed or "")
        self.assertIn("create_task_plan", self.resumed_kwargs()["disabled_tools"])

    def test_a_skill_hidden_from_the_asker_while_the_card_waited_is_not_replayed(self):
        # The asker is not the owner: a teammate's team skill that the owner takes back to private.
        mate = self.other
        theirs = service.create(
            mate,
            self.team.team_id,
            service.Fields(
                title="Mate skill",
                slug="mate-skill",
                description="d",
                tool_level="act",
                body_markdown="STEP: TEAMMATE-WORDS",
                visibility="team",
            ),
        )
        run_id, token = self.ask(skill={"id": str(theirs.id)})
        service.update(mate, theirs.id, service.Fields(visibility="private"))
        self.decide_to_the_end(run_id, token)
        _first, resumed = self.legs
        self.assertNotIn("TEAMMATE-WORDS", resumed or "")
        self.assertIn("create_task_plan", self.resumed_kwargs()["disabled_tools"])


class SkillResumeKeepsTheVersionTests(SkillResumeBase):
    def test_a_version_published_while_the_card_waited_is_not_swapped_in(self):
        """The reader's text must not move under them: the resumed leg runs the version the first leg
        ran, with the ceiling it had (a new `read` version must not leave `act` tools declared under
        a block that says "may only read")."""
        mate = self.other
        theirs = service.create(
            mate,
            self.team.team_id,
            service.Fields(
                title="Mate skill",
                slug="mate-skill",
                description="d",
                tool_level="act",
                body_markdown="STEP: VERSION-ONE-WORDS",
                visibility="team",
            ),
        )
        run_id, token = self.ask(skill={"id": str(theirs.id)})
        service.update(
            mate,
            theirs.id,
            service.Fields(body_markdown="STEP: VERSION-TWO-WORDS", tool_level="read"),
        )
        service.publish(mate, theirs.id)
        self.decide_to_the_end(run_id, token)
        first, resumed = self.legs
        self.assertIn("VERSION-ONE-WORDS", first)
        self.assertIn("VERSION-ONE-WORDS", resumed)
        self.assertNotIn("VERSION-TWO-WORDS", resumed)
        self.assertIn("approves one by one", resumed)  # the kept `act` level, in words


class SkillResumeWithTheContextFlagOffTests(SkillResumeBase):
    """`AGENT_RESUME_KEEPS_BLOCKS` off keeps no context at all, but the ceiling is not context."""

    flags: dict = {}

    def test_the_ceiling_and_the_block_survive_while_nothing_else_is_kept(self):
        run_id, token = self.ask(
            skill={"id": str(self.skill.id)}, thread_context=self.make_thread()
        )
        kept = self.row(run_id).resume_context
        self.assertEqual(set(kept), {"skill"})  # no private summary on the row
        self.decide_to_the_end(run_id, token)
        _first, resumed = self.legs
        self.assertIn(SECRET, resumed)
        self.assertNotIn(base.THREAD_SUMMARY, resumed)  # the old, flag-off behaviour
        self.assertIn("create_task_plan", self.resumed_kwargs()["disabled_tools"])


class PlainRunsAreUnchangedTests(base.ResumeBase):
    def test_a_plain_ask_keeps_no_skill_and_resumes_as_before(self):
        run_id, token = self.ask()
        self.assertIsNone(self.row(run_id).resume_context)
        self.decide_to_the_end(run_id, token)
        self.assertEqual(self.legs, [None, None])
