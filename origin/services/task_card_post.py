"""The project chat's task and milestone cards: the one place that posts and refreshes them.

Every task and milestone has one card in its project's PM channel, built here from the row as
stored, whatever wrote it (MIP-2483):

  * **posted** when the task or milestone is created: by the app's own create endpoints (the create
    form's finalizing `PUT /task/`, the quick add's `POST /task/`, `POST /milestone/`), by the
    agent's tools, which MCP serves too (MIP-1450), and by the public REST API (MIP-1462). These
    call it by name; a model signal would also fire for imports, seeds and scaffold rows. A browser
    that still sends its own card afterwards gets the existing one back (`message_views`), so there
    is never a second;
  * **refreshed** after every write that changes something the card shows, from any path
    (`schedule_refresh`, called by the task signals), so a browser that closes early cannot leave it
    stale. `PATCH card-message`, which the app sends after an edit, rebuilds it here too;
  * **made whole** when a comment arrives on a task that has none (`header_parts`): the comment
    needs a message to thread under, and it used to get a bare title.

Posting goes through `message_views._allocate_seq_and_create_message_with_activities`, the
function the REST send uses, so the card is the same row with the same fan-out as a message: the
per-channel `seq` lock, the `MessageMention` rows (the `@` of the assignee is what tells them), the
mention activity, the Web Push, the outbound `message.created` webhook. The activities are then
announced to the recipients' open apps (`realtime_push`), which the socket handler does for a
message sent from the app. A sidebar row for a task assigned to its own maker (`self_assign`) is
added only for the app's create forms, so a person who clicked Create has a trace; a quick add, an
agent or an API key adds none (MIP-1432's single notice per call already tells them).

The card is also announced to the project's chat room (`realtime_push.push_message`, MIP-1459),
so a colleague with that chat already open sees it at once.

The card's field names are in the language of its sender, the person who made the task
(`task_card.card_language`: their UI language, English when unknown), for the first post and for
every refresh (MIP-1744). The card is ONE stored message for the whole channel with its labels
baked in as text, so it cannot be translated per viewer; the sender's language is stable (a
colleague editing the task does not flip it).

Best effort, like every other observer of a write: the task is already saved, so a failure here is
logged and costs the card, never the write. Idempotent per task: a task that already has a card is
left alone.
"""

from __future__ import annotations

import logging

from django.db import transaction

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.common.user_models import CustomUser
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services import inbox_via, realtime_push, task_card
from origin.services.activity_suppression import activity_suppressed
from origin.services.project_statuses import default_status_names, status_label

log = logging.getLogger(__name__)


def _pm_channel(project_id) -> Channel | None:
    """The project's PM channel, found the way the task-comment mirror finds it, so a
    card and the comments threaded under it can never end up in different channels."""
    return Channel.objects.filter(kind=ChannelKind.PM, legacy_chat_id=int(project_id)).first()


def _post(*, project, task_id, actor_id, body, body_text, metadata, self_assign) -> Message | None:
    from origin.views.chat.message_views import (  # noqa: PLC0415
        _allocate_seq_and_create_message_with_activities,
    )

    channel = _pm_channel(project.project_id)
    if channel is None:
        log.warning("task card: project %s has no PM channel", project.project_id)
        return None
    # A card exists already (a retry, or a comment's mirror that made the header first):
    # one card per task, as the app keeps it.
    if Message.objects.filter(channel=channel, task_id=task_id, is_thread_reply=False).exists():
        return None
    actor = CustomUser.objects.filter(id=actor_id).first()
    if actor is None:
        return None
    message, activities = _allocate_seq_and_create_message_with_activities(
        channel=channel,
        sender=actor,
        body=body,
        body_text=body_text,
        parent_id=None,
        metadata=metadata,
        self_assign=self_assign,
        # Written through a connected app: its kind and family, for the icon (MIP-1570).
        via=inbox_via.connected_app_via(),
    )
    # The socket handler announces the message to the chat room and each activity to its
    # recipient when the app sends a message; here there is no handler, so tell the open apps
    # ourselves: the card to everyone with the chat open, the activity to the person it is for.
    realtime_push.push_message(message, realtime_push.CREATED)
    for activity in activities:
        realtime_push.push_activity(activity)
    return message


def _language_of(user_id) -> str:
    """The card language of the person with this id (English when there is none)."""
    return task_card.card_language(CustomUser.objects.filter(id=user_id).first())


def _system_user_id(project) -> str | None:
    return str(project.project_system_user_id) if project.project_system_user_id else None


def _task_card_parts(
    task, language: str | None = None, *, status_names: dict | None = None
) -> tuple[list, str, dict]:
    """`(body, body_text, metadata)` of `task`'s card, built from the row as stored, with its
    field names in `language` (English when none)."""
    body = task_card.task_card_body(
        labels=task_card.card_labels(language),
        title=task.title,
        status=task.status or "Open",
        status_label=status_label(task, task.status or "Open", defaults=status_names),
        priority=task.priority,
        assignee=task.assignee,
        reporter=task.reporter,
        due_date=task.due_date,
        team_id=task.team_id,
        effort=task.effort_level or None,
    )
    metadata = {
        "taskId": task.task_id,
        "displayId": task.display_id,
        "taskStatus": task.status,
        "systemUserId": _system_user_id(task.project),
    }
    # The app sends the bare title as the preview text, not the emoji-prefixed heading.
    return body, task.title, metadata


def _milestone_card_parts(milestone, language: str | None = None) -> tuple[list, str, dict]:
    """`(body, body_text, metadata)` of a milestone's card, built from the rows as stored, with its
    field names in `language` (English when none)."""
    assignees = [
        row.user
        for row in MilestoneAssignees.objects.filter(milestone=milestone)
        .select_related("user")
        .order_by("ts_created_at", "pk")
    ]
    # A deleted milestone's own row keeps its last status; its card says what happened to it.
    status = "Deleted" if milestone.is_deleted else milestone.status
    body = task_card.milestone_card_body(
        labels=task_card.card_labels(language),
        title=milestone.title,
        status=status,
        status_label=status_label(milestone, status or "Open"),
        priority=milestone.priority,
        sprint_name=milestone.sprint.name if milestone.sprint_id else None,
        reporter=milestone.reporter,
        assignees=assignees,
        due_date=milestone.due_date,
        team_id=milestone.team_id,
    )
    metadata = {
        "taskId": milestone.task_id,
        "taskStatus": status,
        "systemUserId": _system_user_id(milestone.project),
    }
    return body, milestone.title, metadata


def _fresh_task(task_id):
    return (
        TaskMaster.objects.select_related("project", "assignee", "reporter")
        .filter(task_id=task_id)
        .first()
    )


def _fresh_milestone(milestone_id=None, *, task_id=None):
    qs = MilestoneMaster.objects.select_related("project", "sprint", "reporter")
    if milestone_id is not None:
        return qs.filter(milestone_id=milestone_id).first()
    # The live one, else the newest: a deleted milestone's backing task still has its card.
    return qs.filter(task_id=task_id).order_by("is_deleted", "-milestone_id").first()


def header_parts(task_id, *, language_of) -> tuple[list, str, dict] | None:
    """`(body, body_text, metadata)` of the card of the task (or milestone) behind `task_id`, in
    the language of the person `language_of` (the card's sender). None when there is no such task.
    For the writers that make the message themselves (the comment mirror, the backfill)."""
    task = _fresh_task(task_id)
    if task is None or task.project_id is None:
        return None
    language = _language_of(language_of)
    if task.is_milestone:
        milestone = _fresh_milestone(task_id=task_id)
        if milestone is not None:
            return _milestone_card_parts(milestone, language)
    return _task_card_parts(task, language)


def is_title_only(body) -> bool:
    """The bare header the comment mirror used to make: a heading and nothing else."""
    return isinstance(body, list) and len(body) <= 1


def post_task_created_card(task, *, actor_id, self_assign: bool = False) -> Message | None:
    """Post `task`'s card into its project's PM channel, as the person who made it.

    `task` is a saved `TaskMaster`; `actor_id` is who made it (the card's sender, and the
    one person the activity fan-out leaves out). A task with no project has no chat to post
    to. `self_assign` adds the sidebar row for a task the maker gave themselves (the app's create
    form). Never raises.

    The row is read again rather than trusted: a tool's instance still holds what the caller
    typed (`due_date` is the raw string `2026-6-30`, not a date), and the card must show what
    the row stores.
    """
    try:
        task = _fresh_task(task.task_id)
        if task is None or task.project_id is None:
            return None
        body, body_text, metadata = _task_card_parts(task, _language_of(actor_id))
        return _post(
            project=task.project,
            task_id=task.task_id,
            actor_id=actor_id,
            body=body,
            body_text=body_text,
            metadata=metadata,
            self_assign=self_assign,
        )
    except Exception:  # noqa: BLE001 — the task is saved; an observer must not fail the write
        log.warning("task card post failed task=%s", getattr(task, "task_id", None), exc_info=True)
        return None


def post_milestone_created_card(
    milestone, *, actor_id, self_assign: bool = False
) -> Message | None:
    """Post a new milestone's card (sprint, status, priority, reporter, assignees, due).

    The card hangs off the milestone's backing task, as the app's does. Never raises.
    """
    try:
        milestone = _fresh_milestone(milestone.milestone_id)
        if milestone is None or milestone.task_id is None:
            return None
        body, body_text, metadata = _milestone_card_parts(milestone, _language_of(actor_id))
        return _post(
            project=milestone.project,
            task_id=milestone.task_id,
            actor_id=actor_id,
            body=body,
            body_text=body_text,
            metadata=metadata,
            self_assign=self_assign,
        )
    except Exception:  # noqa: BLE001 — the milestone is saved; an observer must not fail the write
        log.warning(
            "milestone card post failed milestone=%s",
            getattr(milestone, "milestone_id", None),
            exc_info=True,
        )
        return None


# --- keeping the card current (MIP-1460) ------------------------------------------------------


def _rewritten_card(card, build) -> tuple[list, str, dict] | None:
    """What `card` must be rewritten to, or None when it already shows what `build(language)`
    returns. `language` is the card's SENDER's, so a card keeps its language whoever edits the
    task (MIP-1744); an old English card of a Japanese sender is healed here."""
    body, body_text, metadata = build(task_card.card_language(card.sender))
    # Merged, not replaced: the browser's card may carry keys this does not know about.
    merged = {**(card.metadata or {}), **metadata}
    if card.body == body and card.body_text == body_text and (card.metadata or {}) == merged:
        return None
    return body, body_text, merged


def _refresh(project, task_id, build, *, announce: bool = True) -> Message | None:
    """Rewrite the card of `task_id` to what `build(language)` returns, when it differs from
    what is stored (`_rewritten_card`), and announce it unless the caller will.

    No card means nothing to refresh (a task that predates cards, or one nobody posted for):
    this never makes one. An unchanged card is not written, so a save that touched nothing the
    card shows costs a read and no broadcast.
    """
    from origin.views.chat.message_views import rewrite_card_message  # noqa: PLC0415

    channel = _pm_channel(project.project_id)
    if channel is None:
        return None
    card = (
        Message.objects.select_related("channel", "sender")
        .filter(channel=channel, task_id=task_id, is_thread_reply=False, deleted_at__isnull=True)
        .order_by("seq")
        .first()
    )
    if card is None:
        return None
    parts = _rewritten_card(card, build)
    if parts is None:
        return None
    body, body_text, merged = parts
    # `merged` is the stored row's own metadata (its stamp, if any, is the creation's) plus what the
    # server just built: nothing a client sent (MIP-1570).
    rewrite_card_message(card, body=body, body_text=body_text, metadata=merged, server_built=True)
    if announce:
        realtime_push.push_message(card, realtime_push.UPDATED)
    return card


def schedule_refresh(*, task_id=None, milestone_id=None) -> None:
    """After the current write commits, refresh the task's (or milestone's) card (MIP-1460).

    For every write, whoever made it, the app included (MIP-2483): leaving the app's edits to the
    browser left the card stale after any screen that sent no rewrite, and after a tab closed too
    soon. A card that already shows the row is not written, so a browser rewrite that follows costs
    a read. Nothing is scheduled inside `suppress_task_activity()` (bulk seeding and teardown).

    An observer, like the webhook: best effort, never fails the write.
    """
    try:
        if activity_suppressed():
            return

        def _run() -> None:
            if milestone_id is not None:
                refresh_milestone_card(milestone_id)
            else:
                refresh_task_card(task_id)

        transaction.on_commit(_run, robust=True)
    except Exception:  # noqa: BLE001 — never break a write
        log.warning("card refresh could not be scheduled", exc_info=True)


def refresh_task_card(task_id, *, announce: bool = True) -> Message | None:
    """Bring a task's chat card up to date with the row. A milestone's backing task refreshes
    the milestone's card instead. Never raises.
    """
    try:
        task = _fresh_task(task_id)
        if task is None or task.project_id is None or task.is_init_task:
            return None
        if task.is_milestone:
            milestone = _fresh_milestone(task_id=task_id)
            if milestone is None:
                return None
            return refresh_milestone_card(milestone.milestone_id, announce=announce)
        return _refresh(
            task.project,
            task.task_id,
            lambda lang: _task_card_parts(task, lang),
            announce=announce,
        )
    except Exception:  # noqa: BLE001 — the task is saved; an observer must not fail the write
        log.warning("task card refresh failed task=%s", task_id, exc_info=True)
        return None


def refresh_task_cards(task_ids) -> list:
    """`refresh_task_card` for many tasks, in a number of queries that does not grow with them
    (MIP-2561): a move's cards. Never raises.

    Announces nothing: returns the ids of the cards it rewrote, for the caller to push with the
    rest (`realtime_push.push_messages`). A milestone's card, and every card when the batch
    fails, is refreshed on its own.
    """
    task_ids = list(task_ids)
    try:
        rewritten, one_by_one = _refresh_batch(task_ids)
    except Exception:  # noqa: BLE001 — one bad card must not cost the others
        log.warning("task cards batch refresh failed tasks=%s", task_ids, exc_info=True)
        rewritten, one_by_one = [], task_ids
    for task_id in one_by_one:
        card = refresh_task_card(task_id, announce=False)
        if card is not None:
            rewritten.append(card.pk)
    return rewritten


def _refresh_batch(task_ids) -> tuple[list, list]:
    """`(ids of the cards rewritten, task ids to refresh one by one)`."""
    from origin.views.chat.message_views import rewrite_card_messages  # noqa: PLC0415

    found = {
        task.task_id: task
        for task in TaskMaster.objects.select_related(
            "project", "assignee", "reporter", "custom_status"
        ).filter(task_id__in=task_ids)
    }
    tasks, milestones = [], []
    for task_id in task_ids:
        task = found.get(task_id)
        if task is None or task.project_id is None or task.is_init_task:
            continue
        if task.is_milestone:
            milestones.append(task_id)
        else:
            tasks.append(task)
    if not tasks:
        return [], milestones

    project_ids = {task.project_id for task in tasks}
    channels: dict = {}
    for channel in Channel.objects.filter(
        kind=ChannelKind.PM, legacy_chat_id__in=project_ids
    ).order_by("pk"):
        channels.setdefault(channel.legacy_chat_id, channel)
    cards: dict = {}
    for card in (
        Message.objects.select_related("channel", "sender")
        .filter(
            channel__in=channels.values(),
            task_id__in=[task.task_id for task in tasks],
            is_thread_reply=False,
            deleted_at__isnull=True,
        )
        .order_by("seq")
    ):
        cards.setdefault((card.channel_id, card.task_id), card)
    status_names = default_status_names(project_ids)

    rewrites = []
    for task in tasks:
        channel = channels.get(task.project_id)
        card = cards.get((channel.pk, task.task_id)) if channel else None
        if card is None:
            continue
        try:
            parts = _rewritten_card(
                card,
                lambda lang: _task_card_parts(task, lang, status_names=status_names),
            )
        except Exception:  # noqa: BLE001 — as `refresh_task_card`: logged, costs this card only
            log.warning("task card refresh failed task=%s", task.task_id, exc_info=True)
            continue
        if parts is not None:
            rewrites.append((card, *parts))
    rewrite_card_messages(rewrites)
    return [card.pk for card, *_ in rewrites], milestones


def refresh_milestone_card(milestone_id, *, announce: bool = True) -> Message | None:
    """The same for a milestone's card (its status, sprint, assignees, dates). Never raises."""
    try:
        milestone = _fresh_milestone(milestone_id)
        if milestone is None or milestone.task_id is None:
            return None
        return _refresh(
            milestone.project,
            milestone.task_id,
            lambda lang: _milestone_card_parts(milestone, lang),
            announce=announce,
        )
    except Exception:  # noqa: BLE001 — the milestone is saved; an observer must not fail the write
        log.warning("milestone card refresh failed milestone=%s", milestone_id, exc_info=True)
        return None
