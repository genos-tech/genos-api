"""`get_my_briefing` costs the same number of queries on a busy day as on a quiet one (MIP-2202).

Measured in production on 2026-11-23: one call made 384 queries, 237 of them in `what_you_did`
and 73 in the ranking, for a member with ~350 changed items today and ~260 yesterday (mostly an
AI app working through OAuth). `what_you_did` asked "may the member open this note?" once per note
(two to four queries each), so it grew with the day. These tests seed the same shape, ask at two
sizes and require the same number of queries, which is also what a regression would break.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import ChannelKind
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_delivery_models import TaskDeliveryState
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.label_acl import LabelAccess
from origin.services import briefing, importance, item_activity, item_lines
from origin.tests.briefing_shape import BriefingShape

JST = ZoneInfo("Asia/Tokyo")

SMALL = (24, 18)
LARGE = (240, 180)  # ten times the day, notes included

# What the whole call may cost on this shape. It was 270 here (384 in production).
MAX_WHAT_YOU_DID = 40
MAX_RANKING = 60
MAX_BRIEFING = 165


# A branch taken or not (a looked-at line to name, a second day with notes) may cost a query or
# two; the day's size must not. On `main` these differ by about a hundred on this seed.
SLACK = 5


def queries_of(fn, *args, **kwargs) -> int:
    with CaptureQueriesContext(connection) as captured:
        fn(*args, **kwargs)
    return len(captured)


class QueryCountFlat(BriefingShape):
    def grow(self) -> None:
        """Seed the small day, record the week, then add the rest of the large day."""
        self.seed_day(3, SMALL[0])
        self.seed_day(2, SMALL[1])
        self.record_week()

    def add_rest(self) -> None:
        self.seed_day(3, LARGE[0] - SMALL[0])
        self.seed_day(2, LARGE[1] - SMALL[1])

    def what_you_did(self) -> int:
        names = briefing._Names(self.ctx)
        return queries_of(briefing.what_you_did, self.ctx, self.now, JST, names)

    def ranking(self) -> int:
        _activity, week = item_activity.briefing_data(self.ctx, self.now)
        self.assertTrue(importance.engaged(week), "the ranking must be the engaged one")
        return queries_of(briefing.important_next, self.ctx, self.now, JST, [], None, week)

    def test_what_you_did_does_not_grow_with_the_day(self):
        self.grow()
        small = self.what_you_did()
        self.add_rest()
        large = self.what_you_did()
        self.assertLessEqual(
            abs(large - small), SLACK, "what_you_did must not ask more questions on a busy day"
        )
        self.assertLessEqual(large, MAX_WHAT_YOU_DID)

    def test_ranking_does_not_grow_with_the_day(self):
        self.grow()
        small = self.ranking()
        self.add_rest()
        large = self.ranking()
        self.assertLessEqual(abs(large - small), SLACK, (small, large))
        self.assertLessEqual(large, MAX_RANKING)

    def test_the_whole_briefing_is_flat_and_small(self):
        self.grow()
        small = queries_of(briefing.build_briefing, self.ctx, now=self.now)
        self.add_rest()
        large = queries_of(briefing.build_briefing, self.ctx, now=self.now)
        self.assertLessEqual(abs(large - small), SLACK, (small, large))
        self.assertLessEqual(large, MAX_BRIEFING)

    def test_the_busy_day_is_still_told(self):
        """The cap is on queries, not on what is told: totals still count every item."""
        self.grow()
        self.add_rest()
        did = briefing.what_you_did(self.ctx, self.now, JST, briefing._Names(self.ctx))
        self.assertGreater(did["today"]["totals"]["items"], LARGE[0])
        self.assertEqual(len(did["today"]["items"]), briefing.DID_ITEMS["today"])
        self.assertTrue(did["today"]["items_truncated"])


class NoteRuleEqualsPerNoteRule(BriefingShape):
    """`what_you_did` decides every note at once (`note_scope.readable_note_rows`). It must say
    what `_may_read_note`, the per-note rule it replaced, says for each kind of note."""

    def test_every_kind_of_note_is_told_exactly_when_it_may_be_opened(self):
        outsider_project = self.hidden_project()  # the member is not in it
        notes: dict[str, tuple[int, object]] = {}
        notes["personal_mine"] = (
            1,
            PersonalNoteMaster.objects.create(
                team=self.team, owner=self.user, title="mine", body=[]
            ),
        )
        notes["personal_theirs"] = (
            1,
            PersonalNoteMaster.objects.create(
                team=self.team, owner=self.user2, title="theirs", body=[]
            ),
        )
        shared = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="shared", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team, note_type=1, note_id=shared.note_id, user=self.user, role_id=3
        )
        notes["personal_shared"] = (1, shared)
        own_task = self.make_task("own")
        notes["task_in_my_project"] = (2, self.task_note(own_task, owner=self.user2, title="t1"))
        hidden_task = self.make_task("hidden", project=outsider_project, assignee=self.user2)
        notes["task_in_other_project"] = (
            2,
            self.task_note(hidden_task, owner=self.user2, project=outsider_project, title="t2"),
        )
        granted = self.task_note(
            hidden_task, owner=self.user2, project=outsider_project, title="t3"
        )
        NotePermissionMaster.objects.create(
            team=self.team, note_type=2, note_id=granted.note_id, user=self.user, role_id=3
        )
        notes["task_granted"] = (2, granted)
        # An author who is no longer a member of the task's project reads it as a stranger.
        left = self.task_note(hidden_task, owner=self.user, project=outsider_project, title="t4")
        notes["task_author_left"] = (2, left)
        chat = self.channel([self.user, self.user2])
        notes["chat_member"] = (
            3,
            ChatNoteMaster.objects.create(
                team=self.team,
                owner=self.user2,
                chat_type=int(ChannelKind.GM),
                channel=chat,
                is_thread=False,
                title="c1",
                body=[],
            ),
        )
        elsewhere = self.channel([self.user2])
        notes["chat_not_member"] = (
            3,
            ChatNoteMaster.objects.create(
                team=self.team,
                owner=self.user2,
                chat_type=int(ChannelKind.GM),
                channel=elsewhere,
                is_thread=False,
                title="c2",
                body=[],
            ),
        )

        for k, (kind, note) in enumerate(notes.values()):
            self.version(kind, note, self.user, self.day_minutes(3, 10 + k))

        expected = {
            name: briefing._may_read_note(self.ctx, kind, note)
            for name, (kind, note) in notes.items()
        }
        # The matrix is only a test of the rule if it has both answers.
        self.assertIn(True, expected.values())
        self.assertIn(False, expected.values())

        did = briefing.did_between(
            self.ctx,
            self.at(self.day_minutes(3)),
            self.at(self.day_minutes(3, 12 * 60)),
            max_items=50,
            max_events=3,
            zone=JST,
        )
        told = {
            i["title"].split("\n")[1] for i in did["items"] if i["type"] == "note"
        }  # fenced: "<workspace_content>\n<title>\n</workspace_content>"
        self.assertEqual(
            told,
            {note.title for name, (_kind, note) in notes.items() if expected[name]},
            expected,
        )


class SkippedReadersChangeNothing(BriefingShape):
    """The ranking leaves out the readers whose events it cannot use. What it computes from the
    rest must be exactly what it computed from all of them."""

    def setUp(self):
        super().setUp()
        self.seed_day(3, 40)
        self.seed_day(2, 30)
        self.record_week()

    def test_todays_counts_are_the_same_without_the_marker_reader(self):
        day = self.d(3)
        full = item_activity.compute(self.ctx, [day])
        lean = item_activity.compute(self.ctx, [day], skip=frozenset({"markers"}))
        self.assertTrue(full)
        self.assertEqual(
            {k: {f: v for f, v in row.items() if f != "capped"} for k, row in full.items()},
            {k: {f: v for f, v in row.items() if f != "capped"} for k, row in lean.items()},
        )

    def test_people_counts_are_the_same_without_deliveries_and_markers(self):
        tasks = list(
            TaskMaster.objects.filter(title__startswith="Item")
            .select_related("project")
            .order_by("task_id")[:30]
        )
        # Someone else comments, a PR merges and the member's AI app writes: only the first counts.
        other = ProjectMembers.objects.filter(project=self.project).exclude(attendee=self.user)
        self.assertTrue(other.exists())
        self.comment(tasks[0], self.user2, self.day_minutes(3, 30))
        self.marker(self.user, self.day_minutes(3, 31), "task", tasks[0].task_id, kind="write")
        self.delivery(tasks[0], TaskDeliveryState.MERGED, self.day_minutes(3, 32))
        lean = importance.people_counts(self.ctx, self.now, tasks)
        access = LabelAccess(self.ctx)
        events, _capped = item_lines.read_events(
            self.ctx,
            access,
            {t.task_id: t for t in tasks},
            self.now - timedelta(days=7),
            self.now,
        )
        me = str(self.user.id)
        full = {
            task_id: len(
                {
                    str(e.actor)
                    for e in found
                    if e.actor and str(e.actor) != me and e.source != "app"
                }
            )
            for task_id, found in events.items()
        }
        self.assertEqual(lean, full)
        self.assertEqual(lean[tasks[0].task_id], 1)
