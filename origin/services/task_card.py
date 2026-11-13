"""The PM-channel "task created" card: its BlockNote body, built server-side.

The v3 PM pane renders ONE top-level card `Message` per task (the composer is
disabled there; only task lifecycle writes post). The app builds it in the
browser (`taskMessageTemplate` / `milestoneMessageTemplate` in
`genos-frontend/src/features/tasks/utils/TaskMessageTemplate.ts`) and sends it
after the task PUT. A task made any other way (MCP, the in-app agent, the
public REST API) has no browser to do that, so `task_card_post` builds the same
card here.

The body mirrors the live shape byte-for-byte (no block ids, a heading without
`isToggleable`) so a server-made card cannot be told from a client-made one.
This module is the single definition: `demo_seeder` imports it for the cards it
seeds, so the demo and the live paths cannot drift apart.

Labels are English unless the caller passes a table. The app rewrites the card
in the viewer's language the next time the task's metadata is saved
(`PATCH /api/v3/tasks/{id}/card-message/`), so a card made here is English
until then.
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

# A milestone card names a sprint and takes any number of assignees.
MILESTONE_CARD_LABELS = {
    **CARD_LABELS,
    "sprint": "Sprint: ",
    "assignees": "Assignees: ",
}

# BlockNote ships a fixed `textColor` palette; these map the task meta values to
# the closest named slot, as the frontend template does.
STATUS_COLOR = {
    "Open": "blue",
    "WIP": "orange",
    "Pending": "purple",
    "Closed": "green",
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
    """
    labels = labels if labels is not None else CARD_LABELS
    return [
        _heading("🚩" if is_milestone else "🧾", title),
        _row(card_label(labels["status"]), card_chip(status, STATUS_COLOR.get(status))),
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
        _row(card_label(labels["reporter"]), card_mention(reporter, team_id)),
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
        _row(card_label(labels["status"]), card_chip(status, STATUS_COLOR.get(status))),
        _row(
            card_label(labels["priority"]),
            card_chip(priority, PRIORITY_COLOR.get(priority)) if priority else neutral_chip(),
        ),
        _row(card_label(labels["reporter"]), card_mention(reporter, team_id)),
        _row(*assignee_row),
        _due_row(labels, due_date),
        _blank_row(),
    ]
