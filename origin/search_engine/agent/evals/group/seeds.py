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


SEEDS: dict[str, Callable[[dict[str, Any]], None]] = {
    "inbox_report": seed_inbox_report,
}


def apply(names: Any, info: dict[str, Any]) -> None:
    """Run the named seeds for the viewer `info` (from `sequence.viewer_info`)."""
    for name in names or ():
        if name not in SEEDS:
            raise SeedError(f"unknown seed {name!r}; known: {sorted(SEEDS)}")
        SEEDS[name](info)
