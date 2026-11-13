"""Extra rows an eval-group case needs on top of the sequence scenario (`seed: [name]` in a case).

The sequence teams are built once by `agent_eval_setup --scenario sequence`. Some items of the
tools stack need a row that story does not have: a long scheduled report in a member's inbox
(MIP-1160), a team with a name (MIP-1090). Each is a SEED here: a function `(info) -> None` that
adds that row to the viewer's own sequence team and is IDEMPOTENT (looks for its own marker first,
so a case run twice, or on every arm, finds it and writes nothing).

Rules (the same as the scenario's): only the sequence teams, never the v1 corpus team; every row
carries a marker (`eval_seed` in `item_optionals`/`metadata`) so it can be told from the story's;
the seed adds, never edits or deletes the story. A seed is applied in `case_runner.prepare_case`
from the case's `seed:` list, under the same lock as the sequence build.

SEEDS never write a model-visible FLAG: they put data in the database, the same data on every arm.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

SEED_KEY = "eval_seed"


class SeedError(Exception):
    pass


# --------------------------------------------------------------------------- #
# t1160: a scheduled report much longer than the tool's cap                    #
# --------------------------------------------------------------------------- #

_REPORT_TEXT = {
    "en": {
        "title": "Weekly work report",
        "summary": "Three things are overdue and twelve are due this week.",
        "overdue": "Overdue",
        "this_week": "Due this week",
        "waiting": "Waiting on others",
        "blocked": "Blocked",
        "review": "In review",
        "closed": "Recently closed",
        "mentions": "Mentions",
        "chat": "Unread chat",
        "calendar": "Calendar",
        "notes": "Notes edited",
        "backlog": "Backlog",
        "archived": "Archived",
        "overdue_1": "CLP-9 Fix the export button is overdue by 3 days",
        "line": "{p}-{n} {topic}: waiting for a decision, last touched {n} days ago, owner is on leave",
        "last": "ZULU-77 Legacy cleanup of the export queue, parked since last spring",
    },
    "ja": {
        "title": "週次の作業レポート",
        "summary": "期限切れが3件、今週期限が12件あります。",
        "overdue": "期限切れ",
        "this_week": "今週期限",
        "waiting": "他の人待ち",
        "blocked": "ブロック中",
        "review": "レビュー中",
        "closed": "最近クローズ",
        "mentions": "メンション",
        "chat": "未読チャット",
        "calendar": "カレンダー",
        "notes": "編集したノート",
        "backlog": "バックログ",
        "archived": "アーカイブ",
        "overdue_1": "CLP-9 エクスポートボタンの修正が3日遅れています",
        "line": "{p}-{n} {topic}: 判断待ち、最後の更新は{n}日前、担当者は休暇中です",
        "last": "ZULU-77 エクスポートキューの旧処理の整理、昨春から保留中です",
    },
}

#: (key, items in the section): about 130 lines, ~14,000 characters: far over the 5,000 cap
_REPORT_SECTIONS = (
    ("overdue", 3),
    ("this_week", 8),
    ("waiting", 8),
    ("blocked", 6),
    ("review", 8),
    ("closed", 12),
    ("mentions", 10),
    ("chat", 10),
    ("calendar", 8),
    ("notes", 10),
    ("backlog", 25),
    ("archived", 25),
)


def report_for(lang: str) -> dict[str, Any]:
    """The report as a job hands it to `schedule_delivery.clean_report`."""
    t = _REPORT_TEXT[lang]
    sections = []
    n = 0
    for key, count in _REPORT_SECTIONS:
        items = []
        for i in range(count):
            n += 1
            if key == "overdue" and i == 0:
                text = t["overdue_1"]
            elif key == "archived" and i == count - 1:
                text = t["last"]
            else:
                text = t["line"].format(p="WRK", n=n, topic=t[key])
            items.append({"text": text, "link": f"/workspace/tasks/WRK-{n}"})
        sections.append({"heading": t[key], "items": items})
    return {"title": t["title"], "summary": t["summary"], "sections": sections}


def seed_inbox_report(info: dict[str, Any]) -> None:
    from origin.models.common.inbox_models import (  # noqa: PLC0415
        ITEM_TYPE_GENOS_REPORT,
        InboxItems,
    )
    from origin.services.schedule_delivery import clean_report  # noqa: PLC0415

    marker = {SEED_KEY: "t1160"}
    if InboxItems.objects.filter(
        team_id=info["team_id"],
        receiver_id=info["user_id"],
        item_type=ITEM_TYPE_GENOS_REPORT,
        item_optionals__contains=marker,
    ).exists():
        return
    lang = "ja" if info["team"] == "ja" else "en"
    body = clean_report(report_for(lang), _REPORT_TEXT[lang]["title"])
    InboxItems.objects.create(
        team_id=info["team_id"],
        receiver_id=info["user_id"],
        sender=None,
        item_type=ITEM_TYPE_GENOS_REPORT,
        item_body=body,
        item_optionals={"kind": "report", "job": "eval", **marker},
        is_read=False,
        request_status="",
    )


# --------------------------------------------------------------------------- #
# t1090-client: a closed task whose changes came from different clients        #
# --------------------------------------------------------------------------- #

#: The seeded task's title per language (also its idempotency marker).
CLIENT_TASK_TITLE = {
    "en": "Refresh the onboarding checklist",
    "ja": "オンボーディングのチェックリストを更新する",
}
#: The names the cases ask about. The viewer owns LAPTOP_KEY; the developer owns CI_KEY.
LAPTOP_KEY = "Laptop script"
CI_KEY = "Chris CI"
CONNECTED_APP = "Claude"


def seed_task_history_clients(info: dict[str, Any]) -> None:
    """A CLOSED task (so it is on nobody's open list) in the viewer's project, whose history
    has one change from each kind of client:

      1. created                    via the web app
      2. Open -> WIP                via the VIEWER's own connected app ("Claude")
      3. priority Normal -> High    via the VIEWER's own API key ("Laptop script")
      4. due date                   via ANOTHER member's API key ("Chris CI", the developer's)
      5. WIP -> Closed              via the web app

    Rows are written directly (a connected app and a key cannot be exercised here without a
    live OAuth flow), with the `via` shape `middleware.current_user.get_current_source` stamps."""
    from datetime import datetime, timedelta  # noqa: PLC0415

    from django.db.models import Max  # noqa: PLC0415
    from oauth2_provider.models import Application  # noqa: PLC0415

    from origin.models.common.api_key_models import (  # noqa: PLC0415
        SCOPE_WRITE,
        ApiKey,
        generate_key,
        hash_key,
    )
    from origin.models.common.mcp_connection_models import McpConnection  # noqa: PLC0415
    from origin.models.project.prj_models import ProjectMaster  # noqa: PLC0415
    from origin.models.task.task_activity_models import (  # noqa: PLC0415
        TaskActivity,
        TaskActivityActionType,
    )
    from origin.models.task.task_models import TaskMaster  # noqa: PLC0415
    from origin.search_engine.agent.evals.group import sequence  # noqa: PLC0415

    lang = "ja" if info["team"] == "ja" else "en"
    title = CLIENT_TASK_TITLE[lang]
    team_id = info["team_id"]
    if TaskMaster.objects.filter(team_id=team_id, title=title, is_deleted=False).exists():
        return
    project = ProjectMaster.objects.filter(team_id=team_id).order_by("project_id").first()
    if project is None:
        raise SeedError("the sequence team has no project")
    me = info["user_id"]
    # (`dev_user_id` in `info` is for a test whose people have random ids)
    dev = str(info.get("dev_user_id") or sequence.user_id(info["team"], "c"))
    number = (
        TaskMaster.objects.filter(project=project).aggregate(m=Max("project_task_number"))["m"] or 0
    ) + 1
    task = TaskMaster.objects.create(
        team_id=team_id,
        project=project,
        reporter_id=me,
        assignee_id=me,
        title=title,
        status="Closed",
        priority="High",
        project_task_number=number,
    )
    TaskActivity.objects.filter(task=task).delete()  # what the signals wrote: not a client's

    def key(owner: str, name: str) -> ApiKey:
        return ApiKey.objects.create(
            user_id=owner,
            name=name,
            key_hash=hash_key(generate_key()),
            prefix="gk_evalseed",
            scope=SCOPE_WRITE,
        )

    mine, theirs = key(me, LAPTOP_KEY), key(dev, CI_KEY)
    application = Application.objects.create(
        client_id=f"eval-seed-{info['team']}-claude",
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://claude.ai/cb",
        name=CONNECTED_APP,
    )
    connection = McpConnection.objects.create(
        user_id=me,
        application=application,
        default_team_id=team_id,
        team_ids=[str(team_id)],
        scope="write",
    )
    app = {"kind": "app"}
    connected = {
        "kind": "oauth",
        "connection_id": str(connection.id),
        "client_name": CONNECTED_APP,
    }

    def api_key(k: ApiKey) -> dict[str, Any]:
        return {"kind": "api_key", "key_id": str(k.id), "key_name": k.name, "key_prefix": k.prefix}

    steps = (
        (TaskActivityActionType.CREATED, "created", None, {"title": title}, me, app),
        (TaskActivityActionType.STATUS, "status", "Open", "WIP", me, connected),
        (TaskActivityActionType.PRIORITY, "priority", "Normal", "High", me, api_key(mine)),
        (TaskActivityActionType.DUE_DATE, "due_date", None, "2026-11-20", dev, api_key(theirs)),
        (TaskActivityActionType.STATUS, "status", "WIP", "Closed", me, app),
    )
    day = datetime.combine(sequence.SCENARIO_DAY, datetime.min.time(), tzinfo=sequence._jst())
    for n, (action, field, old, new, actor, via) in enumerate(steps):
        row = TaskActivity.objects.create(
            task=task,
            team_id=team_id,
            project=project,
            actor_id=actor,
            action_type=action,
            field_name=field,
            old_value=old,
            new_value=new,
            metadata={"via": via, SEED_KEY: "t1090-client"},
        )
        TaskActivity.objects.filter(pk=row.pk).update(
            ts_created_at=day + timedelta(hours=18, minutes=n)
        )


SEEDS: dict[str, Callable[[dict[str, Any]], None]] = {
    "inbox_report": seed_inbox_report,
    "task_history_clients": seed_task_history_clients,
}


def apply(names: Any, info: dict[str, Any]) -> None:
    """Run the named seeds for the viewer `info` (from `sequence.viewer_info`)."""
    for name in names or ():
        if name not in SEEDS:
            raise SeedError(f"unknown seed {name!r}; known: {sorted(SEEDS)}")
        SEEDS[name](info)
