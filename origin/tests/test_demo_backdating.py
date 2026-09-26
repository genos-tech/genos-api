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
