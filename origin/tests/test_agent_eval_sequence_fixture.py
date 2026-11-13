"""The sequence scenario seeded as an eval fixture, in its own teams (MIP-1271).

Also pins the DEFAULT path of `agent_eval_setup` and the v1 fixture module: the v1 corpus is
ranking-sensitive and must stay byte-identical, so the hashes below were recorded on pristine
`origin/main` (sha256 of `fixture.py`, and of the command's stdout with `ensure_fixture` /
`reseed_fixture` mocked) and must never move in this work.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from origin.models.chat.unified_models import Message
from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.evals.group import sequence
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import ToolError

# Recorded on pristine origin/main (346816f2), before any change of this branch.
GOLDEN_FIXTURE_PY_SHA256 = "a0c41db7e166053b2d8c4b92eead6c3bcc49048a394a8b3621813947fbfeb3d5"
GOLDEN_SETUP_STDOUT_SHA256 = {
    False: "6bc554b30d2b2445fb2e64d14c6556ee55adeefcae032ae784697ab4e82c39de",
    True: "3a799f45552ab1d8589e62abde7e1d8906f547f126c547c978e31b7cf643d5d7",
}


def run_setup(*argv, **patches):
    out = io.StringIO()
    summary = {"team_id": "T-1", "user_id": "U-1", "reseeded": "--reseed" in argv}
    base = "origin.search_engine.management.commands.agent_eval_setup"
    with (
        mock.patch(f"{base}.ensure_fixture", return_value=summary),
        mock.patch(f"{base}.reseed_fixture", return_value=summary),
    ):
        call_command("agent_eval_setup", *argv, stdout=out)
    return out.getvalue()


class DefaultPathPinTests(TestCase):
    def test_the_v1_fixture_module_is_byte_identical_to_main(self):
        path = Path(sequence.__file__).parent.parent / "fixture.py"
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), GOLDEN_FIXTURE_PY_SHA256)

    def test_the_default_agent_eval_setup_output_is_byte_identical_to_main(self):
        for reseed in (False, True):
            text = run_setup(*(["--reseed"] if reseed else []))
            self.assertEqual(
                hashlib.sha256(text.encode()).hexdigest(),
                GOLDEN_SETUP_STDOUT_SHA256[reseed],
                f"reseed={reseed}: {text!r}",
            )

    def test_the_default_path_never_seeds_the_scenario(self):
        with mock.patch.object(sequence, "ensure_sequence", side_effect=AssertionError("no")):
            run_setup()

    def test_the_scenario_option_never_touches_the_v1_fixture(self):
        boom = AssertionError("the scenario must not touch the v1 fixture")
        base = "origin.search_engine.management.commands.agent_eval_setup"
        out = io.StringIO()
        fake = {
            "built": [],
            "teams": {},
            "viewers": {},
        }
        with (
            mock.patch(f"{base}.ensure_fixture", side_effect=boom),
            mock.patch(f"{base}.reseed_fixture", side_effect=boom),
            mock.patch.object(sequence, "ensure_sequence", return_value=fake) as ensure,
        ):
            call_command("agent_eval_setup", "--scenario", "sequence", stdout=out)
            call_command("agent_eval_setup", "--scenario", "sequence", "--reseed", stdout=out)
        self.assertEqual(
            [c.kwargs for c in ensure.call_args_list], [{"reseed": False}, {"reseed": True}]
        )


class SequenceFixtureTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # A decoy outside the scenario: nothing below may touch it.
        from origin.models.common.team_models import TeamMembers
        from origin.models.common.user_models import CustomUser

        owner = CustomUser.objects.create_user(
            email="decoy@example.com", username="decoy", password="x"
        )
        cls.decoy_team = TeamMaster.objects.create(
            team_name="Decoy team", team_email="decoy-team@example.com", owner=owner
        )
        TeamMembers.objects.create(team=cls.decoy_team, attendee=owner)
        cls.decoy_user = owner
        cls.first = sequence.ensure_sequence()

    def test_three_teams_and_six_viewers_with_stable_ids(self):
        self.assertEqual(self.first["built"], ["v1", "v2", "ja"])
        self.assertEqual(set(self.first["teams"]), {"v1", "v2", "ja"})
        self.assertEqual(set(self.first["viewers"]), {"v1", "b", "c", "v2", "ja", "ja-b"})
        self.assertEqual(
            self.first["viewers"]["v2"]["user_id"], "00000000-0000-4000-8000-0000005e0201"
        )
        self.assertEqual(self.first["teams"]["ja"], "00000000-0000-4000-8000-0000005eaa03")
        # not the v1 eval team: a fixture user has a different id range
        self.assertNotIn("00000000-0000-4000-8000-00000000ee01", json.dumps(self.first))

    def test_it_is_idempotent(self):
        before = (TaskMaster.objects.count(), Message.objects.count(), TaskComments.objects.count())
        again = sequence.ensure_sequence()
        self.assertEqual(again["built"], [])
        self.assertEqual(again["viewers"], self.first["viewers"])
        self.assertEqual(
            before,
            (TaskMaster.objects.count(), Message.objects.count(), TaskComments.objects.count()),
        )

    def test_each_team_has_the_story_and_one_task(self):
        for label in ("v1", "v2", "ja"):
            team_id = sequence.team_id(label)
            tasks = TaskMaster.objects.filter(team_id=team_id)
            self.assertEqual(tasks.count(), 1, label)
            self.assertEqual(tasks.first().status, "Closed", label)
            self.assertEqual(TaskComments.objects.filter(task__in=tasks).count(), 3, label)

    def test_v2_client_is_on_no_project_and_v1_client_is(self):
        v1, v2 = sequence.viewer_info("v1"), sequence.viewer_info("v2")
        self.assertTrue(ProjectMembers.objects.filter(attendee_id=v1["user_id"]).exists())
        self.assertFalse(ProjectMembers.objects.filter(attendee_id=v2["user_id"]).exists())

    def test_the_story_is_pinned_to_its_day_not_to_now(self):
        first = (
            Message.objects.filter(channel__team_id=sequence.team_id("v1"))
            .order_by("ts_sent_at")
            .first()
        )
        local = first.ts_sent_at.astimezone(sequence._jst())
        self.assertEqual((local.date(), local.hour, local.minute), (sequence.SCENARIO_DAY, 9, 0))

    def test_japanese_team_carries_japanese_text(self):
        comments = TaskComments.objects.filter(task__team_id=sequence.team_id("ja"))
        self.assertTrue(
            any("権限" in json.dumps(c.comment_body, ensure_ascii=False) for c in comments)
        )

    def test_it_runs_with_postgres_only_no_search_or_model_call(self):
        # The build above ran with no OpenSearch client and no model adapter reachable.
        from origin.search_engine import llm

        with mock.patch.object(llm, "_build_adapter", side_effect=AssertionError("no model")):
            sequence.ensure_sequence()

    def test_viewer_info_names_the_task_and_refuses_unknown_viewers(self):
        info = sequence.viewer_info("b")
        self.assertTrue(info["display_id"].startswith("CLP-"))
        self.assertEqual(info["team_id"], str(sequence.team_id("v1")))
        with self.assertRaises(KeyError):
            sequence.viewer_info("nobody")

    def test_reseed_rebuilds_only_the_scenario_teams(self):
        again = sequence.ensure_sequence(reseed=True)
        self.assertEqual(again["built"], ["v1", "v2", "ja"])
        self.assertTrue(TeamMaster.objects.filter(team_id=self.decoy_team.team_id).exists())
        self.assertTrue(type(self.decoy_user).objects.filter(id=self.decoy_user.id).exists())
        self.assertEqual(TaskMaster.objects.filter(team_id=sequence.team_id("v1")).count(), 1)

    # -- the ground truth the t1271 cases rely on, checked against the real in-app tools -------- #

    def run_tool(self, name, viewer, **args):
        info = sequence.viewer_info(viewer)
        return REGISTRY[name].run(
            args, ToolContext(team_id=info["team_id"], user_id=info["user_id"])
        )

    def test_a_project_member_reads_the_task_discussion_through_the_in_app_tool(self):
        info = sequence.viewer_info("v1")
        result = self.run_tool("fetch_task", "v1", task_id=info["task_id"])
        text = json.dumps(result, ensure_ascii=False).lower()
        self.assertIn("missing permission", text)
        self.assertIn("csv", text)
        self.assertIn("shipped", text)

    def test_the_v2_client_cannot_read_the_task_or_its_comments(self):
        info = sequence.viewer_info("v2")
        with self.assertRaises(ToolError):
            self.run_tool("fetch_task", "v2", task_id=info["task_id"])
        with self.assertRaises(ToolError):
            self.run_tool("get_task_history", "v2", task_id=info["task_id"])
