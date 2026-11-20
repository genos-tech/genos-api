"""The two notices before the first month of Core ends: 5 days before, and 1 day before (MIP-1764).

Without them day 30 is a surprise: Core's limits return to Free's and a connected app stops, and
the end date is shown only to someone who opens Settings. The trial itself is
`services/first_month_trial`; this reads its `CoreTrial.ends_at` and says so, twice.

WHERE IT RUNS. A pass in `message_reminder_tick` (the existing every-minute cron, `reminder-tick`),
run after the reminders. No new Railway service: a service made by `config apply` starts with no
variables and never runs (a silent failure), while this tick already has the database and the web
push keys. The pass is two indexed queries (one per stage, each on its own partial index, `coretrial_due_5d` /
`coretrial_due_1d`, which hold only the rows whose stage is still to do) and is empty almost always.

HOW IT FIRES ONCE. `notified_5d_at` / `notified_1d_at` on the trial row. The stamp is also the claim:
`UPDATE ... WHERE stamp IS NULL` inside the same transaction that files the inbox item, so two
overlapping passes cannot both deliver (the second updates 0 rows) and a pass that crashes rolls
the stamp back with the item and redelivers on the next one. Same shape as `todo_reminders.fire`.

WHO GETS ONE. Not a person it no longer helps: an account that is inactive or deleted, or whose
plan is already paid (`resolve_effective_tier` carries no `trial_ends_at` when the trial is not
what gives the plan: a payer, a paying team's member, or a trial that has ended). That stage is
stamped as handled without a notice, so the row is not looked at again; the OTHER stage is judged at
its own moment, so someone who leaves a paying team before day 29 is still told then. A trial that
is within a day of its end when the tick first sees it (the cron was down, or it began late) gets
only the 1-day notice, and the 5-day one is stamped as superseded.

THE OFFER'S SWITCH IS NOT CONSULTED. `FIRST_MONTH_CORE_TRIAL` decides whether a NEW trial starts;
a trial that is running is announced even after the offer is switched off.

WHAT THE PERSON GETS. An inbox item of type 0 (an activity notice; no frontend change: its body is
BlockNote paragraphs, which the existing card renders, with the plans link as a link mark), filed
in EACH team the person is an active member of (the inbox is read per team, and a trial belongs to
a person, so one row in one team could sit where they never look), one push and one email for the
first of them. Both go through the existing inbox path, so the person's own switches decide
(`enable_inbox`, `email:inbox`, snooze, `EMAIL_NOTIFICATIONS_ENABLED`); no new notification
category exists, so no settings screen needs a new row. A person with no active team has no inbox
to put it in and is skipped. The wording is written here in the person's own language
(`CustomUser.language`, English when unknown), because a server-filed item has no client to compose
it: see `_COPY`.

Never logs an address or a name: ids and counts only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMembers
from origin.models.common.trial_models import CoreTrial
from origin.services.user_time import resolve_zone

logger = logging.getLogger(__name__)

STAGE_5D = "5d"
STAGE_1D = "1d"
WINDOW = {STAGE_5D: timedelta(days=5), STAGE_1D: timedelta(days=1)}
DAYS = {STAGE_5D: 5, STAGE_1D: 1}

# `item_optionals["kind"]` of the inbox item: what a client (or a test) reads to tell it apart.
KIND = "core_trial_notice"
ITEM_TYPE_NOTICE = 0
PLANS_PATH = "/workspace/plans"
DEFAULT_LIMIT = 500
DEFAULT_LOCALE = "en"

# Why a stage was handled without a notice. Strings: the log line and the tests read them.
SKIP_RACE = "claimed_elsewhere"
SKIP_INACTIVE = "inactive_account"
SKIP_NOT_HELPED = "plan_already_paid_or_ended"
SKIP_NO_TEAM = "no_team"
SKIP_SUPERSEDED = "superseded_by_1d"
SKIP_ALREADY_SENT = "already_sent"

# ---------------------------------------------------------------------------------------------
# Wording. Every string below is a DRAFT for Ken: TODO(copy). English and Japanese are the real
# pair; the other five locales are plain drafts that reuse the Plan & Usage wording the frontend
# already ships for the same trial (`settings.planUsage.trialEnds` / `trialAfter`), so the two
# surfaces say the same thing. One entry per frontend locale (`CustomUser.language`).
#
#   title_5d / title_1d : the push and email title, and the card's first line
#   ends                : "Your free month of Core ends on {date}."
#   after               : what happens at the end (Free plan, data stays, connected apps pause)
#   plans               : the link text to the plans page
#   date                : how a date is written ("{y}", "{m}", "{d}", "{month}")
# ---------------------------------------------------------------------------------------------
_EN_MONTHS = (
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
_ISO = "{y}-{m:02d}-{d:02d}"

_COPY: dict[str, dict[str, str]] = {
    "en": {  # TODO(copy)
        "title_5d": "Your free month of Core ends in 5 days",
        "title_1d": "Your free month of Core ends in 1 day",
        "ends": "Your free month of Core ends on {date}.",
        "after": (
            "After that your account returns to the Free plan. Your data stays, and connected "
            "apps pause until you are on a paid plan again."
        ),
        "plans": "See plans",
        "date": "{month} {d}, {y}",
    },
    "ja": {  # TODO(copy)
        "title_5d": "Core の無料期間はあと5日で終わります",
        "title_1d": "Core の無料期間はあと1日で終わります",
        "ends": "Core の無料期間は {date} に終わります。",
        "after": (
            "その後はアカウントが無料プランに戻ります。データは残り、接続済みのアプリは"
            "有料プランに戻るまで一時停止します。"
        ),
        "plans": "プランを見る",
        "date": "{y}年{m}月{d}日",
    },
    "es": {  # TODO(copy)
        "title_5d": "Tu mes gratis de Core termina en 5 días",
        "title_1d": "Tu mes gratis de Core termina en 1 día",
        "ends": "Tu mes gratis de Core termina el {date}.",
        "after": (
            "Después tu cuenta vuelve al plan Free. Tus datos se conservan y las apps conectadas "
            "se pausan hasta que vuelvas a un plan de pago."
        ),
        "plans": "Ver planes",
        "date": _ISO,
    },
    "fr": {  # TODO(copy)
        "title_5d": "Votre mois gratuit de Core se termine dans 5 jours",
        "title_1d": "Votre mois gratuit de Core se termine dans 1 jour",
        "ends": "Votre mois gratuit de Core se termine le {date}.",
        "after": (
            "Ensuite, votre compte repasse au forfait Free. Vos données sont conservées et les "
            "apps connectées sont mises en pause jusqu'à un retour sur un forfait payant."
        ),
        "plans": "Voir les forfaits",
        "date": _ISO,
    },
    "hi": {  # TODO(copy)
        "title_5d": "आपका Core का मुफ़्त महीना 5 दिन में समाप्त होगा",
        "title_1d": "आपका Core का मुफ़्त महीना 1 दिन में समाप्त होगा",
        "ends": "आपका Core का मुफ़्त महीना {date} को समाप्त होगा।",
        "after": (
            "उसके बाद आपका खाता Free प्लान पर लौट जाएगा। आपका डेटा बना रहता है, और कनेक्ट किए गए "
            "ऐप तब तक रुके रहते हैं जब तक आप फिर से सशुल्क प्लान पर नहीं आते।"
        ),
        "plans": "प्लान देखें",
        "date": _ISO,
    },
    "zh": {  # TODO(copy)
        "title_5d": "Core 的免费月将在 5 天后结束",
        "title_1d": "Core 的免费月将在 1 天后结束",
        "ends": "Core 的免费月将于 {date} 结束。",
        "after": (
            "之后你的账户将回到免费版。你的数据会保留，已连接的应用会暂停，直到你再次使用付费套餐。"
        ),
        "plans": "查看套餐",
        "date": "{y}年{m}月{d}日",
    },
    "ar": {  # TODO(copy)
        "title_5d": "ينتهي شهر Core المجاني بعد 5 أيام",
        "title_1d": "ينتهي شهر Core المجاني بعد يوم واحد",
        "ends": "ينتهي شهر Core المجاني في {date}.",
        "after": (
            "بعد ذلك يعود حسابك إلى الخطة المجانية. تبقى بياناتك، وتتوقف التطبيقات المتصلة "
            "مؤقتًا حتى تعود إلى خطة مدفوعة."
        ),
        "plans": "عرض الخطط",
        "date": _ISO,
    },
}

LOCALES = tuple(_COPY)


def locale_for(user) -> str:
    """The copy to use: the account's own `language` (a primary subtag), else English."""
    language = (getattr(user, "language", None) or "").strip().lower().split("-")[0]
    return language if language in _COPY else DEFAULT_LOCALE


def format_date(moment: datetime, user, locale: str) -> str:
    """The end date as a calendar day in the PERSON's zone (the UTC date is a day off for Tokyo)."""
    local = moment.astimezone(resolve_zone(getattr(user, "timezone", None)))
    return _COPY[locale]["date"].format(
        y=local.year, m=local.month, d=local.day, month=_EN_MONTHS[local.month - 1]
    )


def plans_url() -> str:
    """The plans page, absolute when the frontend's address is configured (a link in a pushed or
    emailed card has no page to be relative to), else the in-app path."""
    base = (getattr(settings, "FRONTEND_BASE_URL", "") or "").rstrip("/")
    return f"{base}{PLANS_PATH}"


def _paragraph(content: list[dict]) -> dict:
    return {
        "type": "paragraph",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": content,
        "children": [],
    }


def _text(text: str, **styles) -> dict:
    return {"type": "text", "text": text, "styles": styles}


def compose(stage: str, user, ends_at: datetime) -> tuple[str, list[dict], str]:
    """`(title, item_body, push_body)` for one notice, in the person's language."""
    locale = locale_for(user)
    copy = _COPY[locale]
    title = copy[f"title_{stage}"]
    ends = copy["ends"].format(date=format_date(ends_at, user, locale))
    body = [
        _paragraph([_text(title, bold=True)]),
        _paragraph([_text(ends)]),
        _paragraph([_text(copy["after"])]),
        _paragraph(
            [
                {
                    "type": "link",
                    "href": plans_url(),
                    "content": [_text(copy["plans"], underline=True)],
                }
            ]
        ),
    ]
    return title, body, ends


_USER_COLUMNS = (
    "id",
    "ends_at",
    "notified_5d_at",
    "notified_1d_at",
    "user__id",
    "user__is_active",
    "user__is_deleted",
    "user__language",
    "user__timezone",
)


def due_trials(*, now: datetime | None = None, limit: int = DEFAULT_LIMIT) -> list[CoreTrial]:
    """Trials with a stage to do now, soonest to end first.

    Two indexed queries, one per stage, each answered by that stage's own partial index
    (`coretrial_due_5d` / `coretrial_due_1d`: only rows whose stamp is still NULL are in them, so
    both are empty almost always). A single query with an OR across the two stamps cannot use
    either partial index (the planner falls back to the whole `ends_at` index and filters, which
    reads every running trial of the next five days each minute). The user columns the pass needs
    ride along (`only`: the rest of the account row is not read). A trial both queries return is
    one row."""
    now = now or timezone.now()
    found: dict[int, CoreTrial] = {}
    for stage in (STAGE_1D, STAGE_5D):  # the soonest to end first
        rows = (
            CoreTrial.objects.filter(
                ends_at__gt=now,
                ends_at__lte=now + WINDOW[stage],
                **{f"notified_{stage}_at__isnull": True},
            )
            .select_related("user")
            .only(*_USER_COLUMNS)
            .order_by("ends_at")[:limit]
        )
        for trial in rows:
            found.setdefault(trial.pk, trial)
    return sorted(found.values(), key=lambda t: t.ends_at)[:limit]


def _helped_by_the_trial(user_id) -> bool:
    """Is the trial what gives this person their plan right now? Read from the database, not the
    60-second cache: the answer is "no" for a payer, a paying team's member and an ended trial."""
    from origin.search_engine import quota  # noqa: PLC0415 - search_engine is heavy

    quota.invalidate_effective_tier([str(user_id)])
    return quota.resolve_effective_tier(str(user_id))["trial_ends_at"] is not None


def _stage_for(trial: CoreTrial, now: datetime) -> tuple[str | None, str | None]:
    """`(stage to send, why not)`. Within a day of the end the 1-day notice is the only one that is
    still true; the 5-day one is then stamped, never sent."""
    if trial.ends_at <= now + WINDOW[STAGE_1D]:
        if trial.notified_1d_at is None:
            return STAGE_1D, None
        return None, SKIP_ALREADY_SENT
    return STAGE_5D, None


def _after_commit(first_item: InboxItems, title: str, push_body: str) -> None:
    """The push and the email, after the stamp and the items are durable. Through the existing
    inbox path, so the person's own switches decide; both swallow their own errors."""
    from origin.services.email_enqueue import enqueue_email_for_inbox_item  # noqa: PLC0415
    from origin.services.webpush_dispatch import dispatch_push_for_inbox_item  # noqa: PLC0415

    dispatch_push_for_inbox_item(first_item, title=title, body=push_body)
    enqueue_email_for_inbox_item(first_item, title=title)


def fire(trial: CoreTrial, *, now: datetime | None = None) -> str | None:
    """Handle the one stage that is due for this trial. Returns None when a notice was filed, else
    the reason it was not (see the SKIP_* names); neither is an error.

    Raises only on a real failure (the database): the transaction then rolls the stamp back with
    everything else and the next pass tries again."""
    now = now or timezone.now()
    user = trial.user
    stage, why = _stage_for(trial, now)
    with transaction.atomic():
        if stage is None:
            # The 1-day notice already went; only the stale 5-day stamp is outstanding.
            CoreTrial.objects.filter(pk=trial.pk, notified_5d_at__isnull=True).update(
                notified_5d_at=now
            )
            return why
        stamps = {f"notified_{stage}_at": now}
        if stage == STAGE_1D and trial.notified_5d_at is None:
            stamps["notified_5d_at"] = now  # superseded: never send "5 days" after "1 day"
        # The claim and the delivery in one transaction. The UPDATE's own filter is the lock: a
        # second pass that already lost the race updates 0 rows and files nothing.
        claimed = CoreTrial.objects.filter(
            pk=trial.pk, ends_at__gt=now, **{f"notified_{stage}_at__isnull": True}
        ).update(**stamps)
        if not claimed:
            return SKIP_RACE
        if not user.is_active or user.is_deleted:
            return SKIP_INACTIVE
        if not _helped_by_the_trial(user.pk):
            return SKIP_NOT_HELPED
        team_ids = list(
            TeamMembers.objects.filter(
                attendee_id=user.pk, is_deleted=False, team__is_deleted=False
            )
            .order_by("pk")
            .values_list("team__team_id", flat=True)
        )
        if not team_ids:
            return SKIP_NO_TEAM
        title, body, push_body = compose(stage, user, trial.ends_at)
        optionals = {
            "kind": KIND,
            "stage": stage,
            "ends_at": trial.ends_at.isoformat(),
            "href": PLANS_PATH,
        }
        items = [
            InboxItems.objects.create(
                team_id=team_id,
                sender=None,  # system-authored
                receiver=user,
                item_type=ITEM_TYPE_NOTICE,
                item_body=body,
                item_optionals=optionals,
                request_status="",  # an activity, not a request
            )
            for team_id in team_ids
        ]
        transaction.on_commit(lambda: _after_commit(items[0], title, push_body))
    logger.info(
        "[trial-notice] filed stage=%s user=%s teams=%d first_item=%s",
        stage,
        user.pk,
        len(items),
        items[0].item_id,
    )
    return None


def fire_due(*, now: datetime | None = None, limit: int = DEFAULT_LIMIT) -> dict:
    """Handle every trial with a stage due. Returns `{"sent", "skipped", "due"}`.

    One trial's failure never stops the batch: the others belong to other people."""
    now = now or timezone.now()
    due = due_trials(now=now, limit=limit)
    sent = skipped = 0
    for trial in due:
        try:
            reason = fire(trial, now=now)
        except Exception:  # noqa: BLE001 - one bad row must not stall the batch
            logger.exception("[trial-notice] failed trial=%s", trial.pk)
            continue
        if reason is None:
            sent += 1
        else:
            skipped += 1
            logger.info("[trial-notice] skipped trial=%s reason=%s", trial.pk, reason)
    return {"sent": sent, "skipped": skipped, "due": len(due)}
