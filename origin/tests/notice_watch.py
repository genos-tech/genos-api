"""Watch every way the app tells a person something while a block of code runs.

A person is told through a row in their feed (`Activity`, `InboxItems`), a chat message that
names them, an email (sent, or queued in `EmailNotificationEvent`), a Web Push, or an
outgoing webhook. `watching_notices()` records all of them, so a test can say that a write
told nobody, or exactly who it told, without naming the one channel it expects.

Pushes are caught by patching the dispatchers by name, which sees a dispatcher that imports
lazily (as `project_requests` does) and not one that was imported at module load. A push is
always preceded by a row, except `schedule_push_to_user`, which is patched for that reason.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from unittest import mock

from django.core import mail

from origin.models.chat.unified_models import Activity, Message, MessageMention
from origin.models.common.inbox_models import InboxItems
from origin.models.common.notification_models import EmailNotificationEvent
from origin.models.common.webhook_models import WebhookDelivery

CHANNELS = (Activity, InboxItems, Message, MessageMention, EmailNotificationEvent, WebhookDelivery)
PUSHES = (
    "origin.services.webpush_dispatch.schedule_push_for_inbox_item",
    "origin.services.webpush_dispatch.schedule_push_for_activities",
    "origin.services.webpush_dispatch.schedule_push_to_user",
    "origin.services.webpush_dispatch.dispatch_push_for_inbox_item",
    "origin.services.webpush_dispatch.dispatch_push_for_activities",
    "origin.services.webpush_sender.send_web_push",
)


@contextmanager
def watching_notices():
    """Yields a dict that, once the block has run, holds what was sent:
    `{"rows": {model name: new rows}, "emails": int, "pushes": {dispatcher: calls}}`.
    Each is empty (or 0) when nobody was told."""
    seen: dict = {}
    before = {model.__name__: model.objects.count() for model in CHANNELS}
    outbox = len(mail.outbox)
    with ExitStack() as stack:
        pushes = {name: stack.enter_context(mock.patch(name)) for name in PUSHES}
        yield seen
    after = {model.__name__: model.objects.count() for model in CHANNELS}
    seen["rows"] = {
        name: after[name] - before[name] for name in after if after[name] != before[name]
    }
    seen["emails"] = len(mail.outbox) - outbox
    seen["pushes"] = {
        name.rsplit(".", 1)[-1]: patched.call_count
        for name, patched in pushes.items()
        if patched.call_count
    }
