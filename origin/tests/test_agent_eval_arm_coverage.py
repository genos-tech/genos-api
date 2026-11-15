"""What each arm of the grouped eval runs, read from the REAL case and arm files (no model).

The paid run of 2026-11-15 found that `--arms parity` ran only the cases that `requires_arm: parity`
(82 runs): the NEGATIVES of the batch (a plain "add a task" must not start sending a status; asking to
LOOK must never become a write) carried the item tag (`t1059`, `t1561`...) and not the arm's, so the
control half of every item ran on arm0 and never with the flag on, unless the arm files were moved aside.

The rule is the harness's own (an item arm runs the cases that carry its tag or `requires_arm` it), so the
fix is in the case files: every case of a file that has a positive for an arm carries the arm's tag too.
This test is what makes the next parity task do the same.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from origin.search_engine.agent.evals.group import harness


def loaded():
    cases = harness.load_cases(include_legacy=False)
    return cases, harness.load_arms(), harness.select_cases(cases)


class ControlsRunWithTheFlagOnTests(SimpleTestCase):
    def test_every_case_of_a_file_with_a_parity_positive_runs_on_the_parity_arm(self):
        cases, arms, selected = loaded()
        plan = harness.build_plan(selected, arms, arm_names=["parity"])
        ran = {i.case.id for i in plan.runnable() if i.arm.name == "parity"}
        files = {c.source for c in selected if c.requires_arm == "parity"}
        self.assertGreater(len(files), 5)  # the batch's files: a vacuous loop would pass
        missing = sorted(c.id for c in selected if c.source in files and c.id not in ran)
        self.assertEqual(
            missing,
            [],
            "a case of a file with `requires_arm: parity` cases runs on arm0 but not with the flag on: "
            "add the tag `parity` to its `tags` (its item tag is not the arm's)",
        )

    def test_the_negatives_are_the_cases_the_parity_arm_gained(self):
        cases, arms, selected = loaded()
        plan = harness.build_plan(selected, arms, arm_names=["parity"])
        on = [i.case for i in plan.runnable() if i.arm.name == "parity"]
        positives = [c for c in on if c.requires_arm == "parity"]
        controls = [c for c in on if not c.requires_arm]
        self.assertTrue(positives and controls)
        # every control ALSO runs on arm0 (that is what makes it a control), none of the positives does
        arm0 = {i.case.id for i in plan.runnable() if i.arm.is_baseline}
        self.assertTrue(all(c.id in arm0 for c in controls))
        self.assertFalse(any(c.id in arm0 for c in positives))
        self.assertEqual(len(on), len(positives) + len(controls))

    def test_a_control_case_is_tagged_with_the_arm_and_with_its_item(self):
        cases, arms, selected = loaded()
        for c in selected:
            if "parity" in c.tags and not c.requires_arm:
                items = [t for t in c.tags if t not in (harness.GROUP_TAG, "parity")]
                self.assertTrue(items, f"{c.id}: tagged `parity` but not with its item")

    def test_the_same_holds_for_every_other_arm_that_has_positives(self):
        # t905 and t1289 already did: their item tag IS the arm's name. Any new arm with a positive and a
        # negative in one file must tag the negative the same way, or it is a control that never runs.
        cases, arms, selected = loaded()
        for name in arms:
            files = {c.source for c in selected if c.requires_arm == name}
            for c in selected:
                if c.source in files and not c.requires_arm:
                    with self.subTest(arm=name, case=c.id):
                        self.assertIn(name, c.tags)


class ArmZeroIsUntouchedTests(SimpleTestCase):
    def test_arm0_runs_every_case_that_does_not_require_an_arm_and_skips_the_rest(self):
        cases, arms, selected = loaded()
        plan = harness.build_plan(selected, arms)
        items = plan.for_arm(harness.ARM0)
        self.assertEqual(
            sorted(i.case.id for i in items if not i.skip),
            sorted(c.id for c in selected if not c.requires_arm),
        )
        self.assertEqual(
            sorted(i.case.id for i in items if i.skip),
            sorted(c.id for c in selected if c.requires_arm),
        )

    def test_all_on_still_runs_the_whole_selection(self):
        cases, arms, selected = loaded()
        plan = harness.build_plan(selected, arms)
        self.assertEqual(
            sorted(i.case.id for i in plan.runnable() if i.arm.name == harness.ALL_ON),
            sorted(c.id for c in selected),
        )

    def test_an_arm_with_no_parity_cases_gained_none(self):
        # the saving rule holds: an item arm runs its own cases, not the selection
        cases, arms, selected = loaded()
        plan = harness.build_plan(selected, arms)
        for arm in plan.arms:
            if arm.is_baseline or arm.name in ("parity", harness.ALL_ON):
                continue
            for item in plan.runnable():
                if item.arm is arm:
                    self.assertTrue(
                        item.case.requires_arm in arm.satisfies or arm.name in item.case.tags,
                        (arm.name, item.case.id),
                    )
