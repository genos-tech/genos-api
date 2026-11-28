"""MIP-2189: a turn reloaded from history keeps its skill, so a retry runs the skill again.

The saved turn stores the skill's REFERENCE (id, version, slug: never the text) and the history
payload returns it. The retry sends it back; the same version runs again, and a version that is gone
falls back to the current published one with a note for the answer.
"""

from __future__ import annotations

from django.test import override_settings

from origin.models.skills.skill_models import SkillVersion
from origin.search_engine import agent_views
from origin.search_engine.models import AgentRun
from origin.services.skills import run, service
from origin.tests.test_skills_run import AskTests, RunBase


@override_settings(GENOS_SKILLS=True, GENOS_SKILLS_PUBLIC=True)
class RetryFromHistoryTests(RunBase):
    ask = AskTests.ask

    def history(self):
        """The turns of the one session, as the history endpoint builds them."""
        run_row = AgentRun.objects.latest("started_at")
        AgentRun.objects.filter(pk=run_row.pk).update(final_answer_text="An answer.")
        return agent_views._build_turns_payload(run_row.session)

    def make_team_skill(self):
        skill = self.skill(self.user2, "team", body_markdown="STEP: version one")
        return skill

    def publish_v2(self, skill):
        service.update(self.user2, skill.id, service.Fields(body_markdown="STEP: version two"))
        service.publish(self.user2, skill.id)

    def test_the_saved_turn_keeps_the_skill_reference_and_never_the_text(self):
        skill = self.make_team_skill()
        self.ask({"id": str(skill.id)}, query="/report weekly")
        turn = self.history()[0]
        self.assertEqual(turn["skill"], {"id": str(skill.id), "version_no": 1, "slug": "report"})
        self.assertNotIn("STEP", repr(turn["skill"]))

    def test_a_plain_ask_has_no_skill_on_its_turn(self):
        self.ask(None, query="what is due?")
        self.assertIsNone(self.history()[0]["skill"])

    def test_the_history_carries_no_skill_while_the_switch_is_off(self):
        skill = self.make_team_skill()
        self.ask({"id": str(skill.id)})
        with override_settings(GENOS_SKILLS=False):
            self.assertIsNone(self.history()[0]["skill"])

    def test_retry_from_history_runs_the_same_version_again(self):
        skill = self.make_team_skill()
        self.ask({"id": str(skill.id)})
        ref = self.history()[0]["skill"]
        self.publish_v2(skill)  # the author published a newer version since
        resp, got = self.ask(ref, query="/report weekly")
        self.assertEqual(resp.status_code, 200)
        extra = got["kwargs"]["system_extra"]
        self.assertIn("STEP: version one", extra)
        self.assertNotIn("STEP: version two", extra)
        self.assertNotIn("no longer available", extra)
        # The retried turn keeps the same reference, so a second retry still works.
        self.assertEqual(AgentRun.objects.latest("started_at").skill_ref, ref)

    def test_a_version_that_is_gone_runs_the_current_one_with_a_note(self):
        skill = self.make_team_skill()
        self.ask({"id": str(skill.id)})
        ref = self.history()[0]["skill"]
        self.publish_v2(skill)
        service.accept_update(self.user, skill.id)  # the install moves to version 2
        SkillVersion.objects.filter(skill_id=skill.id, version_no=1).delete()
        resp, got = self.ask(ref)
        self.assertEqual(resp.status_code, 200)
        extra = got["kwargs"]["system_extra"]
        self.assertIn("STEP: version two", extra)
        self.assertIn("Version 1 of this skill", extra)
        self.assertIn("no longer available", extra)

    def test_a_skill_deleted_since_is_the_404_sentence_not_a_plain_ask(self):
        skill = self.make_team_skill()
        self.ask({"id": str(skill.id)})
        ref = self.history()[0]["skill"]
        service.delete(self.user2, skill.id)
        resp, got = self.ask(ref)
        self.assertEqual(resp.status_code, 404)
        self.assertIn("error", resp.json())
        self.assertNotIn("kwargs", got)

    def test_the_owner_retry_runs_the_current_draft(self):
        skill = self.skill(self.user, body_markdown="STEP: draft one")
        self.ask({"id": str(skill.id)})
        ref = self.history()[0]["skill"]
        self.assertEqual(ref["version_no"], 0)
        service.update(self.user, skill.id, service.Fields(body_markdown="STEP: draft two"))
        _resp, got = self.ask(ref)
        self.assertIn("STEP: draft two", got["kwargs"]["system_extra"])

    def test_the_version_field_is_ignored_when_it_is_not_a_positive_integer(self):
        skill = self.make_team_skill()
        for bad in ("1", True, -3, 0, None, 1.5):
            with self.subTest(bad=bad):
                got = run.resolve(
                    self.user, self.team.team_id, {"id": str(skill.id), "version_no": bad}
                )
                self.assertEqual((got.version_no, got.note), (1, ""))
