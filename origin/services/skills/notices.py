"""Tell the author of a skill when an operator or the system stops, hides or restores it (MIP-2192).

Before this the author found out only by opening the skill page: a held, hidden or taken-down skill
just stopped running for everybody else. Now each of the four events files ONE inbox item of type 0
(an activity notice; its body is BlockNote paragraphs, which the existing card renders, so no
frontend change) for the owner, in the skill's team, and goes through the existing push and email
path, so the person's own switches decide (`enable_inbox`, `email:inbox`, snooze). No new
notification category and no new screen.

PRIVACY. The notice says what happened and the sentence an operator or the check wrote for the
author (`moderation_reason`, which the author is shown on the skill page anyway). It never says who
reported the skill, how many did, or what they wrote: nothing about a reporter leaves the report row.

SAFE TO CALL INSIDE A TRANSACTION. The item is filed when the surrounding transaction commits, and a
failure here (a missing owner, the database) is logged with ids only and never undoes the decision
that caused it. Official skills (shipped by Genos) have no author to tell.
"""

from __future__ import annotations

import logging

from django.db import transaction

from origin.models.common.inbox_models import InboxItems
from origin.models.skills.skill_models import Skill

logger = logging.getLogger(__name__)

HELD = "held"
HIDDEN = "hidden"
TAKEN_DOWN = "taken_down"
RESTORED = "restored"
EVENTS = (HELD, HIDDEN, TAKEN_DOWN, RESTORED)

# `item_optionals["kind"]`: what a client or a test reads to tell this item apart.
KIND = "skill_moderation"
ITEM_TYPE_NOTICE = 0
DEFAULT_LOCALE = "en"

# `title` is the push and email title and the card's first line; `what` says what happened, `next`
# what the author can do. `{slug}` is the skill's name as the author typed it (held to the slug rule),
# never its title. English and Japanese are the real pair; every other locale reads the English copy.
_COPY: dict[str, dict[str, dict[str, str]]] = {
    "en": {
        HELD: {
            "title": "Your skill /{slug} is waiting for a review",
            "what": "The automatic check flagged it, so only you can use it until an operator has looked.",
            "next": "Nothing is lost. You can keep editing it.",
        },
        HIDDEN: {
            "title": "Your skill /{slug} was hidden after reports",
            "what": "Several people reported it, so it is hidden until an operator has looked.",
            "next": "Nothing is lost. You can keep editing it.",
        },
        TAKEN_DOWN: {
            "title": "Your skill /{slug} was taken down",
            "what": "An operator stopped it for everybody else. You can still read and edit it.",
            "next": "You cannot share it again until an operator restores it.",
        },
        RESTORED: {
            "title": "Your skill /{slug} is back",
            "what": "An operator looked at it and put it back, so people can use it again.",
            "next": "",
        },
    },
    "ja": {
        HELD: {
            "title": "スキル /{slug} は確認待ちです",
            "what": "自動チェックで指摘があったため、運営が確認するまで利用できるのはあなただけです。",
            "next": "内容は失われません。編集は続けられます。",
        },
        HIDDEN: {
            "title": "スキル /{slug} は報告により非表示になりました",
            "what": "複数の方から報告があったため、運営が確認するまで非表示になっています。",
            "next": "内容は失われません。編集は続けられます。",
        },
        TAKEN_DOWN: {
            "title": "スキル /{slug} は公開停止になりました",
            "what": "運営がほかの人への提供を止めました。あなたは引き続き読んで編集できます。",
            "next": "運営が復元するまで、再び共有することはできません。",
        },
        RESTORED: {
            "title": "スキル /{slug} が復元されました",
            "what": "運営が確認して元に戻したため、再び利用できます。",
            "next": "",
        },
    },
}
_REASON_LABEL = {"en": "Reason: ", "ja": "理由: "}


def locale_for(user) -> str:
    language = (getattr(user, "language", None) or "").strip().lower().split("-")[0]
    return language if language in _COPY else DEFAULT_LOCALE


def _paragraph(text: str, *, bold: bool = False) -> dict:
    return {
        "type": "paragraph",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": [{"type": "text", "text": text, "styles": {"bold": True} if bold else {}}],
        "children": [],
    }


def compose(skill: Skill, event: str, user) -> tuple[str, list[dict]]:
    """`(title, item_body)` in the author's language."""
    locale = locale_for(user)
    copy = _COPY[locale][event]
    title = copy["title"].format(slug=skill.slug)
    body = [_paragraph(title, bold=True), _paragraph(copy["what"])]
    reason = (skill.moderation_reason or "").strip()
    # The reason of a restore is the operator's note to the author; of a hold or a takedown, why.
    if reason and event in (HELD, TAKEN_DOWN, RESTORED):
        body.append(_paragraph(f"{_REASON_LABEL[locale]}{reason}"))
    if copy["next"]:
        body.append(_paragraph(copy["next"]))
    return title, body


def notify_author(skill: Skill, event: str) -> None:
    """File the notice for `skill`'s owner when the current transaction commits. Never raises."""
    if event not in EVENTS or skill.is_official or skill.owner_id is None:
        return
    skill_id, owner_id, team_id = skill.pk, skill.owner_id, skill.team_id

    def _file() -> None:
        try:
            _file_now(skill_id, owner_id, team_id, event)
        except Exception:  # noqa: BLE001 - a notice never undoes a moderation decision
            logger.exception("skills: notice %s for skill=%s failed", event, skill_id)

    transaction.on_commit(_file)


def _file_now(skill_id, owner_id, team_id, event: str) -> None:
    from origin.models.common.user_models import CustomUser  # noqa: PLC0415
    from origin.services.webpush_dispatch import schedule_push_for_inbox_item  # noqa: PLC0415

    skill = Skill.objects.filter(pk=skill_id).first()  # what is true now, not at the call
    owner = CustomUser.objects.filter(pk=owner_id, is_active=True, is_deleted=False).first()
    if skill is None or owner is None:
        return
    title, body = compose(skill, event, owner)
    item = InboxItems.objects.create(
        team_id=team_id,
        sender=None,  # system-authored
        receiver=owner,
        item_type=ITEM_TYPE_NOTICE,
        item_body=body,
        item_optionals={"kind": KIND, "event": event, "skill_id": str(skill_id)},
        request_status="",  # an activity, not a request
    )
    schedule_push_for_inbox_item(item, title=title)
