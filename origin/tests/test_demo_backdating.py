"""Regression: the demo workspace must LOOK old and INDEX as new.

The demo's job is to show what Genos looks like after a team has used it
for a quarter, so display timestamps are pushed ~12 weeks into the past.
The whole thing rests on one field split, and both halves fail silently:

  * backdate too little (or not at all) and the visitor sees a workspace
    stamped "8 seconds ago" — an empty product with sample text in it;
  * backdate `ts_updated_at` and the content vanishes from Spotlight.
    Every chunker filters its incremental window on `ts_updated_at`
    (`ts_updated_at__gte=since`) and `_apply_freshness` decays relevance
    by `exp(-age_days/90)` against it, so the post-signin reindex
    (`--since-minutes 5`) would skip the rows and the visitor's first
    question would be answered "I couldn't find anything".

Neither failure raises. Hence these tests.

The third test is the subtle one. `seq`, not `ts_sent_at`, is the
ordering axis for four consumers — the sidebar's preview resolution
(MAX(seq)), the agent's `fetch_chat_thread` / `thread_summary`,
`chat_chunker`'s context window, and forward-only read-cursor advance.
Backdating only writes `ts_sent_at`, so a non-monotonic assignment does
not error; it just makes the AI narrate a conversation in a different
order than the visitor reads it.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Max, Min
from django.test import TestCase
from django.utils import timezone

from origin.models.chat.unified_models import Channel, Message
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.demo_seeder import _backdate, create_demo_environment

User = get_user_model()


class DemoBackdatingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="demo_backdate_user",
            email="demo_backdate@example.com",
            password="x",
            is_demo=True,
        )
        cls.env = create_demo_environment(cls.user, short="backdate")
        cls.team_id = cls.env["team_id"]

    # -- display side ----------------------------------------------------

    def test_chat_history_spans_months_not_seconds(self):
        """The visible history must actually be old — this is the whole
        point of the feature, and "nothing was backdated" is the default
        state it has to be protected against."""
        agg = Message.objects.filter(channel__team=self.team_id).aggregate(
            Min("ts_sent_at"), Max("ts_sent_at")
        )
        age_days = (timezone.now() - agg["ts_sent_at__min"]).days
        self.assertGreaterEqual(
            age_days,
            60,
            f"oldest demo message is only {age_days}d old — the workspace "
            "reads as freshly created, which is the impression backdating exists to fix",
        )
        # …and it must reach forward to the present, or the workspace
        # reads as abandoned instead of as actively used.
        newest_age = (timezone.now() - agg["ts_sent_at__max"]).days
        self.assertLessEqual(newest_age, 7, "newest demo message is over a week old")

    def test_tasks_are_backdated(self):
        oldest = TaskMaster.objects.filter(team=self.team_id).aggregate(Min("ts_created_at"))[
            "ts_created_at__min"
        ]
        self.assertGreaterEqual((timezone.now() - oldest).days, 30)

    def test_every_channel_looks_currently_active(self):
        """The sidebar previews each channel with its MAX(seq) top-level
        message and the client sorts channels by that message's `tsSent`.
        A channel whose newest top-level message is a month old shows up
        as dormant in the chat list no matter how rich its history is —
        which is what happened when thread replies (which take seq AFTER
        every top-level message) absorbed the recent end of the span.
        """
        now = timezone.now()
        for channel in Channel.objects.filter(team=self.team_id):
            latest = (
                Message.objects.filter(channel=channel, is_thread_reply=False)
                .order_by("-seq")
                .first()
            )
            if latest is None:
                continue
            age_days = (now - latest.ts_sent_at).days
            self.assertLessEqual(
                age_days,
                10,
                f"channel {channel.id} previews as {age_days}d stale in the sidebar",
            )

    # -- index side ------------------------------------------------------

    def test_index_timestamps_stay_fresh(self):
        """`ts_updated_at` must remain ~now on every backdated surface, or
        the post-signin incremental reindex skips the rows entirely."""
        cutoff = timezone.now() - timedelta(minutes=30)
        for model, filters in (
            (Message, {"channel__team": self.team_id}),
            (TaskMaster, {"team": self.team_id}),
            (TaskComments, {"task__team": self.team_id}),
        ):
            oldest = model.objects.filter(**filters).aggregate(Min("ts_updated_at"))[
                "ts_updated_at__min"
            ]
            if oldest is None:
                continue
            self.assertGreater(
                oldest,
                cutoff,
                f"{model.__name__}.ts_updated_at was backdated — the search "
                "index windows and scores on this field, so those rows are "
                "now invisible to Spotlight",
            )

    def test_backdate_helper_refuses_index_field(self):
        """The guard that makes the mistake above impossible to make."""
        task = TaskMaster.objects.filter(team=self.team_id).first()
        with self.assertRaises(AssertionError):
            _backdate(TaskMaster, [task.pk], ts_updated_at=timezone.now() - timedelta(days=30))

    # -- the ordering invariant ------------------------------------------

    def test_display_order_agrees_with_seq(self):
        """Within each group a consumer actually orders (the non-thread
        timeline, and each thread's replies), `ts_sent_at` must ascend
        with `seq`. See the module docstring for why nothing errors when
        it doesn't."""
        for channel in Channel.objects.filter(team=self.team_id):
            rows = (
                Message.objects.filter(channel=channel)
                .order_by("seq")
                .values_list("seq", "ts_sent_at", "is_thread_reply", "thread_root_id")
            )
            groups: dict = {}
            for seq, sent, is_reply, root in rows:
                groups.setdefault(("thread", root) if is_reply else ("main", None), []).append(
                    (seq, sent)
                )
            for key, entries in groups.items():
                stamps = [sent for _seq, sent in entries]
                self.assertEqual(
                    stamps,
                    sorted(stamps),
                    f"channel {channel.id} group {key}: ts_sent_at does not "
                    "ascend with seq",
                )

    def test_thread_replies_follow_their_parent(self):
        """A reply dated before the message it answers is the one
        chronological impossibility a reader would actually notice."""
        parents = dict(
            Message.objects.filter(
                channel__team=self.team_id, is_thread_reply=False
            ).values_list("pk", "ts_sent_at")
        )
        replies = Message.objects.filter(
            channel__team=self.team_id, is_thread_reply=True, thread_root__isnull=False
        ).values_list("pk", "thread_root_id", "ts_sent_at")
        checked = 0
        for pk, root_id, sent in replies:
            root_sent = parents.get(root_id)
            if root_sent is None:
                continue
            checked += 1
            self.assertGreaterEqual(
                sent, root_sent, f"reply {pk} predates its parent {root_id}"
            )
        self.assertGreater(checked, 0, "no thread replies seeded — test proves nothing")

    # -- task decision history -------------------------------------------
    #
    # `_replay_task_history` re-saves fields on already-created tasks so
    # `task_signals` emits real `TaskActivity` rows. Its correctness claim
    # is narrow and load-bearing: the audit trail gains history while the
    # tasks' FINAL state is untouched, because `task_chunker` indexes
    # `task_status` / `task_priority` and ~40 eval cases read them.

    def test_task_history_ends_in_the_blueprint_state(self):
        """The replay must leave every task in the state the blueprint
        declares. If a restore step were dropped, tasks would silently
        end in a mid-history state — changing the search index and the
        eval fixture with no error anywhere."""
        from origin.services.demo_seeder import _content

        declared: dict = {}
        for blueprint in _content("en").PROJECT_BLUEPRINTS:
            specs = list(blueprint["tasks"])
            for extra in blueprint.get("extra_milestones", []):
                specs += list(extra["tasks"])
            for spec in specs:
                declared[spec["title"]] = (spec["status"], spec["priority"])

        # `Blocked` is DERIVED from open blocker edges by
        # `sync_blocked_status`, so those tasks legitimately differ from
        # their declared status — the blueprint value is advisory there.
        checked = 0
        for task in TaskMaster.objects.filter(team=self.team_id):
            if task.title not in declared or task.status == "Blocked":
                continue
            want_status, want_priority = declared[task.title]
            checked += 1
            self.assertEqual(
                (task.status, task.priority),
                (want_status, want_priority),
                f"{task.title!r} ended at ({task.status}, {task.priority}) but the "
                f"blueprint declares ({want_status}, {want_priority}) — a history "
                "replay left the task in a mid-history state",
            )
        # 25 tasks, 7 of them Blocked-by-derivation and 3 milestone
        # backing rows whose titles aren't in the task specs → 15 here.
        self.assertGreaterEqual(checked, 15, "too few tasks checked — test proves little")

    def test_tasks_have_real_activity_history(self):
        """Without the replay every task's feed is a single 'created
        this' row, which is the "workspace made 8 seconds ago" tell on
        the one surface that renders a timeline."""
        from origin.models.task.task_activity_models import TaskActivity

        rows = TaskActivity.objects.filter(team=self.team_id)
        changes = rows.exclude(action_type="created")
        self.assertGreaterEqual(
            changes.count(),
            30,
            "too few task CHANGES seeded — every task reads as born in its "
            "final state, so its activity feed has no history to show",
        )
        # Spread across kinds: status-only history reads as a state
        # machine, not as a team making different sorts of decisions.
        kinds = set(changes.values_list("action_type", flat=True))
        for expected in ("status_changed", "priority_changed", "assignee_changed"):
            self.assertIn(expected, kinds, f"no {expected} rows in the demo's history")
        # EVERY row needs an actor, `created` rows included. `_record`
        # resolves it from a thread-local the seeder does not run through,
        # so each writing site has to opt in via `_acting_as`: task and
        # milestone creation, the dependency sweep (whose `sync_blocked_status`
        # flip is a real recorded change), and the replay itself. A single
        # site missed renders "Someone" on that whole class of row, sitting
        # next to named ones — which reads as a bug, not as the system.
        anonymous = rows.filter(actor=None)
        self.assertEqual(
            anonymous.count(),
            0,
            "activity rows with no actor render 'Someone' in the feed: "
            f"{sorted(set(anonymous.values_list('action_type', flat=True)))}",
        )

    def test_activity_rows_spread_over_the_tasks_lifetime(self):
        """All of a task's changes landing on one instant is the exact
        artifact backdating exists to remove — and it was the original
        behaviour here (every row took the task's creation date)."""
        from origin.models.task.task_activity_models import TaskActivity

        multi = 0
        for task in TaskMaster.objects.filter(team=self.team_id):
            stamps = list(
                TaskActivity.objects.filter(task=task)
                .order_by("activity_id")
                .values_list("ts_created_at", flat=True)
            )
            if len(stamps) < 3:
                continue
            multi += 1
            self.assertEqual(
                stamps,
                sorted(stamps),
                f"{task.title!r}: activity rows are not in chronological order, so "
                "the old→new values chain backwards in the feed",
            )
            self.assertGreater(
                (stamps[-1] - stamps[0]).days,
                1,
                f"{task.title!r}: all {len(stamps)} changes happened within a day",
            )
            # A change can never predate the task it is on.
            self.assertGreaterEqual(stamps[0], task.ts_created_at - timedelta(minutes=1))
        self.assertGreater(multi, 5, "too few multi-change tasks — test proves little")

    def test_no_field_history_returns_to_where_it_started(self):
        """A chain that ends on the value it began with renders as
        `High → Low → High`: a seeder looping, not a team deciding.

        This is the defect the whole rewind exists to prevent, and it is
        STRUCTURALLY the default. A task is inserted in its final state,
        so a replay that starts there and restores there cannot help
        ending where it began — every chain round-tripped before
        `_replay_task_history` learned to rewind first. Nothing errors and
        no count changes; only reading a rendered feed shows it.
        """
        from origin.models.task.task_activity_models import TaskActivity

        checked = 0
        for task in TaskMaster.objects.filter(team=self.team_id):
            rows = (
                TaskActivity.objects.filter(task=task)
                .exclude(action_type="created")
                .order_by("activity_id")
                .values_list("field_name", "old_value", "new_value")
            )
            chains: dict = {}
            for field, old, new in rows:
                chains.setdefault(field, []).append((old, new))
            for field, chain in chains.items():
                checked += 1
                self.assertNotEqual(
                    chain[0][0],
                    chain[-1][1],
                    f"{task.title!r}: {field} history starts and ends at "
                    f"{chain[0][0]!r} — {len(chain)} rows that undo themselves",
                )
        self.assertGreater(checked, 20, "too few field chains — test proves little")

    def test_nothing_happens_to_a_task_after_it_closes(self):
        """Closing is the last thing that happens to a task.

        Two separate mechanisms conspire against this and neither raises.
        A single `.save()` of several fields emits one row per field in
        `_TRACKED_TASK_FIELDS` order, which puts `status` before
        `priority` — so a restore that closed the task rendered "closed
        this" and then "changed priority". And the backdating spread was
        status-blind, dating changes on a task closed in July to last
        week. Both read as a task still being triaged after it shipped.
        """
        from origin.models.task.task_activity_models import TaskActivity
        from origin.services.task_blocking import CLOSED_STATUSES

        checked = 0
        for task in TaskMaster.objects.filter(team=self.team_id, status__in=CLOSED_STATUSES):
            rows = list(
                TaskActivity.objects.filter(task=task)
                .order_by("activity_id")
                .values_list("action_type", "field_name", "new_value", "ts_created_at")
            )
            closes = [
                i
                for i, (_a, field, new, _ts) in enumerate(rows)
                if field == "status" and new in CLOSED_STATUSES
            ]
            if not closes:
                continue
            checked += 1
            after = rows[closes[-1] + 1 :]
            self.assertEqual(
                after,
                [],
                f"{task.title!r} closed, then {len(after)} more things happened to "
                f"it: {[r[0] for r in after]}",
            )
            # …and the close itself has to sit back in the task's own
            # lifetime, not days ago on work that shipped months back.
            age_at_close = (timezone.now() - rows[closes[-1]][3]).days
            self.assertGreater(
                age_at_close,
                6,
                f"{task.title!r} is Closed but was closed only {age_at_close}d ago",
            )
        self.assertGreater(checked, 1, "too few closed tasks with history — proves little")

    def test_changes_are_not_all_the_visitors_own_doing(self):
        """A workspace where every change was made by the visitor shows a
        person working alone, not a team deciding together.

        This is the failure the seeder's rule 4 raises on, and it is easy
        to write by accident: the step that supplies a column's BIRTH value
        emits no row (the rewind applies it silently), so naming Alice in
        that step puts her nowhere in the feed. Three tasks shipped that
        way — each naming a bot, each rendering as the demo user — and no
        count noticed, because the rows were all present and all correctly
        attributed to *somebody*.
        """
        from origin.models.task.task_activity_models import TaskActivity

        changes = TaskActivity.objects.filter(team=self.team_id).exclude(action_type="created")
        actors = set(changes.values_list("actor__username", flat=True))
        self.assertGreaterEqual(
            len(actors),
            3,
            f"only {sorted(actors)} appear in the whole workspace's change history",
        )
        # Per task, the assertion has to be the defect itself rather than a
        # budget for it. Counting visitor-only feeds and allowing "a few"
        # cannot work: three tasks are legitimately visitor-only (their
        # history names nobody else), so any threshold loose enough to
        # permit those is also loose enough to permit one real failure —
        # which is exactly what it did when I first wrote it this way.
        #
        # So: a task whose blueprint NAMES a teammate must SHOW one.
        from origin.services.demo_seeder import _TASK_HISTORY, _content

        titles_by_key = {}
        for blueprint in _content("en").PROJECT_BLUEPRINTS:
            specs = list(blueprint["tasks"])
            for extra in blueprint.get("extra_milestones", []):
                specs += list(extra["tasks"])
            for spec in specs:
                if spec.get("key"):
                    titles_by_key[spec["key"]] = spec["title"]

        names_by_key: dict = {}
        for key, actor, _field, _value in _TASK_HISTORY:
            names_by_key.setdefault(key, set()).add(actor)

        checked = 0
        for key, named in names_by_key.items():
            if not named - {"demo"} or key not in titles_by_key:
                continue
            task = TaskMaster.objects.filter(
                team=self.team_id, title=titles_by_key[key]
            ).first()
            if task is None:
                continue
            shown = set(
                TaskActivity.objects.filter(task=task)
                .exclude(action_type="created")
                .values_list("actor__username", flat=True)
            )
            checked += 1
            self.assertTrue(
                shown - {self.user.username},
                f"{titles_by_key[key]!r}: history names {sorted(named - {'demo'})} but "
                f"every row it emitted is the visitor's ({sorted(shown)}) — the steps "
                "naming those teammates supplied birth values, which the rewind "
                "applies silently",
            )
        self.assertGreater(checked, 10, "too few tasks name a teammate — proves little")

    def test_the_board_was_not_all_touched_on_the_same_day(self):
        """Per-task spread is not enough — the BOARD has to look staggered.

        Each task's own changes were already spread across its lifetime,
        and the aggregate still read as a batch job: every open task's
        newest change landed on the same day, because the window stopped a
        fixed 2 days short of `now` for all of them. A visitor scanning the
        task list saw fourteen feeds that all ended "yesterday". Hence the
        recency ladder, and hence this test — the defect is only visible
        across tasks, so no per-task assertion can see it.
        """
        from origin.models.task.task_activity_models import TaskActivity

        newest_ages = []
        for task in TaskMaster.objects.filter(team=self.team_id):
            latest = (
                TaskActivity.objects.filter(task=task)
                .exclude(action_type="created")
                .order_by("-ts_created_at")
                .values_list("ts_created_at", flat=True)
                .first()
            )
            if latest is not None:
                newest_ages.append((timezone.now() - latest).days)
        self.assertGreater(len(newest_ages), 10, "too few tasks with changes — proves little")
        # Counting DISTINCT days is the wrong measure, and I got it wrong
        # once: the closed tasks are spread by a different rule (the
        # first-third window), so they alone supply four or five distinct
        # values while every open task still piles onto "yesterday". The
        # defect is a MODE — one day holding most of the board — so the
        # assertion has to be about the size of the largest cluster.
        from collections import Counter

        biggest_day, count = Counter(newest_ages).most_common(1)[0]
        self.assertLessEqual(
            count,
            len(newest_ages) // 2,
            f"{count} of {len(newest_ages)} tasks were last touched {biggest_day}d ago "
            f"(all ages: {sorted(newest_ages)}) — the board reads as one batch job, "
            "however well each individual feed is spread",
        )

    def test_created_row_agrees_with_the_history_that_follows_it(self):
        """`task_record_changes` stamps the task's status into the CREATED
        row's payload at insert time — which is its FINAL status, because
        that is the state the seeder inserts. After the rewind the first
        visible change starts from the task's birth state instead, so the
        untouched payload would name a status the feed then "changes from"
        something else. `_patch_created_status` realigns it.
        """
        from origin.models.task.task_activity_models import TaskActivity

        checked = 0
        for task in TaskMaster.objects.filter(team=self.team_id):
            rows = list(
                TaskActivity.objects.filter(task=task)
                .order_by("activity_id")
                .values_list("action_type", "field_name", "old_value", "new_value")
            )
            if not rows or rows[0][0] != "created":
                continue
            payload = rows[0][3]
            if not isinstance(payload, dict):
                continue
            first_status = next(
                (old for _a, field, old, _new in rows[1:] if field == "status"), None
            )
            if first_status is None:
                continue
            checked += 1
            self.assertEqual(
                payload.get("status"),
                first_status,
                f"{task.title!r}: created as {payload.get('status')!r} but its first "
                f"status change reads 'from {first_status!r}' — the feed contradicts "
                "its own first row",
            )
        self.assertGreater(checked, 3, "too few tasks with status history — proves little")
