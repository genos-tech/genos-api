"""The PM-channel "task created" card: its BlockNote body, built server-side.

The v3 PM pane renders ONE top-level card `Message` per task (the composer is
disabled there; only task lifecycle writes post). `task_card_post` builds it here
for every create and refresh, whatever made the write (MIP-2483). The app's own
template (`taskMessageTemplate` / `milestoneMessageTemplate` in
`genos-frontend/src/features/tasks/utils/TaskMessageTemplate.ts`) has the same
shape; what it sends to a project chat is no longer stored.

The body mirrors the live shape byte-for-byte (no block ids, a heading without
`isToggleable`) so a server-made card cannot be told from a client-made one.
This module is the single definition: `demo_seeder` imports it for the cards it
seeds, so the demo and the live paths cannot drift apart.

Labels are English unless the caller passes a table. `task_card_post` passes the
table of the card sender's language (`card_language`, `card_labels`; MIP-1744):
the field names are baked into the stored message as text, so one language has to
be chosen when it is written.
"""

from __future__ import annotations

# The card field names. The same table the English demo content carries
# (`demo_content_en.CARD_LABELS`; a test pins the two together), and the same
# strings as the frontend's `tasks.messageTemplate`.
CARD_LABELS = {
    "status": "Status: ",
    "priority": "Priority: ",
    "effort": "Effort: ",
    "assignee": "Assignee: ",
    "reporter": "Reporter: ",
    "due": "Due: ",
    "unassigned": "Unassigned",
}

# What a card names a person whose account row is gone (`reporter` is SET_NULL when the user
# is hard-deleted). The same words `account_deletion` gives an erased account's username
# (`DELETED_USERNAME`; a test pins the two together), so a hard-deleted person reads like a
# soft-deleted one. Not in `CARD_LABELS`: that table is pinned to the demo content's.
DELETED_USER = "Deleted user"

# A milestone card names a sprint and takes any number of assignees.
MILESTONE_CARD_LABELS = {
    **CARD_LABELS,
    "sprint": "Sprint: ",
    "assignees": "Assignees: ",
}

# The field names per UI language, for a card the SERVER writes (MIP-1744). The words are the
# ones `genos-frontend/src/i18n/locales/<lang>` writes on the app's own card
# (`tasks.messageTemplate`), so a server-made card reads like an app-made one. `en` is the table
# above, pinned to the English demo content (a test). The Japanese demo content
# (`demo_content_ja.CARD_LABELS`) still spells a few labels the older way (ASCII colon, 労力): it
# is the v1 eval corpus (`test_demo_v1_golden`), so it is left alone here. The vocabulary is the
# UI's (`CustomUser.language`); a language with no table here reads English. Grow it by adding a
# language: nothing else needs touching.
_LABELS_BY_LANGUAGE = {
    "en": {**MILESTONE_CARD_LABELS, "deleted_user": DELETED_USER},
    "ja": {
        "status": "ステータス：",
        "priority": "優先度：",
        "effort": "Effortレベル：",
        "assignee": "担当者：",
        "reporter": "報告者：",
        "due": "締切：",
        "unassigned": "未割り当て",
        "sprint": "スプリント：",
        "assignees": "担当者：",
        "deleted_user": "削除済みユーザー",
    },
    "zh": {
        "status": "状态：",
        "priority": "优先级：",
        "effort": "工作量：",
        "assignee": "负责人：",
        "reporter": "报告人：",
        "due": "截止：",
        "unassigned": "未指派",
        "sprint": "冲刺：",
        "assignees": "负责人：",
        "deleted_user": "已删除用户",
    },
    "es": {
        "status": "Estado: ",
        "priority": "Prioridad: ",
        "effort": "Esfuerzo: ",
        "assignee": "Asignado: ",
        "reporter": "Reportador: ",
        "due": "Vence: ",
        "unassigned": "Sin asignar",
        "sprint": "Sprint: ",
        "assignees": "Asignados: ",
        "deleted_user": "Usuario eliminado",
    },
    "fr": {
        "status": "Statut : ",
        "priority": "Priorité : ",
        "effort": "Effort : ",
        "assignee": "Assigné : ",
        "reporter": "Rapporteur : ",
        "due": "Échéance : ",
        "unassigned": "Non assigné",
        "sprint": "Sprint : ",
        "assignees": "Assignés : ",
        "deleted_user": "Utilisateur supprimé",
    },
    "ar": {
        "status": "الحالة: ",
        "priority": "الأولوية: ",
        "effort": "الجهد: ",
        "assignee": "المُسنَد إليه: ",
        "reporter": "المُبلِّغ: ",
        "due": "الاستحقاق: ",
        "unassigned": "غير مُسنَدة",
        "sprint": "السبرنت: ",
        "assignees": "المُسنَد إليهم: ",
        "deleted_user": "مستخدم محذوف",
    },
    "hi": {
        "status": "स्थिति: ",
        "priority": "प्राथमिकता: ",
        "effort": "प्रयास: ",
        "assignee": "असाइनी: ",
        "reporter": "रिपोर्टर: ",
        "due": "ड्यू: ",
        "unassigned": "असाइन नहीं",
        "sprint": "स्प्रिंट: ",
        "assignees": "असाइनी: ",
        "deleted_user": "हटाया गया उपयोगकर्ता",
    },
}
CARD_LANGUAGES = tuple(_LABELS_BY_LANGUAGE)


def card_labels(language: str | None) -> dict[str, str]:
    """The field names (task and milestone) in `language`; English for an unknown one."""
    return _LABELS_BY_LANGUAGE.get(language or "", _LABELS_BY_LANGUAGE["en"])


def card_language(user) -> str:
    """The language a card is written in for `user`: their UI language, English when it is
    unknown (NULL), not one the cards have a table for, or when there is no user."""
    code = (getattr(user, "language", None) or "").strip().lower()
    return code if code in _LABELS_BY_LANGUAGE else "en"


# BlockNote ships a fixed `textColor` palette; these map the task meta values to
# the closest named slot, as the frontend template does.
STATUS_COLOR = {
    "Open": "blue",
    "WIP": "orange",
    "Pending": "purple",
    "Closed": "green",
    "Rejected": "brown",
    "Deleted": "red",
}
PRIORITY_COLOR = {
    "Minimal": "gray",
    "Low": "green",
    "Normal": "blue",
    "High": "orange",
    "Critical": "red",
}
EFFORT_COLOR = {
    "Minimal": "gray",
    "Low": "green",
    "Moderate": "blue",
    "High": "orange",
    "Extensive": "red",
}

# A level-3 heading WITHOUT `isToggleable` (seeded note headings carry it; card
# headings do not) and no per-block `id`.
CARD_HEADING_PROPS = {
    "level": 3,
    "textColor": "default",
    "textAlignment": "left",
    "backgroundColor": "default",
}
PARA_PROPS = {
    "textColor": "default",
    "textAlignment": "left",
    "backgroundColor": "default",
}


def card_label(text: str) -> dict:
    return {"text": text, "type": "text", "styles": {"bold": True}}


def card_chip(text: str, color: str | None) -> dict:
    return {"text": text, "type": "text", "styles": {"bold": True, "textColor": color or "gray"}}


def neutral_chip() -> dict:
    return {"text": "—", "type": "text", "styles": {"bold": True, "textColor": "gray"}}


def card_mention(user, team_id) -> dict:
    """Inline `mention` node matching the frontend `mentionNode` shape so
    the card's assignee / reporter rows render as click-through mentions."""
    return {
        "type": "mention",
        "props": {
            "avatarImgPath": [""],
            "customStatus": "N/A",
            "teamId": str(team_id),
            "teamName": "N/A",
            "userEmail": user.email,
            "userId": str(user.id),
            "userName": user.username,
        },
    }


def _person(user, team_id, labels: dict[str, str] | None = None) -> dict:
    """The mention for `user`, or, when the row is gone (a NULL reporter), the plain words
    `DELETED_USER` (in the card's language, when `labels` carries it) in the muted italic of an
    unassigned row. Only a missing person is handled: anything else that is not a user still
    raises."""
    if user is None:
        return {
            "text": (labels or {}).get("deleted_user", DELETED_USER),
            "type": "text",
            "styles": {"italic": True, "textColor": "gray"},
        }
    return card_mention(user, team_id)


def _heading(prefix: str, title: str) -> dict:
    return {
        "type": "heading",
        "props": CARD_HEADING_PROPS,
        "content": [{"text": f"{prefix} {title}", "type": "text", "styles": {}}],
        "children": [],
    }


def _row(*content: dict) -> dict:
    return {"type": "paragraph", "props": PARA_PROPS, "content": list(content), "children": []}


def _unassigned(labels: dict[str, str]) -> dict:
    return {
        "text": labels["unassigned"],
        "type": "text",
        "styles": {"italic": True, "textColor": "gray"},
    }


def _due_row(labels: dict[str, str], due_date) -> dict:
    due_str = due_date.isoformat() if due_date else "—"
    return _row(card_label(labels["due"]), {"text": f"📅 {due_str}", "type": "text", "styles": {}})


def _blank_row() -> dict:
    return {"type": "paragraph", "props": PARA_PROPS, "content": [], "children": []}


def task_card_body(
    *,
    title: str,
    status: str,
    priority: str | None,
    assignee,
    reporter,
    due_date,
    team_id,
    is_milestone: bool = False,
    labels: dict[str, str] | None = None,
    effort: str | None = None,
    status_label: str | None = None,
) -> list:
    """Build the PM task-card header body.

    A `priority` or `effort` that is unset (a task made over MCP often has neither;
    the demo never seeds an effort) shows the neutral em-dash chip, as an unset value
    does in the app.

    `labels` supplies the field names. It is optional, and defaults to English,
    because `starter_seeder` reaches this builder too: real customer workspaces
    are not part of the bilingual demo, and their behaviour must not change.
    The *values* beside the labels stay as they are: `status` and `priority`
    are DB enum strings, so a chip reading `Open` is correct in either language.
    `status_label` is what the project calls this status (MIP-2493): the chip says it,
    and keeps the colour of `status`, its category.
    """
    labels = labels if labels is not None else CARD_LABELS
    return [
        _heading("🚩" if is_milestone else "🧾", title),
        _row(
            card_label(labels["status"]),
            card_chip(status_label or status, STATUS_COLOR.get(status)),
        ),
        _row(
            card_label(labels["priority"]),
            card_chip(priority, PRIORITY_COLOR.get(priority)) if priority else neutral_chip(),
        ),
        _row(
            card_label(labels["effort"]),
            card_chip(effort, EFFORT_COLOR.get(effort)) if effort else neutral_chip(),
        ),
        _row(
            card_label(labels["assignee"]),
            card_mention(assignee, team_id) if assignee is not None else _unassigned(labels),
        ),
        _row(card_label(labels["reporter"]), _person(reporter, team_id, labels)),
        _due_row(labels, due_date),
        _blank_row(),
    ]


def milestone_card_body(
    *,
    title: str,
    status: str,
    priority: str | None,
    sprint_name: str | None,
    reporter,
    assignees,
    due_date,
    team_id,
    labels: dict[str, str] | None = None,
    status_label: str | None = None,
) -> list:
    """Build the PM milestone-card header body (`milestoneMessageTemplate`).

    A milestone takes any number of assignees and a sprint, and has no Effort
    row (the milestone form does not expose one).
    """
    labels = labels if labels is not None else MILESTONE_CARD_LABELS
    # Milestones default to Open at create time; an empty value reads as that.
    status = status or "Open"
    assignee_row = [card_label(labels["assignees"])]
    if not assignees:
        assignee_row.append(_unassigned(labels))
    for index, user in enumerate(assignees):
        if index:
            # A comma between mention nodes: it cannot live inside one, and the
            # chat renderer keeps each mention clickable this way.
            assignee_row.append({"text": ", ", "type": "text", "styles": {}})
        assignee_row.append(card_mention(user, team_id))
    return [
        _heading("🚩", title),
        _row(card_label(labels["sprint"]), card_chip(sprint_name or "—", "blue")),
        _row(
            card_label(labels["status"]),
            card_chip(status_label or status, STATUS_COLOR.get(status)),
        ),
        _row(
            card_label(labels["priority"]),
            card_chip(priority, PRIORITY_COLOR.get(priority)) if priority else neutral_chip(),
        ),
        _row(card_label(labels["reporter"]), _person(reporter, team_id, labels)),
        _row(*assignee_row),
        _due_row(labels, due_date),
        _blank_row(),
    ]


_UNASSIGNED_WORDS = frozenset(table["unassigned"] for table in _LABELS_BY_LANGUAGE.values())


def card_values(body) -> list[list[str]]:
    """What a card SAYS, block by block, with its field names and wording language left out: the
    heading, each chip, each mentioned person and each date. Two cards of the same row read the
    same here whoever wrote them (the app's browser, in its viewer's language, or this module)."""
    rows = []
    for block in body if isinstance(body, list) else []:
        values = []
        content = block.get("content") if isinstance(block, dict) else None
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "mention":
                values.append(f"user:{(item.get('props') or {}).get('userId')}")
                continue
            styles = item.get("styles") or {}
            if styles == {"bold": True}:  # a field name
                continue
            text = str(item.get("text", "")).strip()
            # "Unassigned" and "Deleted user" in whatever language. The server writes them in
            # muted italic; the app's own task card writes "Unassigned" in plain text.
            if styles.get("italic") or (not styles and text in _UNASSIGNED_WORDS):
                values.append("none")
            elif text and text != ",":
                values.append(text)
        if values:
            rows.append(values)
    return rows
