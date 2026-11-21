"""A launch plan in the v2 demo workspace, part of it written through a connected Claude app (MIP-1740).

The landing page's hero shows a REAL product screenshot with "by the agent / by you" labels. Those
labels come from the `via` a write is stamped with (`get_current_source`), so they can only appear
when something in the demo was written the way a connected AI app writes. This module seeds a small,
believable launch plan where some of it was:

* a project "Launch plan" with a milestone and four tasks: three made by the visitor's Claude app and
  one by the visitor; a comment and a priority change by each; and a decision note ("Decision:
  launch on March 12") that Claude wrote and the visitor then added a line to.

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

import uuid
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from typing import Any

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
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import demo_v2, message_via
from origin.services.demo_blocks import _body, _text_body
from origin.services.demo_seeder import (
    _acting_as,
    _create_milestone_with_backing_task,
    _create_sprint,
)
from origin.services.mcp_credential import McpOAuthCredential
from origin.services.project_code import derive_project_code
from origin.services.task_card_post import _task_card_parts
from origin.views.utils.note_role import ROLE_OWNER
from origin.views.utils.note_version import snapshot_note_version

NOTE_TYPE_TASK = 2

# The department that owns the plan (index into the story's four people, see `demo_company`), and the
# two people besides the visitor who are in the project.
PRODUCT, DESIGN = 0, 2

# The name the visitor gave their Claude connection. It is the owner's alone (never shown to a
# colleague), so any harmless word will do.
CLAUDE_CONNECTION_NAME = "Claude"
# claude.ai's one fixed callback (`mcp_client_family`): the only thing `app_family` reads.
CLAUDE_REDIRECT_URI = "https://claude.ai/api/mcp/auth_callback"

# Dates in the text are fixed ("launch on March 12"), so the due dates are the SAME upcoming March
# rather than offsets from today: the plan reads the same on any day the visitor signs in.
LAUNCH_DAY = 12


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


def next_march(day: int, today: date) -> date:
    """The coming 12 March (or 5, 9 ...): this year's unless it has passed."""
    candidate = date(today.year, 3, day)
    return candidate if candidate >= today else date(today.year + 1, 3, day)


# `by`: who made the task ("claude" = the visitor's connected app, "member" = the visitor by hand).
# `then`: a later priority change, `(by, value)`. `assignee` is a blueprint-style key.
TASKS = (
    {
        "key": "legal",
        "by": "claude",
        "assignee": "demo",
        "status": "WIP",
        "priority": "Normal",
        "due_day": 5,
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
        "due_day": 9,
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
        "due_day": 6,
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
        "due_day": 10,
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
            "March launch",
            "Have the announcement, the demo and the legal sign-off ready by the launch date.",
        ),
        "tasks": {
            "legal": (
                "Confirm legal sign-off",
                "Get written sign-off on the launch wording. The legal review ends on March 5.",
            ),
            "announce": (
                "Draft the announcement",
                "A short post for the blog and the team chat: what is launching, when, and who it is for.",
            ),
            "room": (
                "Book the demo room",
                "Reserve a room with a screen for the launch demo on March 11.",
            ),
            "script": (
                "Prepare the demo script",
                "One page: what we show, in which order, and what we say.",
            ),
        },
        "comments": {
            "legal_claude": "I raised the priority: nothing can be announced before sign-off, and the legal review ends on March 5.",
            "announce_claude": "The first draft is ready: three short paragraphs, opening with the March 12 date. I left the legal wording out until sign-off.",
            "announce_member": "Thanks. I will read it tomorrow and send it once legal has signed off.",
            "room_member": "Rooms fill up fast, so I raised this to High.",
            "room_claude": "Room B is free on March 11 from 14:00 to 16:00. I put a hold on it until you confirm.",
        },
        "note_title": "Decision: launch on March 12",
        "note_sections": lambda who: (
            ("Decision", ["Launch on March 12."]),
            (
                "Why",
                [
                    "Because the legal review ends on March 5, and nothing can be announced before sign-off. "
                    "That leaves one week to finish the announcement and the demo."
                ],
            ),
            (
                "Other options",
                [
                    ("bullet", "March 5: the day the review ends, too tight."),
                    ("bullet", "March 19: a week later than we need."),
                ],
            ),
            (
                "Decided by",
                [f"Proposed by Claude, approved by {who}. It can be reversed if the review slips."],
            ),
        ),
        "note_update": ("Update", ["Checked with the team: nobody is away on March 12."]),
    },
    "ja": {
        "project": "ローンチ計画",
        "tags": [
            ("広報", "#7c3aed", "#ffffff"),
            ("法務", "#0ea5e9", "#ffffff"),
            ("イベント", "#10b981", "#ffffff"),
        ],
        "milestone": (
            "3月のローンチ",
            "ローンチ日までに、告知とデモと法務の承認をそろえます。",
        ),
        "tasks": {
            "legal": (
                "法務の承認を確認する",
                "ローンチの文言について、法務から書面での承認をもらいます。法務のレビューは3月5日に終わります。",
            ),
            "announce": (
                "告知文の下書きを書く",
                "ブログとチームチャット用の短い告知文。何がいつ始まり、誰のためのものかを書きます。",
            ),
            "room": (
                "デモ用の会議室を予約する",
                "3月11日のローンチデモのために、画面のある会議室を押さえます。",
            ),
            "script": (
                "デモの台本を用意する",
                "1ページにまとめます。何をどの順で見せ、何を話すか。",
            ),
        },
        "comments": {
            "legal_claude": "優先度を上げました。承認が下りるまで何も告知できず、法務のレビューは3月5日に終わります。",
            "announce_claude": "最初の下書きができました。短い3段落で、冒頭に3月12日を書いています。法務の文言は、承認が下りるまで入れていません。",
            "announce_member": "ありがとう。明日読んで、法務の承認が下りたら送ります。",
            "room_member": "会議室はすぐ埋まるので、優先度を高にしました。",
            "room_claude": "Bルームは3月11日の14:00から16:00まで空いています。確認をもらうまで仮押さえしておきます。",
        },
        "note_title": "決定: 3月12日にローンチする",
        "note_sections": lambda who: (
            ("決定", ["3月12日にローンチします。"]),
            (
                "理由",
                [
                    "法務のレビューが3月5日に終わり、承認が下りるまで何も告知できないためです。"
                    "残りの1週間で、告知とデモの準備を終えられます。"
                ],
            ),
            (
                "検討した他の案",
                [
                    ("bullet", "3月5日: レビューが終わる当日で、余裕がありません。"),
                    ("bullet", "3月19日: 必要以上に1週間遅くなります。"),
                ],
            ),
            ("決定者", [f"Claude が提案し、{who} が承認しました。レビューが遅れたら見直せます。"]),
        ),
        "note_update": ("更新", ["チームに確認しました。3月12日に不在の人はいません。"]),
    },
}


def _task_body(summary: str) -> list:
    return _body(("🧾 Summary", [summary]))


def _post_card(task, channel, lang) -> None:
    """The task's chat card, as the product builds it for a task made over MCP.

    `_task_card_parts` is the builder `task_card_post` uses for that card AND for its refresh after
    an edit, so the refresh that runs when the sign-in commits finds the card current and writes
    nothing. Its labels are in the workspace's language `lang`, which is the language of the card's
    sender (the reporter: the visitor, whose `language` the seeder set), the one the refresh reads
    (MIP-1744); `message_via.stamped` is the funnel that puts the connected app's kind and family
    on the message, for the icon."""
    fresh = TaskMaster.objects.select_related("project", "assignee", "reporter").get(pk=task.pk)
    body, body_text, metadata = _task_card_parts(fresh, lang)
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
    copy = COPY.get(lang, COPY["en"])
    today = date.today()
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
                "due_offset_days": (next_march(LAUNCH_DAY, today) - today).days,
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
                due_date=next_march(spec["due_day"], today),
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
            _post_card(tasks[spec["key"]], channel, lang)

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
    return project
