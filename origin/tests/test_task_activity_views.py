"""Tests for `task_activity_views._collapse_description_edits`.

The helper is a pure function over `TaskActivity` model instances — it
inspects `action_type`, `actor_id`, `ts_created_at`, and `metadata`,
mutates `metadata` in-memory only, and never touches the DB. We
exercise it directly with unsaved instances so the tests run without
spinning up the test DB just to verify a state machine.

Indexed against the contract documented in
`origin/views/task/task_activity_views.py`:

    Consecutive `description_edited` rows by the same actor whose
    adjacent timestamps fall within DESCRIPTION_EDIT_GROUP_WINDOW
    (15 minutes) are merged into a single row — the latest edit wins
    and gains `metadata.grouped_count` + `metadata.grouped_first_ts`.
"""

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.views.task.task_activity_views import (
    DESCRIPTION_EDIT_GROUP_WINDOW,
    _collapse_description_edits,
)


def _row(
    *,
    action_type: str,
    ts: datetime,
    actor_id: int | None = 1,
    new_value=None,
    activity_id: int = 0,
) -> TaskActivity:
    """Build an unsaved `TaskActivity` with just the fields the helper
    looks at. `activity_id` is included so the assertion errors are
    easier to read when something goes sideways."""
    row = TaskActivity(
        action_type=action_type,
        actor_id=actor_id,
        new_value=new_value,
        metadata={},
    )
    row.ts_created_at = ts
    row.activity_id = activity_id
    return row


class CollapseDescriptionEditsTests(SimpleTestCase):
    """Targeted tests for the description-edit grouping helper."""

    DESC = TaskActivityActionType.DESCRIPTION
    COMMENT = TaskActivityActionType.COMMENT_ADDED

    def setUp(self):
        # Anchor a deterministic "now" so timestamps are easy to read.
        # All rows are constructed newest-first to match the order the
        # view passes in.
        self.now = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)

    def test_within_window_collapses_to_single_row_with_grouped_count(self):
        # Three same-actor description edits, each 5 minutes apart →
        # well inside the 15-minute window; all three should fold into
        # the newest row.
        newest = _row(
            action_type=self.DESC,
            ts=self.now,
            new_value={"v": "newest"},
            activity_id=300,
        )
        middle = _row(
            action_type=self.DESC,
            ts=self.now - timedelta(minutes=5),
            new_value={"v": "middle"},
            activity_id=200,
        )
        oldest = _row(
            action_type=self.DESC,
            ts=self.now - timedelta(minutes=10),
            new_value={"v": "oldest"},
            activity_id=100,
        )

        out = _collapse_description_edits([newest, middle, oldest])

        self.assertEqual(len(out), 1)
        anchor = out[0]
        # The latest edit wins — both the row identity and its payload.
        self.assertIs(anchor, newest)
        self.assertEqual(anchor.new_value, {"v": "newest"})
        self.assertEqual(anchor.metadata["grouped_count"], 3)
        self.assertEqual(
            anchor.metadata["grouped_first_ts"],
            (self.now - timedelta(minutes=10)).isoformat(),
        )

    def test_gap_exceeds_window_keeps_rows_separate(self):
        # Three same-actor description edits with a >15-minute gap
        # between each → the window check fails on every step and we
        # return all three rows verbatim, no metadata mutation.
        gap = DESCRIPTION_EDIT_GROUP_WINDOW + timedelta(minutes=1)
        newest = _row(
            action_type=self.DESC,
            ts=self.now,
            new_value={"v": "newest"},
            activity_id=300,
        )
        middle = _row(
            action_type=self.DESC,
            ts=self.now - gap,
            new_value={"v": "middle"},
            activity_id=200,
        )
        oldest = _row(
            action_type=self.DESC,
            ts=self.now - 2 * gap,
            new_value={"v": "oldest"},
            activity_id=100,
        )

        out = _collapse_description_edits([newest, middle, oldest])

        self.assertEqual([r.activity_id for r in out], [300, 200, 100])
        for r in out:
            # Singletons must not be stamped with the grouping keys —
            # those signal "this row collapsed N>1 originals".
            self.assertNotIn("grouped_count", r.metadata)
            self.assertNotIn("grouped_first_ts", r.metadata)

    def test_intervening_action_breaks_the_run(self):
        # A `comment_added` between two same-actor description edits
        # (well within the time window) breaks the run, so all three
        # rows pass through in original order with no grouping.
        newest_desc = _row(
            action_type=self.DESC,
            ts=self.now,
            new_value={"v": "after-comment"},
            activity_id=300,
        )
        comment = _row(
            action_type=self.COMMENT,
            ts=self.now - timedelta(minutes=2),
            new_value={"text": "looks good"},
            activity_id=250,
        )
        oldest_desc = _row(
            action_type=self.DESC,
            ts=self.now - timedelta(minutes=5),
            new_value={"v": "before-comment"},
            activity_id=200,
        )

        out = _collapse_description_edits([newest_desc, comment, oldest_desc])

        self.assertEqual([r.activity_id for r in out], [300, 250, 200])
        for r in out:
            self.assertNotIn("grouped_count", r.metadata)

    def test_different_actor_breaks_the_run(self):
        # Bonus coverage — same-action, same-window, different actor
        # must NOT merge (collaborative edits stay distinct so the
        # feed shows who did what).
        a = _row(
            action_type=self.DESC,
            ts=self.now,
            actor_id=1,
            activity_id=200,
        )
        b = _row(
            action_type=self.DESC,
            ts=self.now - timedelta(minutes=2),
            actor_id=2,
            activity_id=100,
        )

        out = _collapse_description_edits([a, b])

        self.assertEqual([r.activity_id for r in out], [200, 100])
        self.assertNotIn("grouped_count", a.metadata)
        self.assertNotIn("grouped_count", b.metadata)


class CollapseAroundABodyKeepingRowTests(SimpleTestCase):
    """A description row that kept the previous body (MIP-738) is the one place a
    restore is offered, so it stands alone: it is never folded into a run, and
    the runs on either side of it neither merge with it nor with each other."""

    DESC = TaskActivityActionType.DESCRIPTION

    def setUp(self):
        self.now = datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc)

    def _desc(self, minutes_ago, *, kept=False, activity_id=0):
        row = _row(
            action_type=self.DESC,
            ts=self.now - timedelta(minutes=minutes_ago),
            activity_id=activity_id,
        )
        row.metadata = {"previousBodyKept": True} if kept else {}
        return row

    def test_the_newer_run_keeps_its_own_count_and_the_body_row_stays_whole(self):
        # Newest first: three autosaves, then the rewrite, then two older autosaves.
        rows = [
            self._desc(1, activity_id=6),
            self._desc(2, activity_id=5),
            self._desc(3, activity_id=4),
            self._desc(4, kept=True, activity_id=3),
            self._desc(5, activity_id=2),
            self._desc(6, activity_id=1),
        ]

        out = _collapse_description_edits(rows)

        self.assertEqual([r.activity_id for r in out], [6, 3, 2])
        # The run above the rewrite is closed and counted; it did not swallow it.
        self.assertEqual(out[0].metadata["grouped_count"], 3)
        # The rewrite is untouched: no count, still restorable.
        self.assertNotIn("grouped_count", out[1].metadata)
        self.assertTrue(out[1].metadata["previousBodyKept"])
        # The run below it folds on its own, not into the one above.
        self.assertEqual(out[2].metadata["grouped_count"], 2)

    def test_edits_either_side_of_the_body_row_do_not_fold_into_each_other(self):
        rows = [
            self._desc(1, activity_id=3),
            self._desc(2, kept=True, activity_id=2),
            self._desc(3, activity_id=1),
        ]

        out = _collapse_description_edits(rows)

        self.assertEqual([r.activity_id for r in out], [3, 2, 1])
        for r in out:
            self.assertNotIn("grouped_count", r.metadata)

    def test_two_body_rows_in_a_row_are_both_kept(self):
        rows = [
            self._desc(1, kept=True, activity_id=2),
            self._desc(2, kept=True, activity_id=1),
        ]

        self.assertEqual([r.activity_id for r in _collapse_description_edits(rows)], [2, 1])

    def test_a_body_row_alone_passes_through(self):
        rows = [self._desc(1, kept=True, activity_id=1)]

        out = _collapse_description_edits(rows)

        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].metadata["previousBodyKept"])
