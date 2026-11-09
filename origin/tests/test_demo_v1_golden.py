"""The v1 demo content is the eval corpus: it must not change by accident.

`evals/fixture.py` seeds `create_demo_environment` with no version, and the
deploy-gating eval suites assert on ~40 title substrings and rank-sensitive
retrieval results over exactly what that seeds. The visitor-facing demo moved to
a polished "v2" presentation (MIP-1237); this test is the proof that the polish
did not leak into what the evals see, without running a billed eval.

It seeds the default (v1) workspace in both languages through the same call the
fixture makes, reduces everything a person or a retriever could read to a stable
text (titles, bodies, names, comments, messages, notes, to-dos, relative due
dates; never ids, timestamps or slugs), and compares a hash to the one recorded
on `main` before v2 existed.

If this fails on purpose (v1 content really was edited), re-run the evals
first, then update the hashes.
"""

import contextlib
import hashlib
import json
import re
from datetime import date, datetime, timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from origin.models.chat.todo_models import ToDoItem
from origin.models.chat.unified_models import Message
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectTags
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.demo_seeder import create_demo_environment

User = get_user_model()

# Recorded on main (before the v2 presentation layer), `short="golden"`.
#
# MIP-1357: re-recorded once, from the SAME v1 content. The first recording hashed
# the due dates written into note text as absolute dates, so it only held on the
# day it was made. The v1 content was checked unchanged first: with "today" frozen
# at 2026-11-08 the old digest still equalled the old hashes (en 08c6ae43...,
# ja b63c0d22...), and the date-normalised digest of that same content is below.
GOLDEN = {
    "en": "3283ac8ca9cf952bfb415f5aa9f9383c65e5982f49a18f3c03408c94c2dfb372",
    "ja": "a3f413b03ce2d0fd07c546559bb743c8a1c3729f8bcc38606e8f5462634b91f9",
}


def _plain(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def canonical_dump(team_id) -> str:
    """Everything readable in a seeded workspace, in a stable order."""
    team = TeamMaster.objects.get(team_id=team_id)
    today = date.today()
    out: list = [("team", team.team_name.replace("golden", "<short>"))]

    out.append(
        (
            "members",
            sorted(
                (m.attendee.username, m.attendee.role or "")
                for m in TeamMembers.objects.filter(team=team).select_related("attendee")
            ),
        )
    )
    for p in ProjectMaster.objects.filter(team=team).order_by("project_name"):
        out.append(("project", p.project_name.replace("golden", "<short>"), p.code))
        out.append(
            (
                "tags",
                sorted((t.tag_name, t.tag_color) for t in ProjectTags.objects.filter(project=p)),
            )
        )
    for m in MilestoneMaster.objects.filter(team=team).order_by("title"):
        out.append(("milestone", m.title, m.status, m.priority))
    for t in TaskMaster.objects.filter(team=team).order_by("title"):
        out.append(
            (
                "task",
                t.title,
                t.status,
                t.priority,
                t.effort_level,
                t.assignee.username if t.assignee_id else None,
                (t.due_date - today).days if t.due_date else None,
                t.tags,
                t.content,
            )
        )
    for c in (
        TaskComments.objects.filter(task__team=team)
        .select_related("task", "sender")
        .order_by("task__title", "comment_id")
    ):
        out.append(("comment", c.task.title, c.sender.username, c.comment_body))
    for msg in (
        Message.objects.filter(channel__team=team)
        .select_related("channel", "sender")
        .order_by("channel__title", "seq")
    ):
        out.append(
            (
                "message",
                msg.channel.title.replace("golden", "<short>"),
                msg.sender.username if msg.sender_id else None,
                msg.body,
            )
        )
    for model, name in (
        (PersonalNoteMaster, "note"),
        (TaskNoteMaster, "task_note"),
        (ChatNoteMaster, "chat_note"),
    ):
        out.append(
            (
                name,
                sorted((n.title, _plain(n.body)) for n in model.objects.filter(team=team)),
            )
        )
    out.append(
        (
            "todos",
            sorted(
                (i.title, _plain(i.notes), i.is_completed)
                for i in ToDoItem.objects.filter(group__team=team)
            ),
        )
    )
    # Row order is not a property of the data (DMs share an empty title, so the
    # database may return their messages in any order): compare as a sorted set.
    return _plain(sorted(out, key=_plain))


# BlockNote block ids and the user ids inside mentions are fresh uuids on every seed.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


# The seeder writes each due date into the task's note text as well ("📅 2026-12-03").
# `canonical_dump` already turns the structured `due_date` into a day offset; a date
# inside TEXT has to be turned the same way, or the digest moves by a day every day
# (the recorded hashes held on the day they were made and went red at the first run
# after UTC midnight, on `main` and on every PR). MIP-1357.
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _relative_dates(text: str, today: date) -> str:
    def offset(match: re.Match) -> str:
        try:
            days = (date.fromisoformat(match.group(0)) - today).days
        except ValueError:  # not a calendar date (e.g. 2026-13-45): leave it alone
            return match.group(0)
        return f"<today{days:+d}>"

    return _ISO_DATE.sub(offset, text)


def digest(team_id) -> str:
    text = _UUID.sub("<uuid>", canonical_dump(team_id))
    text = _relative_dates(text, date.today())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DemoV1GoldenTests(TestCase):
    def _seed(self, lang):
        user = User.objects.create_user(
            username=f"golden_{lang}",
            email=f"golden_{lang}@example.com",
            password="x",
            is_demo=True,
        )
        env = create_demo_environment(user, short="golden", lang=lang)
        return env["team_id"]

    def test_default_v1_english_is_unchanged(self):
        self.assertEqual(digest(self._seed("en")), GOLDEN["en"])

    def test_default_v1_japanese_is_unchanged(self):
        self.assertEqual(digest(self._seed("ja")), GOLDEN["ja"])

    # The two tests above run on whatever day CI runs, which is how a digest that
    # depended on the calendar passed on the day it was recorded and failed every
    # day after (MIP-1357). These seed on days chosen to differ from it: another
    # weekday, another month, and a year boundary.
    def test_the_digest_does_not_move_with_the_calendar(self):
        with _on(date(2027, 2, 28)):
            self.assertEqual(digest(self._seed("en")), GOLDEN["en"])

    def test_the_japanese_digest_does_not_move_with_the_calendar(self):
        with _on(date(2026, 12, 31)):
            self.assertEqual(digest(self._seed("ja")), GOLDEN["ja"])


class RelativeDatesTests(TestCase):
    """`_relative_dates` keeps what a due date MEANS (how far away it is), so editing a
    v1 due date still changes the digest, while the calendar day does not."""

    TODAY = date(2026, 11, 8)

    def test_a_date_becomes_its_distance_from_today(self):
        self.assertEqual(_relative_dates("📅 2026-12-03", self.TODAY), "📅 <today+25>")

    def test_a_past_date_is_negative_and_today_is_zero(self):
        self.assertEqual(_relative_dates("2026-11-01", self.TODAY), "<today-7>")
        self.assertEqual(_relative_dates("2026-11-08", self.TODAY), "<today+0>")

    def test_every_date_in_a_text_is_converted(self):
        self.assertEqual(
            _relative_dates("from 2026-11-09 to 2026-11-12", self.TODAY),
            "from <today+1> to <today+4>",
        )

    def test_the_same_due_date_is_the_same_text_on_any_day(self):
        self.assertEqual(
            _relative_dates("📅 2026-12-03", date(2026, 11, 8)),
            _relative_dates("📅 2026-12-04", date(2026, 11, 9)),
        )

    def test_a_different_due_date_is_a_different_text(self):
        self.assertNotEqual(
            _relative_dates("📅 2026-12-03", self.TODAY),
            _relative_dates("📅 2026-12-04", self.TODAY),
        )

    def test_something_that_only_looks_like_a_date_is_left_alone(self):
        for text in ("2026-13-45", "1234-56-78", "v2026-11-0812", "id 12026-11-081"):
            with self.subTest(text=text):
                self.assertEqual(_relative_dates(text, self.TODAY), text)


@contextlib.contextmanager
def _on(day: date):
    """Run with "today" as `day`, in the seeder, in this module and in Django's clock."""

    class _FrozenDate(date):
        @classmethod
        def today(cls):
            return cls(day.year, day.month, day.day)

    noon = datetime(day.year, day.month, day.day, 12, 0, tzinfo=timezone.utc)
    with (
        mock.patch("origin.services.demo_seeder.date", _FrozenDate),
        mock.patch(f"{__name__}.date", _FrozenDate),
        mock.patch("django.utils.timezone.now", return_value=noon),
    ):
        yield
