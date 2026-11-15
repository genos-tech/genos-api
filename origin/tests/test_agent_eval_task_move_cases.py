"""The grouped-eval pieces of MIP-1609 (arm `task-move-parity`, case file `t1609-task-move.yaml`, the resume runner's
`destination`, `assignee: gone` and `note_by`).

NO MODEL RUNS. The model under test is the stub of `test_eval_group_resume_runner` (it records the exact prompt it is
sent), so what this proves without spending anything is what has to be right before the paid session:

  * the file: both languages, the positives need the arm, the controls carry the arm's tag and run on arm0 too, and the
    harness accepts every key;
  * the runner (extended additively): the destination project, the person who left the team and the task note are built
    as the case says, and a row without the new keys is what it always was;
  * every case pauses on its write in both states of the flag, the REAL tool then runs on approval, and the prompt checks
    go the intended way: a positive FAILS them with the flag off (the move goes through, nothing is said) and PASSES
    them with it on, a control is the same in both.
"""

from __future__ import annotations

import yaml

from origin.search_engine.agent.evals.group import harness, resume_runner
from origin.search_engine.agent.parity import FOLLOWUPS_FLAG, PARITY_FLAG, TASK_MOVE_FLAG

from .test_eval_group_resume_runner import GROUP_DIR, PROMPT_CHECKS, RunnerBase

ARM = "task-move-parity"
SLUGS = ("refused", "notes-narrowed", "plain-move")
# The flags production runs with that touch this path (the key-free test settings default them off), then the same
# with the new flag on: the two states a paid run compares.
PRODUCTION = {
    "AGENT_RESUME_KEEPS_BLOCKS": True,
    "AGENT_RESUME_KEEPS_TURNS": True,
    PARITY_FLAG: True,
    FOLLOWUPS_FLAG: True,
}
FLAG_ON = {**PRODUCTION, TASK_MOVE_FLAG: True}


def cases() -> list[dict]:
    return yaml.safe_load((GROUP_DIR / "t1609-task-move.yaml").read_text(encoding="utf-8"))


def case(case_id: str) -> dict:
    (found,) = [c for c in cases() if c["id"] == case_id]
    return found


class CaseFileTests(RunnerBase):
    def test_every_case_is_there_in_both_languages_and_nothing_else(self):
        ids = [c["id"] for c in cases()]
        self.assertEqual(
            sorted(ids), sorted(f"t1609.{slug}.{lang}" for slug in SLUGS for lang in ("en", "ja"))
        )

    def test_the_harness_loads_them_positives_need_the_arm_controls_carry_its_tag(self):
        loaded = [c for c in harness.load_cases(include_legacy=False) if "t1609" in c.tags]
        self.assertEqual(len(loaded), len(cases()))
        for c in loaded:
            self.assertEqual(c.kind, "resume", c.id)
            if ".plain-move." in c.id:
                self.assertIsNone(c.requires_arm, c.id)
                self.assertIn(ARM, c.tags, c.id)
            else:
                self.assertEqual(c.requires_arm, ARM, c.id)

    def test_the_arm_runs_every_case_and_arm0_only_the_controls(self):
        loaded = harness.select_cases(harness.load_cases(include_legacy=False))
        plan = harness.build_plan(
            [c for c in loaded if "t1609" in c.tags], harness.load_arms(), arm_names=[ARM]
        )
        on = sorted(i.case.id for i in plan.runnable() if i.arm.name == ARM)
        arm0 = sorted(i.case.id for i in plan.runnable() if i.arm.is_baseline)
        self.assertEqual(on, sorted(c["id"] for c in cases()))
        self.assertEqual(arm0, sorted(c["id"] for c in cases() if ".plain-move." in c["id"]))

    def test_each_language_is_asked_in_its_language(self):
        for c in cases():
            has_japanese = any("぀" <= ch <= "ヿ" or "㐀" <= ch <= "鿿" for ch in c["query"])
            self.assertEqual(has_japanese, c["lang"] == "ja", c["id"])
            self.assertIs(c["fixture"], False, c["id"])

    def test_every_case_moves_one_task_to_the_destination_and_forbids_a_new_proposal(self):
        for c in cases():
            self.assertEqual(c["proposal"]["tool"], "update_task", c["id"])
            self.assertEqual(c["proposal"]["args"]["project_id"], "{destination_id}", c["id"])
            self.assertEqual(c["decision"], "approve", c["id"])
            self.assertIs(c["expect"]["no_pending"], True, c["id"])

    def test_the_refused_cases_are_about_someone_who_left_and_the_narrowed_about_a_note(self):
        for c in cases():
            (row,) = c["tasks"]
            if ".refused." in c["id"]:
                self.assertEqual(row.get("assignee"), "gone", c["id"])
                self.assertNotIn("note_by", row)
            elif ".notes-narrowed." in c["id"]:
                self.assertEqual(row.get("note_by"), "other", c["id"])
                self.assertNotIn("assignee", row)
            else:
                self.assertEqual(set(row), {"key", "title"}, c["id"])


class WorldTests(RunnerBase):
    def test_the_world_builds_the_destination_the_leaver_and_the_note(self):
        from origin.models.common.team_models import TeamMembers
        from origin.models.note.task_note_models import TaskNoteMaster
        from origin.models.project.prj_models import ProjectMembers
        from origin.views.utils.note_role import NOTE_TYPE_TASK, ROLE_OWNER, get_explicit_role

        world = resume_runner.World(
            {
                "destination": {"name": "Archive"},
                "tasks": [
                    {"key": "a", "title": "A", "assignee": "gone"},
                    {"key": "b", "title": "B", "note_by": "other", "reporter": "gone"},
                ],
            }
        )
        try:
            dest = world.destination
            self.assertEqual(dest.project_name, "Archive")
            self.assertEqual(dest.team_id, world.team.team_id)
            self.assertEqual(
                set(ProjectMembers.objects.filter(project=dest).values_list("attendee", flat=True)),
                {world.user.id},
            )
            self.assertEqual(world.fill("{destination_id}"), dest.project_id)
            self.assertEqual(world.tasks["a"].assignee_id, world.gone.id)
            self.assertEqual(world.tasks["b"].reporter_id, world.gone.id)
            # the leaver: a soft-deleted team row, no project row
            self.assertTrue(
                TeamMembers.objects.filter(
                    team=world.team, attendee=world.gone, is_deleted=True
                ).exists()
            )
            self.assertFalse(ProjectMembers.objects.filter(attendee=world.gone).exists())
            note = TaskNoteMaster.objects.get(task=world.tasks["b"])
            self.assertEqual(note.owner_id, world.other.id)
            self.assertEqual(
                get_explicit_role(world.other.id, NOTE_TYPE_TASK, note.note_id), ROLE_OWNER
            )
        finally:
            world.teardown()
        from django.contrib.auth import get_user_model

        self.assertFalse(get_user_model().objects.filter(pk=world.gone.pk).exists())

    def test_a_case_without_the_new_keys_is_what_it_always_was(self):
        world = resume_runner.World({"tasks": [{"key": "plain", "title": "Plain"}]})
        try:
            task = world.tasks["plain"]
            self.assertEqual((task.assignee_id, task.reporter_id), (world.user.id, world.user.id))
            self.assertIsNone(world.destination)
            self.assertIsNone(world.gone)
            self.assertEqual(world.note_ids, [])
            self.assertEqual(world.fill("{destination_id}"), "{destination_id}")
        finally:
            world.teardown()


class EveryCaseTests(RunnerBase):
    def checks(self, result):
        return {
            c["name"]: c
            for c in result["checks"]
            if c["name"] in PROMPT_CHECKS + ("paused_on_the_write",)
        }

    def test_every_case_pauses_and_its_prompt_checks_go_the_intended_way(self):
        problems: list[str] = []
        for c in cases():
            off = self.run_case(c, PRODUCTION)
            on = self.run_case(c, FLAG_ON)
            for label, result in (("flag off", off), ("flag on", on)):
                if result.get("error"):
                    problems.append(f"{c['id']} [{label}]: crashed: {result['error']}")
            if any(r.get("error") for r in (off, on)):
                continue
            c_off, c_on = self.checks(off), self.checks(on)
            if not (c_off["paused_on_the_write"]["ok"] and c_on["paused_on_the_write"]["ok"]):
                problems.append(f"{c['id']}: the first leg did not pause")
                continue
            names = [n for n in c_on if n in PROMPT_CHECKS]
            if not names:
                problems.append(f"{c['id']}: no prompt check")
                continue
            if c["polarity"] == "pos":
                if all(c_off[n]["ok"] for n in names):
                    problems.append(f"{c['id']}: passes its prompt checks with the flag off")
                failed = [(n, c_on[n]["detail"]) for n in names if not c_on[n]["ok"]]
                if failed:
                    problems.append(
                        f"{c['id']}: fails its prompt checks with the flag on: {failed}"
                    )
            else:
                for n in names:
                    if not (c_off[n]["ok"] and c_on[n]["ok"]):
                        problems.append(
                            f"{c['id']}: control differs or fails ({n}): "
                            f"{c_off[n]['detail']} / {c_on[n]['detail']}"
                        )
        self.maxDiff = None
        self.assertEqual(problems, [])

    def test_the_refused_move_wrote_nothing_and_the_flag_off_move_went_through(self):
        # what the two arms really did, read from the stub's prompt: the tool's own words
        off = self.run_case(case("t1609.refused.en"), PRODUCTION)
        on = self.run_case(case("t1609.refused.en"), FLAG_ON)
        self.assertIn('"changed_fields": ["project"]', off["resumed_prompt"])
        self.assertIn("Do not remove, clear or change the assignee yourself", on["resumed_prompt"])
        self.assertNotIn('"changed_fields"', on["resumed_prompt"])
