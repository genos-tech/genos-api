"""The v2 demo's launch plan reads like real work on any day of the year (MIP-1745).

Two things were wrong with the plan MIP-1740 seeded:

  * its dates were a fixed story ("launch on March 12", the legal review ends "March 5"), so the due
    dates were the COMING March and the sample read oddly from April to February;
  * its rows were never backdated (the launch plan's project is not in `seeded_projects`, which is
    what `_backdate_history` walks), so every activity row, comment and card read "just now".

What is pinned here:

  * **Dates are offsets from the visitor's today**, in text and in due dates, in both languages,
    and every date of the plan lands on a weekday.
  * **Rows are days old**, per task oldest row first and two days apart, the newest at least two
    whole days old (ja shows exactly one day as "11 日前" until the frontend fix is deployed).
  * **Order is kept**: the activity feed sorts by id, so a higher id is never older; a comment
    carries its own activity row's time; a card is never newer than the replies under it.
  * **A comment does not read "(edited)"**: the comment bubble compares `tsSent` with `tsUpdated`.
  * **The search index is not hurt**: `ts_updated_at` of the tasks stays as it was written.
  * Running the pass again changes nothing, and v1 is untouched.
"""

from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import demo_launch_plan
from origin.services.demo_seeder import create_demo_environment

User = get_user_model()

MONTHS_EN = (
    "January February March April May June July August September October November December"
).split()
TASK_KEYS = ("legal", "announce", "room", "script")
MIN_AGE = timedelta(days=2)


def _user(name):
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", password="x", is_demo=True
    )


def _seed(lang, short, today=None):
    """One v2 workspace; `today` (when given) is the visitor's day as the seeder reads it."""
    visitor = _user(f"lpd_{short}")
    if today is None:
        env = create_demo_environment(visitor, short=short, lang=lang, version="v2")
    else:
        with mock.patch.object(demo_launch_plan, "user_today", return_value=today):
            env = create_demo_environment(visitor, short=short, lang=lang, version="v2")
    copy = demo_launch_plan.COPY[lang]
    project = ProjectMaster.objects.get(team_id=env["team_id"], project_name=copy["project"])
    tasks = {
        key: TaskMaster.objects.get(project=project, title=title)
        for key, (title, _summary) in copy["tasks"].items()
    }
    return visitor, project, tasks


def _day_text(lang, day):
    return f"{day.month}月{day.day}日" if lang == "ja" else f"{MONTHS_EN[day.month - 1]} {day.day}"


class PlanDatesTests(TestCase):
    """`plan_dates(today)` is pure, so any day of the year can be tried."""

    def test_every_date_is_a_weekday_and_after_today(self):
        day = date(2026, 1, 1)
        while day < date(2028, 3, 1):
            dates = demo_launch_plan.plan_dates(day)
            every = [dates["launch"], dates["review"], dates["room"], dates["later"]]
            every += list(dates["due"].values())
            for value in every:
                self.assertLess(value.weekday(), 5, (day, value))
                self.assertGreater(value, day, (day, value))
            self.assertGreaterEqual((dates["launch"] - day).days, 21)
            self.assertLessEqual((dates["launch"] - day).days, 27)
            day += timedelta(days=1)

    def test_the_story_keeps_its_shape_whatever_the_day(self):
        for today in (date(2026, 11, 21), date(2027, 4, 1), date(2027, 12, 30), date(2028, 2, 29)):
            dates = demo_launch_plan.plan_dates(today)
            self.assertEqual(dates["due"]["legal"], dates["review"])
            self.assertEqual(dates["launch"] - dates["review"], timedelta(days=7))
            self.assertEqual(dates["later"] - dates["launch"], timedelta(days=7))
            self.assertEqual(dates["launch"] - dates["room"], timedelta(days=1))
            self.assertLess(dates["due"]["announce"], dates["launch"])
            self.assertLess(dates["due"]["script"], dates["launch"])

    def test_a_date_is_written_by_hand_not_by_the_server_locale(self):
        self.assertEqual(demo_launch_plan.format_day("en", date(2027, 4, 1)), "April 1")
        self.assertEqual(demo_launch_plan.format_day("ja", date(2027, 4, 1)), "4月1日")

    def test_no_text_names_a_fixed_month(self):
        """The text is a template: the month comes from the date, never from the source."""
        for lang in ("en", "ja"):
            copy = demo_launch_plan.COPY[lang]
            days = demo_launch_plan.format_dates(
                lang, demo_launch_plan.plan_dates(date(2027, 4, 1))
            )
            text = repr((copy["milestone"], copy["tasks"], copy["comments"], copy["note_title"]))
            text += repr(copy["note_sections"]("x", days))
            self.assertNotIn("March", text)
            self.assertNotIn("3月", text)


class DemoLaunchPlanRelativeDatesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = date(2027, 4, 1)  # a day that has nothing to do with March
        cls.en = _seed("en", "reden", cls.today)
        cls.ja = _seed("ja", "redja", cls.today)

    def each_language(self):
        for lang, ws in (("en", self.en), ("ja", self.ja)):
            with self.subTest(lang=lang):
                yield lang, ws

    def test_due_dates_are_offsets_from_the_visitors_day(self):
        dates = demo_launch_plan.plan_dates(self.today)
        for _lang, (_visitor, _project, tasks) in self.each_language():
            for key in TASK_KEYS:
                self.assertEqual(tasks[key].due_date, dates["due"][key], key)

    def test_the_milestone_is_due_on_the_launch_day(self):
        dates = demo_launch_plan.plan_dates(self.today)
        for _lang, (_visitor, project, _tasks) in self.each_language():
            backing = TaskMaster.objects.get(project=project, is_milestone=True)
            self.assertEqual(backing.due_date, dates["launch"])

    def test_the_text_carries_the_dates_of_that_day(self):
        dates = demo_launch_plan.plan_dates(self.today)
        for lang, (_visitor, project, tasks) in self.each_language():
            launch = _day_text(lang, dates["launch"])
            review = _day_text(lang, dates["review"])
            room = _day_text(lang, dates["room"])
            note = TaskNoteMaster.objects.get(project=project)
            self.assertIn(launch, note.title)
            self.assertIn(review, repr(note.body))
            comments = " ".join(
                repr(c.comment_body) for c in TaskComments.objects.filter(task__project=project)
            )
            self.assertIn(launch, comments)
            self.assertIn(review, comments)
            self.assertIn(review, repr(tasks["legal"].content))
            self.assertIn(room, repr(tasks["room"].content))
            for text in (note.title, repr(note.body), comments):
                self.assertNotIn("March" if lang == "en" else "3月", text)


class DemoLaunchPlanBackdatingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.before = timezone.now()
        cls.en = _seed("en", "bden")
        cls.ja = _seed("ja", "bdja")

    def each_language(self):
        for lang, ws in (("en", self.en), ("ja", self.ja)):
            with self.subTest(lang=lang):
                yield lang, ws

    def activity(self, task):
        return list(TaskActivity.objects.filter(task=task).order_by("activity_id"))

    def test_no_row_reads_just_now_and_none_is_under_two_whole_days(self):
        now = timezone.now()
        for _lang, (_visitor, project, tasks) in self.each_language():
            for key, task in tasks.items():
                for row in self.activity(task):
                    self.assertGreaterEqual(now - row.ts_created_at, MIN_AGE, (key, row.pk))
                created = TaskMaster.objects.get(pk=task.pk).ts_created_at
                self.assertGreaterEqual(now - created, MIN_AGE)
            for row in TaskActivity.objects.filter(task__project=project):
                self.assertGreaterEqual(now - row.ts_created_at, MIN_AGE, "the milestone's too")
            for comment in TaskComments.objects.filter(task__project=project):
                self.assertGreaterEqual(now - comment.ts_sent_at, MIN_AGE)
            for message in Message.objects.filter(channel__project=project):
                self.assertGreaterEqual(now - message.ts_sent_at, MIN_AGE)

    def test_per_task_oldest_first_and_two_days_apart(self):
        for _lang, (_visitor, _project, tasks) in self.each_language():
            for key, task in tasks.items():
                stamps = [row.ts_created_at for row in self.activity(task)]
                self.assertEqual(stamps, sorted(stamps), key)
                for older, newer in zip(stamps, stamps[1:], strict=False):
                    # whole days, so a row never lands just under a day boundary
                    self.assertEqual(round((newer - older).total_seconds()), 2 * 86400, key)
                created = TaskMaster.objects.get(pk=task.pk).ts_created_at
                self.assertEqual(created, stamps[0], "a task is as old as its created row")

    def test_the_spread_is_days_not_months(self):
        now = timezone.now()
        for _lang, (_visitor, _project, tasks) in self.each_language():
            oldest = min(TaskMaster.objects.get(pk=t.pk).ts_created_at for t in tasks.values())
            self.assertGreaterEqual(now - oldest, timedelta(days=6))
            self.assertLessEqual(now - oldest, timedelta(days=14))

    def test_a_comment_carries_the_time_of_its_own_activity_row(self):
        for _lang, (_visitor, _project, tasks) in self.each_language():
            for key, task in tasks.items():
                rows = [
                    r
                    for r in self.activity(task)
                    if r.action_type == TaskActivityActionType.COMMENT_ADDED
                ]
                comments = list(TaskComments.objects.filter(task=task).order_by("comment_id"))
                self.assertEqual(len(rows), len(comments), key)
                for row, comment in zip(rows, comments, strict=True):
                    self.assertEqual(comment.ts_sent_at, row.ts_created_at, key)

    def test_a_comment_does_not_read_edited(self):
        for _lang, (visitor, _project, tasks) in self.each_language():
            client = APIClient()
            client.credentials(
                HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(visitor).access_token}"
            )
            res = client.get("/api/v2/task/comment/", {"task_id": tasks["announce"].task_id})
            self.assertEqual(res.status_code, 200, res.content)
            self.assertEqual(len(res.data), 2)
            for comment in res.data:
                self.assertEqual(comment["tsSent"], comment["tsUpdated"])

    def test_the_cards_and_replies_keep_seq_order_and_never_predate_their_card(self):
        for _lang, (_visitor, project, tasks) in self.each_language():
            channel = Channel.objects.get(project=project, kind=ChannelKind.PM)
            cards = list(
                Message.objects.filter(channel=channel, is_thread_reply=False).order_by("seq")
            )
            stamps = [m.ts_sent_at for m in cards]
            self.assertEqual(len(cards), 4)
            self.assertEqual(stamps, sorted(stamps))
            by_task = {m.task_id: m for m in cards}
            legal = TaskMaster.objects.get(pk=tasks["legal"].pk)
            self.assertEqual(by_task[legal.task_id].ts_sent_at, legal.ts_created_at)
            replies = Message.objects.filter(channel=channel, is_thread_reply=True)
            self.assertEqual(replies.count(), 5)
            for reply in replies:
                root = Message.objects.get(pk=reply.thread_root_id)
                self.assertGreaterEqual(reply.ts_sent_at, root.ts_sent_at)

    def test_the_milestone_the_project_and_the_channel_predate_the_work(self):
        for _lang, (_visitor, project, tasks) in self.each_language():
            first = min(TaskMaster.objects.get(pk=t.pk).ts_created_at for t in tasks.values())
            backing = TaskMaster.objects.get(project=project, is_milestone=True)
            self.assertLess(backing.ts_created_at, first)
            self.assertLess(ProjectMaster.objects.get(pk=project.pk).ts_created_at, first)
            channel = Channel.objects.get(project=project, kind=ChannelKind.PM)
            self.assertLess(Channel.objects.get(pk=channel.pk).ts_created_at, first)

    def test_the_search_index_clock_is_left_alone(self):
        """`ts_updated_at` of a task is what the chunkers window on: it stays at seeding time."""
        for _lang, (_visitor, _project, tasks) in self.each_language():
            for task in tasks.values():
                updated = TaskMaster.objects.get(pk=task.pk).ts_updated_at
                self.assertGreaterEqual(updated, self.before)

    def test_running_the_pass_again_with_the_same_now_changes_nothing(self):
        now = timezone.now()

        def snapshot(project):
            return (
                list(
                    TaskActivity.objects.filter(task__project=project)
                    .order_by("activity_id")
                    .values_list("ts_created_at", flat=True)
                ),
                list(
                    TaskComments.objects.filter(task__project=project)
                    .order_by("pk")
                    .values_list("ts_sent_at", "ts_updated_at")
                ),
                list(
                    Message.objects.filter(channel__project=project)
                    .order_by("seq")
                    .values_list("ts_sent_at", flat=True)
                ),
                list(
                    TaskMaster.objects.filter(project=project)
                    .order_by("task_id")
                    .values_list("ts_created_at", flat=True)
                ),
            )

        for _lang, (_visitor, project, _tasks) in self.each_language():
            demo_launch_plan.backdate_launch_plan(project, now)
            first = snapshot(project)
            demo_launch_plan.backdate_launch_plan(project, now)
            self.assertEqual(snapshot(project), first)

    def test_a_second_visitor_is_seeded_the_same_way(self):
        _visitor, _project, tasks = _seed("en", "bden2")
        now = timezone.now()
        for task in tasks.values():
            for row in self.activity(task):
                self.assertGreaterEqual(now - row.ts_created_at, MIN_AGE)

    def test_v1_is_untouched(self):
        visitor = _user("lpd_v1")
        env = create_demo_environment(visitor, short="bdv1", lang="en")
        self.assertFalse(
            ProjectMaster.objects.filter(
                team_id=env["team_id"], project_name="Launch plan"
            ).exists()
        )
