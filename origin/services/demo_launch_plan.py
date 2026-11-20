"""A launch plan in the v2 demo workspace, part of it written through a connected Claude app (MIP-1740).

The landing page's hero shows a REAL product screenshot with "by the agent / by you" labels. Those
labels come from the `via` a write is stamped with (`get_current_source`), so they can only appear
when something in the demo was written the way a connected AI app writes. This module seeds a small,
believable launch plan where some of it was:

* a project "Launch plan" with a milestone and four tasks: three made by the visitor's Claude app and
  one by the visitor; a comment and a priority change by each; and a decision note ("Decision:
  launch on <the date>") that Claude wrote and the visitor then added a line to.

**The dates are relative and the rows are days old (MIP-1745).** The launch is the first Thursday at
least three weeks after the visitor's today and every other date is an offset from it (`plan_dates`),
so the sample reads like real work in any month; the text is a template filled in at seeding
(`resolve_copy`). The rows are backdated by `backdate_launch_plan` (see there) before the
transaction commits, so no visitor is ever served a task row, comment or card that reads "just now".
(The decision note, its versions and the sprint keep their seeding-time stamps: the seeder leaves
notes alone on purpose, see the module comment above `demo_seeder._backdate`.)

**Nothing is hand-written into a row.** Every record goes through the model save the product's own
write uses, under the same thread-local the request middleware / MCP set:

  * the source is `set_current_user(user, via=...)` (here `_acting_as` + `via_override`), so
    `get_current_source()` reports it, and the stamping sites read it from there:
    `TaskActivity.metadata["via"]` (`task_signals._record`, the `created` row, the priority row and
    each `comment_added` row), `NoteVersionMaster.via` (`write_via.recorded_via`, from
    `snapshot_note_version`) and `Message.metadata["via"]` (`message_via.stamped`, on the chat card
    and on the mirror of each comment);
  * the `via` itself is what `McpOAuthCredential.via()` returns for a connection to an official
    Claude client (kind "oauth", `app: "claude"` earned from the client's registered redirect URI
    by `mcp_client_family.app_family`, never from its name). The credential is built in memory from
    plain objects: no `Application` or `McpConnection` row is written, so nothing new appears in the
    visitor's Settings and nothing else needs cleaning up.

A connected app writes AS ITS OWNER, so the actor of Claude's rows is the visitor (the visitor reads
the whole `via`, name included); a colleague reads only `{"kind": "oauth", "app": "claude"}`
(`via_privacy.via_for_viewer`). The rows the visitor wrote themselves are written with the plain
`_acting_as(visitor)` the rest of the seeder uses: their source is the web app's `{"kind": "app"}`,
which earns no chip, and their note version has no `via`.

Runs ONLY for v2 (`create_demo_environment(version="v2")`); v1 is the frozen eval corpus and never
imports this module. It writes nothing outside the new project, and `delete_demo_team_data` sweeps it
with the team (tasks, notes, versions, messages, project).
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from django.utils import timezone

from origin.middleware.current_user import via_override
from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import (
    ProjectLabelAssignment,
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
)
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import demo_v2, message_via
from origin.services.demo_blocks import _body, _text_body
from origin.services.demo_seeder import (
    _acting_as,
    _backdate,
    _backdate_each,
    _backdate_pm_channel,
    _create_milestone_with_backing_task,
    _create_sprint,
)
from origin.services.mcp_credential import McpOAuthCredential
from origin.services.project_code import derive_project_code
from origin.services.task_card_post import _task_card_parts
from origin.services.user_time import user_today
from origin.views.utils.note_role import ROLE_OWNER
from origin.views.utils.note_version import snapshot_note_version

logger = logging.getLogger(__name__)

NOTE_TYPE_TASK = 2

# The department that owns the plan (index into the story's four people, see `demo_company`), and the
# two people besides the visitor who are in the project.
PRODUCT, DESIGN = 0, 2

# The name the visitor gave their Claude connection. It is the owner's alone (never shown to a
# colleague), so any harmless word will do.
CLAUDE_CONNECTION_NAME = "Claude"
# claude.ai's one fixed callback (`mcp_client_family`): the only thing `app_family` reads.
CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"

# The plan's dates are OFFSETS from the visitor's today, computed when the workspace is seeded, so
# the sample reads like real work in any month (MIP-1745). The launch is the first Thursday at
# least three weeks out; every other date is a fixed number of days from it, so each of them is a
# weekday too (a demo room booked for a Saturday would be the tell this is made up).
LAUNCH_LEAD_DAYS = 21
THURSDAY = 3
REVIEW_BEFORE_LAUNCH = 7  # the legal review ends a week before the launch
ROOM_BEFORE_LAUNCH = 1  # the demo is the day before
LATER_AFTER_LAUNCH = 7  # the option "a week later than we need"

MONTHS_EN = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def claude_via(user) -> dict[str, Any]:
    """The `via` a write by `user`'s connected Claude app carries, as `McpOAuthCredential.via()`
    produces it (`kind`, `connection_id`, `client_name`, and the official `app` family).

    Built by that code from an in-memory connection (see the module docstring), so the shape can
    never drift from the real stamp. Refuses to continue if the family is not earned, because a
    generic "via MCP" chip would silently replace the Claude mark the page is about."""
    connection = SimpleNamespace(
        id=uuid.uuid4(),
        user=user,
        scope="write",
        allow_destructive=False,
        application=SimpleNamespace(
            name=CLAUDE_CONNECTION_NAME,
            client_id=uuid.uuid4().hex,
            redirect_uris=CLAUDE_REDIRECT_URI,
        ),
    )
    via = McpOAuthCredential(connection).via()
    if via.get("kind") != "oauth" or via.get("app") != "claude":
        raise RuntimeError(f"the demo's Claude connection did not earn its family: {via!r}")
    return via


@contextmanager
def _writing(actor, via):
    """Writes in this block are `actor`'s, arriving through `via` (None: the web app).

    `_acting_as` is the OUTER manager on purpose: it calls `set_current_user(actor)`, which resets
    the source, so `via_override` has to be applied after it. Nothing inside may call `_acting_as`
    or `_save_as` (they would reset the source and every later write would lose its stamp).
    """
    with _acting_as(actor):
        if via is None:
            yield
        else:
            with via_override(via):
                yield


def plan_dates(today: date) -> dict[str, Any]:
    """Every date of the plan, as offsets from `today` (pure: any day of the year can be tried).

    `launch`, `review` (the legal review ends), `room` (the demo room is booked), `later` (the
    "a week later" option), and `due[task key]`."""
    launch = today + timedelta(days=LAUNCH_LEAD_DAYS)
    launch += timedelta(days=(THURSDAY - launch.weekday()) % 7)
    return {
        "launch": launch,
        "review": launch - timedelta(days=REVIEW_BEFORE_LAUNCH),
        "room": launch - timedelta(days=ROOM_BEFORE_LAUNCH),
        "later": launch + timedelta(days=LATER_AFTER_LAUNCH),
        "due": {spec["key"]: launch - timedelta(days=spec["due_before"]) for spec in TASKS},
    }


def format_day(lang: str, day: date) -> str:
    """ "April 1" / "4月1日", written by hand: `strftime` would follow the server's locale, and the
    year is left out on purpose (every date of the plan is within five weeks of today)."""
    if lang == "ja":
        return f"{day.month}月{day.day}日"
    return f"{MONTHS_EN[day.month - 1]} {day.day}"


def format_dates(lang: str, dates: dict[str, Any]) -> dict[str, str]:
    """The dates the text mentions, as the text writes them (the keys of its `{placeholders}`)."""
    return {key: format_day(lang, dates[key]) for key in ("launch", "review", "room", "later")}


# `by`: who made the task ("claude" = the visitor's connected app, "member" = the visitor by hand).
# `then`: a later priority change, `(by, value)`. `assignee` is a blueprint-style key.
TASKS = (
    {
        "key": "legal",
        "by": "claude",
        "assignee": "demo",
        "status": "WIP",
        "priority": "Normal",
        "due_before": 7,
        "tags": (1,),
        "effort": "Moderate",
        "then": ("claude", "High"),
    },
    {
        "key": "announce",
        "by": "claude",
        "assignee": "demo",
        "status": "Open",
        "priority": "Normal",
        "due_before": 3,
        "tags": (0,),
        "effort": "Low",
        "then": None,
    },
    {
        "key": "room",
        "by": "claude",
        "assignee": "bot0",
        "status": "Open",
        "priority": "Normal",
        "due_before": 6,
        "tags": (2,),
        "effort": "Minimal",
        "then": ("member", "High"),
    },
    {
        "key": "script",
        "by": "member",
        "assignee": "demo",
        "status": "Open",
        "priority": "Normal",
        "due_before": 2,
        "tags": (2,),
        "effort": "Moderate",
        "then": None,
    },
)

# In the order they are written: (task key, by, copy key).
COMMENTS = (
    ("legal", "claude", "legal_claude"),
    ("announce", "claude", "announce_claude"),
    ("announce", "member", "announce_member"),
    ("room", "member", "room_member"),
    ("room", "claude", "room_claude"),
)

COPY = {
    "en": {
        "project": "Launch plan",
        "tags": [
            ("Comms", "#7c3aed", "#ffffff"),
            ("Legal", "#0ea5e9", "#ffffff"),
            ("Event", "#10b981", "#ffffff"),
        ],
        "milestone": (
            "Launch readiness",
            "Have the announcement, the demo and the legal sign-off ready by the launch date.",
        ),
        "tasks": {
            "legal": (
                "Confirm legal sign-off",
                "Get written sign-off on the launch wording. The legal review ends on {review}.",
            ),
            "announce": (
                "Draft the announcement",
                "A short post for the blog and the team chat: what is launching, when, and who it is for.",
            ),
            "room": (
                "Book the demo room",
                "Reserve a room with a screen for the launch demo on {room}.",
            ),
            "script": (
                "Prepare the demo script",
                "One page: what we show, in which order, and what we say.",
            ),
        },
        "comments": {
            "legal_claude": "I raised the priority: nothing can be announced before sign-off, and the legal review ends on {review}.",
            "announce_claude": "The first draft is ready: three short paragraphs, opening with the {launch} date. I left the legal wording out until sign-off.",
            "announce_member": "Thanks. I will read it tomorrow and send it once legal has signed off.",
            "room_member": "Rooms fill up fast, so I raised this to High.",
            "room_claude": "Room B is free on {room} from 14:00 to 16:00. I put a hold on it until you confirm.",
        },
        "note_title": "Decision: launch on {launch}",
        "note_sections": lambda who, day: (
            ("Decision", [f"Launch on {day['launch']}."]),
            (
                "Why",
                [
                    f"Because the legal review ends on {day['review']}, and nothing can be announced "
                    "before sign-off. That leaves one week to finish the announcement and the demo."
                ],
            ),
            (
                "Other options",
                [
                    ("bullet", f"{day['review']}: the day the review ends, too tight."),
                    ("bullet", f"{day['later']}: a week later than we need."),
                ],
            ),
            (
                "Decided by",
                [f"Proposed by Claude, approved by {who}. It can be reversed if the review slips."],
            ),
        ),
        "note_update": ("Update", ["Checked with the team: nobody is away on {launch}."]),
    },
    "ja": {
        "project": "ローンチ計画",
        "tags": [
            ("広報", "#7c3aed", "#ffffff"),
            ("法務", "#0ea5e9", "#ffffff"),
            ("イベント", "#10b981", "#ffffff"),
        ],
        "milestone": (
            "ローンチの準備",
            "ローンチ日までに、告知とデモと法務の承認をそろえます。",
        ),
        "tasks": {
            "legal": (
                "法務の承認を確認する",
                "ローンチの文言について、法務から書面での承認をもらいます。法務のレビューは{review}に終わります。",
            ),
            "announce": (
                "告知文の下書きを書く",
                "ブログとチームチャット用の短い告知文。何がいつ始まり、誰のためのものかを書きます。",
            ),
            "room": (
                "デモ用の会議室を予約する",
                "{room}のローンチデモのために、画面のある会議室を押さえます。",
            ),
            "script": (
                "デモの台本を用意する",
                "1ページにまとめます。何をどの順で見せ、何を話すか。",
            ),
        },
        "comments": {
            "legal_claude": "優先度を上げました。承認が下りるまで何も告知できず、法務のレビューは{review}に終わります。",
            "announce_claude": "最初の下書きができました。短い3段落で、冒頭に{launch}を書いています。法務の文言は、承認が下りるまで入れていません。",
            "announce_member": "ありがとう。明日読んで、法務の承認が下りたら送ります。",
            "room_member": "会議室はすぐ埋まるので、優先度を高にしました。",
            "room_claude": "Bルームは{room}の14:00から16:00まで空いています。確認をもらうまで仮押さえしておきます。",
        },
        "note_title": "決定: {launch}にローンチする",
        "note_sections": lambda who, day: (
            ("決定", [f"{day['launch']}にローンチします。"]),
            (
                "理由",
                [
                    f"法務のレビューが{day['review']}に終わり、承認が下りるまで何も告知できないためです。"
                    "残りの1週間で、告知とデモの準備を終えられます。"
                ],
            ),
            (
                "検討した他の案",
                [
                    ("bullet", f"{day['review']}: レビューが終わる当日で、余裕がありません。"),
                    ("bullet", f"{day['later']}: 必要以上に1週間遅くなります。"),
                ],
            ),
            ("決定者", [f"Claude が提案し、{who} が承認しました。レビューが遅れたら見直せます。"]),
        ),
        "note_update": ("更新", ["チームに確認しました。{launch}に不在の人はいません。"]),
    },
}


def resolve_copy(lang: str, today: date) -> dict[str, Any]:
    """The plan's text for `lang` with the dates of `today` filled in (`COPY` holds templates).

    `note_sections` stays a function of the author's name, so a name is never read as a template
    (a `{` in a username must not break the seeding)."""
    template = COPY.get(lang, COPY["en"])
    days = format_dates(lang if lang in COPY else "en", plan_dates(today))

    def fill(text: str) -> str:
        return text.format(**days)

    note_heading, note_lines = template["note_update"]
    return {
        **template,
        "milestone": tuple(fill(text) for text in template["milestone"]),
        "tasks": {
            key: tuple(fill(text) for text in texts) for key, texts in template["tasks"].items()
        },
        "comments": {key: fill(text) for key, text in template["comments"].items()},
        "note_title": fill(template["note_title"]),
        "note_sections": lambda who: template["note_sections"](who, days),
        "note_update": (fill(note_heading), [fill(line) for line in note_lines]),
    }


def _task_body(summary: str) -> list:
    return _body(("🧾 Summary", [summary]))


def _post_card(task, channel) -> None:
    """The task's chat card, as the product builds it for a task made over MCP.

    `_task_card_parts` is the builder `task_card_post` uses for that card AND for its refresh after
    an edit, so the refresh that runs when the sign-in commits finds the card current and writes
    nothing. Its labels are English (the refresh's are too); `message_via.stamped` is the funnel
    that puts the connected app's kind and family on the message, for the icon."""
    fresh = TaskMaster.objects.select_related("project", "assignee", "reporter").get(pk=task.pk)
    body, body_text, metadata = _task_card_parts(fresh)
    next_seq = Message.objects.filter(channel=channel).count() + 1
    Message.objects.create(
        channel=channel,
        sender=fresh.reporter,
        seq=next_seq,
        body=body,
        body_text=body_text,
        task=fresh,
        metadata=message_via.stamped(metadata),
    )


def _comment(task, visitor, text: str, project_id: int) -> None:
    """One comment, the way the comment API and the MCP `add_comment` tool write it: the row (its
    save is what records `comment_added` with the source) and its mirror under the task's chat card.

    NOT `fan_out_task_comment`: that also notifies people, calls the webhook and pushes to
    genos-sockets, none of which a seeder should do."""
    from origin.services.unified_writer import write_task_comment_as_thread_reply  # noqa: PLC0415

    comment = TaskComments.objects.create(
        task=task,
        sender=visitor,
        comment_id=TaskComments.objects.filter(task=task).count() + 1,
        comment_body=_text_body(text),
    )
    write_task_comment_as_thread_reply(
        task_id=task.task_id,
        comment_id=comment.comment_id,
        sender_id=str(visitor.id),
        body=comment.comment_body,
        project_id=project_id,
        bypass_flag=True,
    )


def create_launch_plan(team, demo_user, bots, lang, *, label, sprint_name) -> ProjectMaster:
    """Add the launch plan to a v2 demo workspace; returns its project.

    `label` is the Product department's project label (from `create_company_layer`).
    Runs inside `create_demo_environment`'s transaction.
    """
    # The visitor's own day (their stored zone, else the server's: it is NULL for a brand-new
    # visitor, whose browser has not reported one yet), so "three weeks out" is theirs.
    today = user_today(demo_user)
    copy = resolve_copy(lang, today)
    dates = plan_dates(today)
    claude = claude_via(demo_user)
    via_of = {"claude": claude, "member": None}

    project_name = copy["project"]
    project = ProjectMaster.objects.create(
        team=team,
        project_name=project_name,
        code=derive_project_code(
            project_name,
            set(
                ProjectMaster.objects.filter(team=team, code__isnull=False).values_list(
                    "code", flat=True
                )
            ),
        ),
        owner=demo_user,
        project_system_user=demo_user,
        is_private=False,
    )
    # `.create`, not bulk: the project-chat membership signal fires on save.
    for user in (demo_user, bots[PRODUCT], bots[DESIGN]):
        ProjectMembers.objects.create(team=team, project=project, attendee=user)
    ProjectTags.objects.bulk_create(
        [
            ProjectTags(
                team=team,
                project=project,
                tag_id=index + 1,
                tag_name=name,
                tag_color=color,
                tag_text_color=text_color,
            )
            for index, (name, color, text_color) in enumerate(copy["tags"])
        ]
    )
    ProjectLabelAssignment.objects.create(label=label, project=project)
    sprint = _create_sprint(team, project, name_template=sprint_name)

    milestone_title, milestone_goal = copy["milestone"]
    with _acting_as(demo_user):
        milestone, backing = _create_milestone_with_backing_task(
            team,
            project,
            sprint,
            demo_user,
            bots,
            {
                "title": milestone_title,
                "body": _body(("🎯 Goal", [milestone_goal])),
                "status": "WIP",
                "priority": "Normal",
                # The seeder adds this to ITS today (the server's), so the offset is counted from
                # that one: the milestone lands on the launch day whatever zone the visitor is in.
                "due_offset_days": (dates["launch"] - date.today()).days,
            },
        )

    people = {"demo": demo_user, "bot0": bots[PRODUCT]}
    tasks: dict[str, TaskMaster] = {}
    for spec in TASKS:
        title, summary = copy["tasks"][spec["key"]]
        with _writing(demo_user, via_of[spec["by"]]):
            tasks[spec["key"]] = TaskMaster.objects.create(
                team=team,
                project=project,
                sprint=sprint,
                milestone=milestone,
                assignee=people[spec["assignee"]],
                reporter=demo_user,
                title=title,
                status=spec["status"],
                priority=spec["priority"],
                content=_task_body(summary),
                due_date=dates["due"][spec["key"]],
                tags=demo_v2.task_tags(copy["tags"], spec["tags"]),
                effort_level=spec["effort"],
                effort_level_code=demo_v2.EFFORT_CODES[spec["effort"]],
                mentioned_user_ids=[],
                parent_task_id=backing.task_id,
            )

    # A priority change, by whoever made it. Reloaded first: a stale instance makes the signal
    # invent a phantom change on the next save (see `_replay_task_history`).
    for spec in TASKS:
        if spec["then"] is None:
            continue
        by, priority = spec["then"]
        task = tasks[spec["key"]]
        with _writing(demo_user, via_of[by]):
            task.refresh_from_db()
            task.priority = priority
            task.save(update_fields=["priority"])

    # The chat cards come after the edits, so they show the tasks as they now are.
    channel = Channel.objects.get(project=project, kind=ChannelKind.PM)
    for spec in TASKS:
        with _writing(demo_user, via_of[spec["by"]]):
            _post_card(tasks[spec["key"]], channel)

    for key, by, copy_key in COMMENTS:
        with _writing(demo_user, via_of[by]):
            _comment(tasks[key], demo_user, copy["comments"][copy_key], project.project_id)

    # The decision note: Claude writes it (a task note on the task it rests on, with the owner's
    # role row and the first version: the trio `create_note` writes), then the visitor adds a line.
    legal = tasks["legal"]
    sections = copy["note_sections"](demo_user.username)
    with _writing(demo_user, claude):
        note = TaskNoteMaster.objects.create(
            team=team,
            project=project,
            owner=demo_user,
            task=legal,
            title=copy["note_title"],
            body=_body(*sections),
        )
        NotePermissionMaster.objects.create(
            team=team,
            user=demo_user,
            note_id=note.note_id,
            note_type=NOTE_TYPE_TASK,
            role_id=ROLE_OWNER,
        )
        snapshot_note_version(
            team=team,
            editor=demo_user,
            note_type=NOTE_TYPE_TASK,
            note_id=note.note_id,
            title=note.title,
            body=note.body,
        )
    with _writing(demo_user, None):
        note.body = _body(*sections, copy["note_update"])
        note.save(update_fields=["body"])
        snapshot_note_version(
            team=team,
            editor=demo_user,
            note_type=NOTE_TYPE_TASK,
            note_id=note.note_id,
            title=note.title,
            body=note.body,
        )
    backdate_launch_plan(project, timezone.now())
    return project


# --- backdating (MIP-1745) ---------------------------------------------------------------------
#
# The plan's project is not in `seeded_projects`, so the seeder's own pass (`_backdate_history`)
# never reaches it and every row read "just now". This pass follows the recipe of the retake of the
# landing page's hero screenshot: per task the oldest row first and the rows TWO days apart, the
# newest at least two days old.
#
#   * WHOLE days, never a little under: a row 47 hours old floors to "1 day", and in Japanese
#     exactly one day reads "11 日前" until the frontend fix (genos-frontend #1053) is deployed.
#   * All tasks start at the same time (they were made together: three by one Claude request), and
#     the minutes below only keep them in id order. That is what the PM channel needs: its cards are
#     stamped with their task's creation time and must not regress against `seq`
#     (`_assert_seq_monotonic`), so a task with more rows must not start earlier than its
#     neighbour (right-aligning every task to "now - 2 days" would make the 4-row task the oldest).
#   * The activity feed sorts by id, so a higher id is never older.
FIRST_ROW_DAYS = 8  # the first row of every task
ROW_STEP_DAYS = 2  # between a task's rows
NEWEST_MIN_DAYS = 2  # no row is younger than this
MILESTONE_DAYS = FIRST_ROW_DAYS + ROW_STEP_DAYS  # the milestone frames the work, so it is older
PROJECT_DAYS = MILESTONE_DAYS + ROW_STEP_DAYS  # and the project (and its channel) older still


def backdate_launch_plan(project: ProjectMaster, now: datetime) -> None:
    """Push the plan's DISPLAY timestamps into the past (idempotent for a given `now`).

    Every write is a queryset `.update()`, which skips `auto_now`: that is intended and safe here.
    The pass runs inside `create_demo_environment`'s transaction, so no browser has synced a row
    yet, and what the browser syncs on (`TaskMaster.ts_updated_at`, `Message.ts_updated_at`) is not
    touched at all: it stays at seeding time, which is also what keeps the plan inside Spotlight's
    incremental reindex window and its freshness decay (`task_chunker` and `chat_chunker` read it;
    see the module comment above `demo_seeder._backdate`).

    The one `ts_updated_at` that IS set is a comment's, to its own `ts_sent_at`: the comment bubble
    shows "edited" when the two differ to the second. Nothing indexes it on its own: the task's
    chunk carries the comments and the task is dirty (see above).
    """
    tasks = list(TaskMaster.objects.filter(project=project, is_milestone=False).order_by("task_id"))
    count = len(tasks)

    def at(days: int, rank: int = 0) -> datetime:
        # `rank` minutes EARLIER than the whole day: the age never falls under `days`.
        return now - timedelta(days=days, minutes=count - rank)

    for rank, task in enumerate(tasks):
        rows = list(TaskActivity.objects.filter(task=task).order_by("activity_id"))
        if not rows:
            continue
        # A sign-in must never fail for a cosmetic pass: a task with more rows than fit (a later
        # change that records one more row per save) gets a tighter step, and past that equal
        # days; the tests pin the two-day spacing of today's rows.
        step = ROW_STEP_DAYS
        if len(rows) > 1 and FIRST_ROW_DAYS - step * (len(rows) - 1) < NEWEST_MIN_DAYS:
            step = max((FIRST_ROW_DAYS - NEWEST_MIN_DAYS) // (len(rows) - 1), 0)
            logger.warning(
                "launch plan task %s: %s rows, step %s days", task.task_id, len(rows), step
            )
        days = [max(FIRST_ROW_DAYS - step * index, NEWEST_MIN_DAYS) for index in range(len(rows))]
        stamps = {row.pk: at(day, rank) for row, day in zip(rows, days, strict=True)}
        _backdate_each(TaskActivity, stamps, "ts_created_at")
        _backdate(TaskMaster, [task.pk], ts_created_at=stamps[rows[0].pk])

        # A comment is as old as the activity row it recorded (the same order: both are written
        # in one step).
        commented = [r for r in rows if r.action_type == TaskActivityActionType.COMMENT_ADDED]
        comments = list(TaskComments.objects.filter(task=task).order_by("comment_id"))
        if len(commented) != len(comments):
            logger.warning("launch plan task %s: comments and rows disagree", task.task_id)
        for row, comment in zip(commented, comments, strict=False):
            sent = stamps[row.pk]
            TaskComments.objects.filter(pk=comment.pk).update(ts_sent_at=sent, ts_updated_at=sent)

    # The milestone, its backing task, the project and the channel predate the work.
    milestone_at = at(MILESTONE_DAYS)
    backing_pks = list(
        TaskMaster.objects.filter(project=project, is_milestone=True).values_list("pk", flat=True)
    )
    _backdate(TaskMaster, backing_pks, ts_created_at=milestone_at)
    _backdate(
        TaskActivity,
        TaskActivity.objects.filter(task_id__in=backing_pks).values_list("pk", flat=True),
        ts_created_at=milestone_at,
    )
    _backdate(
        MilestoneMaster,
        MilestoneMaster.objects.filter(project=project).values_list("pk", flat=True),
        ts_created_at=milestone_at,
    )
    _backdate(ProjectMaster, [project.pk], ts_created_at=at(PROJECT_DAYS))
    channel = Channel.objects.filter(project=project, kind=ChannelKind.PM).first()
    if channel:
        _backdate(Channel, [channel.pk], ts_created_at=at(PROJECT_DAYS))
        # Cards take their task's time and each mirrored reply its comment's: the seeder's own
        # helper, which also asserts a reply never predates its card.
        _backdate_pm_channel(channel, now)
