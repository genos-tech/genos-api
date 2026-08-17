"""Milestone backing-task mirror resolution in search results.

Contract under test (`milestone_mirrors.resolve_milestone_mirrors`): a
milestone is a `MilestoneMaster` row PLUS a backing `TaskMaster` row, and
`task_chunker` indexes the latter as an ordinary `entity_type="task"`, so
a milestone could surface in Spotlight with the "Task" subtitle and the
assignment icon. Given a result set:

  * a mirrored `task` row whose milestone is ALSO present is dropped, and
    hands over its score/ranks;
  * a mirrored `task` row that is the SOLE representation is relabeled in
    place (the comment-match case — dropping it would lose the result);
  * a relabeled row's `entity_id` is the canonical `milestone:<id>`,
    because the frontend routes milestones by parsing exactly that;
  * `task_status` is corrected to the milestone's own status, and never
    introduced on the agent shape (which withholds it);
  * ordinary tasks, and a set with no task rows at all, are untouched;
  * a lookup failure degrades to the input, never raising.

`MilestoneMaster` is patched rather than written: the function's only DB
use is one `values_list`, and the module imports it lazily, so a
`SimpleTestCase` covers the logic without a database.
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from origin.search_engine.milestone_mirrors import resolve_milestone_mirrors

# Where the function's lazy import resolves from.
_TARGET = "origin.models.task.milestone_models.MilestoneMaster"


def _backing(*rows):
    """MilestoneMaster stub whose `values_list` yields `rows`.

    Each row is (task_id, milestone_id, status) — the backing task, the
    milestone it backs, and the milestone's OWN status column.
    """
    m = MagicMock()
    m.objects.filter.return_value.values_list.return_value = list(rows)
    return m


def _task(task_id, *, score=1.0, status="WIP", **extra):
    row = {
        "entity_type": "task",
        "entity_id": f"task:{task_id}",
        "task_id": str(task_id),
        "title": "Beta launch",
        "score": score,
        "keyword_rank": 1,
        "vector_rank": 1,
        "task_status": status,
        "project_id": "7",
    }
    row.update(extra)
    return row


def _milestone(milestone_id, *, task_id=None, score=1.0, status="Open", **extra):
    row = {
        "entity_type": "milestone",
        "entity_id": f"milestone:{milestone_id}",
        "task_id": str(task_id) if task_id is not None else None,
        "title": "Beta launch",
        "score": score,
        "keyword_rank": 2,
        "vector_rank": 2,
        "task_status": status,
        "project_id": "7",
    }
    row.update(extra)
    return row


class BothRepresentationsPresentTests(SimpleTestCase):
    """The milestone survives; its backing task does not."""

    def test_backing_task_is_dropped(self):
        rows = [_milestone(42, task_id=99, score=0.9), _task(99, score=0.5)]
        with patch(_TARGET, _backing((99, 42, "Open"))):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual([r["entity_type"] for r in out], ["milestone"])
        self.assertEqual(out[0]["entity_id"], "milestone:42")

    def test_milestone_inherits_a_higher_mirror_score_and_resorts(self):
        # The mirror outranked both the milestone AND an unrelated task.
        # Without the score transfer, suppressing it would demote the
        # milestone below a result it had beaten.
        other = _task(7, score=0.6)
        rows = [_task(99, score=0.9), other, _milestone(42, task_id=99, score=0.2)]
        with patch(_TARGET, _backing((99, 42, "Open"))):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual([r["entity_type"] for r in out], ["milestone", "task"])
        self.assertEqual(out[0]["score"], 0.9)
        self.assertEqual(out[1], other)

    def test_a_lower_mirror_score_does_not_demote_the_milestone(self):
        rows = [_milestone(42, task_id=99, score=0.9), _task(99, score=0.5)]
        with patch(_TARGET, _backing((99, 42, "Open"))):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual(out[0]["score"], 0.9)

    def test_missing_ranks_are_filled_from_the_mirror(self):
        rows = [
            _milestone(42, task_id=99, keyword_rank=None, vector_rank=3),
            _task(99, keyword_rank=5, vector_rank=8),
        ]
        with patch(_TARGET, _backing((99, 42, "Open"))):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual(out[0]["keyword_rank"], 5)
        # Already had one of its own — not overwritten.
        self.assertEqual(out[0]["vector_rank"], 3)


class SoleRepresentationTests(SimpleTestCase):
    """No milestone row to fall back on, so relabel rather than drop."""

    def test_backing_task_is_relabeled_not_dropped(self):
        # This is the comment-match case: the query hit a comment on the
        # milestone's backing task, so the milestone's own doc never
        # matched. Dropping here would lose the result entirely.
        rows = [_task(99, matched_chunk_types=["task_comment"])]
        with patch(_TARGET, _backing((99, 42, "Open"))):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["entity_type"], "milestone")

    def test_relabeled_entity_id_is_canonical(self):
        # `canonicalSpotlightHref` parses milestone rows with an anchored
        # `^milestone:(\d+)$`; anything else silently downgrades to the
        # backing-task route.
        rows = [_task(99)]
        with patch(_TARGET, _backing((99, 42, "Open"))):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual(out[0]["entity_id"], "milestone:42")

    def test_status_is_corrected_to_the_milestones_own(self):
        # The chip value arrived off the backing TaskMaster; a milestone
        # keeps its status in a separate column, so a row labeled
        # "Milestone" must show the milestone's.
        rows = [_task(99, status="WIP")]
        with patch(_TARGET, _backing((99, 42, "Closed"))):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual(out[0]["task_status"], "Closed")

    def test_blank_milestone_status_becomes_none(self):
        rows = [_task(99, status="WIP")]
        with patch(_TARGET, _backing((99, 42, ""))):
            out = resolve_milestone_mirrors(rows)
        self.assertIsNone(out[0]["task_status"])

    def test_agent_shape_does_not_gain_a_status_field(self):
        # `_group_by_entity` withholds `task_status` when `for_agent` —
        # those rows are serialized into LLM grounding context, so adding
        # a field would change agent behaviour.
        row = _task(99)
        del row["task_status"]
        with patch(_TARGET, _backing((99, 42, "Open"))):
            out = resolve_milestone_mirrors([row])
        self.assertNotIn("task_status", out[0])
        self.assertEqual(out[0]["entity_type"], "milestone")

    def test_task_fields_are_left_in_place(self):
        # The milestone route consumes `project_id`, and the relabeled
        # row's `task_id` stays useful as the frontend's fallback route.
        rows = [_task(99)]
        with patch(_TARGET, _backing((99, 42, "Open"))):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual(out[0]["project_id"], "7")
        self.assertEqual(out[0]["task_id"], "99")


class NonMirrorTests(SimpleTestCase):
    """Everything that is not a backing task passes through."""

    def test_ordinary_tasks_survive(self):
        rows = [_task(1), _task(2)]
        with patch(_TARGET, _backing()):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual([r["entity_type"] for r in out], ["task", "task"])

    def test_only_the_mirrored_task_is_touched(self):
        rows = [_milestone(42, task_id=99), _task(99), _task(7)]
        with patch(_TARGET, _backing((99, 42, "Open"))):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual([r["entity_type"] for r in out], ["milestone", "task"])
        self.assertEqual(out[1]["task_id"], "7")

    def test_no_task_rows_skips_the_query_entirely(self):
        stub = _backing((99, 42, "Open"))
        rows = [_milestone(42, task_id=99), {"entity_type": "chat", "score": 1.0}]
        with patch(_TARGET, stub):
            out = resolve_milestone_mirrors(rows)
        self.assertIs(out, rows)
        stub.objects.filter.assert_not_called()

    def test_soft_deleted_milestone_leaves_its_task_alone(self):
        # The query filters `is_deleted=False`, so a soft-deleted
        # milestone yields no mirror and the live backing task stands on
        # its own.
        rows = [_task(99)]
        with patch(_TARGET, _backing()):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual(out[0]["entity_type"], "task")

    def test_milestone_row_with_a_non_canonical_entity_id_is_not_matched(self):
        # Can't tell WHICH milestone it is, so treat the set as not
        # containing it: the mirror is relabeled rather than dropped,
        # which keeps a representation rather than betting on a bad id.
        rows = [_milestone(42, task_id=99, entity_id="milestone:42:chunk:3"), _task(99)]
        with patch(_TARGET, _backing((99, 42, "Open"))):
            out = resolve_milestone_mirrors(rows)
        self.assertEqual(len(out), 2)
        self.assertEqual([r["entity_type"] for r in out], ["milestone", "milestone"])


class FailureModeTests(SimpleTestCase):
    def test_lookup_failure_returns_the_input_unchanged(self):
        stub = MagicMock()
        stub.objects.filter.side_effect = RuntimeError("db gone")
        rows = [_task(99)]
        with patch(_TARGET, stub):
            out = resolve_milestone_mirrors(rows)
        self.assertIs(out, rows)
        self.assertEqual(out[0]["entity_type"], "task")

    def test_task_row_without_a_task_id_is_ignored(self):
        # A legacy / project-less task row can carry a null `task_id`;
        # there is nothing to match a milestone on, and it must not
        # widen the query to `None`.
        row = _task(99)
        row["task_id"] = None
        stub = _backing((99, 42, "Open"))
        with patch(_TARGET, stub):
            out = resolve_milestone_mirrors([row])
        self.assertEqual(out[0]["entity_type"], "task")
        stub.objects.filter.assert_not_called()
