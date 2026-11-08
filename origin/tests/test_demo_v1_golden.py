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

import hashlib
import json
import re
from datetime import date

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
GOLDEN = {
    "en": "08c6ae43215f929cf7d525bb96d38008a09bf9ddfdbaebbeb851053563e2821e",
    "ja": "b63c0d22a5a7edfa154752b02742b026f180d6b91d482c5cca19eb3f3dd018bd",
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


def digest(team_id) -> str:
    text = _UUID.sub("<uuid>", canonical_dump(team_id))
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
