"""Report and takedown of a skill (MIP-2044).

A skill someone else wrote runs with the CALLER's access, so the damage a hostile one can do is
bounded by the guards in `run.py` and `guard.py`; this is how a bad one is stopped for everybody.

REPORT. Any signed-in member who can read a skill (and is not its owner) may report it, with one of
six reasons and up to 1,000 characters. One open report per account and skill (a second click is the
same report). Three open reports from different accounts within 7 days HIDE the skill
(`reported_hidden`) until an operator looks. An account may file at most 20 reports a day. The
operator queue is the `skills_moderate` command (the Django admin is not installed in this project),
and the staff API in `skill_views.py` does the same two things: TAKE DOWN and RESTORE.

TAKEDOWN. A taken-down, held or hidden skill is not `active`, and every reader and every run checks
`status == active` on each request (`service.can_read`, the picker, the by-name lookups, a resumed
leg's `rebuild_block`), so a takedown stops installs, team runs and the picker at once, with no cache
to expire. The owner keeps reading and editing their own text (they wrote it) but cannot share it
again until an operator restores it. The operator's reason is kept on the skill for the author to
read.

Everything here that changes a skill needs an OPERATOR: a staff account. A caller who is not one gets
the same "not found" as an unknown skill, so the route does not confirm that it exists.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from origin.models.common.user_models import CustomUser
from origin.models.skills.skill_models import (
    REPORT_DISMISSED,
    REPORT_OPEN,
    REPORT_REASON_CHOICES,
    REPORT_UPHELD,
    STATUS_ACTIVE,
    STATUS_REPORTED_HIDDEN,
    STATUS_TAKEN_DOWN,
    Skill,
    SkillReport,
)
from origin.services.skills import notices, service

logger = logging.getLogger(__name__)

# Three different accounts within seven days hide the skill; twenty reports a day per account.
HIDE_AFTER_REPORTERS = 3
HIDE_WINDOW = timedelta(days=7)
REPORTS_PER_DAY = 20
REPORT_TEXT_MAX = 1000
REASON_MAX = 500

REASONS = tuple(value for value, _label in REPORT_REASON_CHOICES)


class ReportLimitReached(service.SkillError):
    status = 429
    code = "report_limit"

    def __init__(self) -> None:
        super().__init__(f"You can send {REPORTS_PER_DAY} reports a day. Try again tomorrow.")


def is_operator(user: Any) -> bool:
    return bool(getattr(user, "is_authenticated", False) and getattr(user, "is_staff", False))


def require_operator(user: Any) -> None:
    """The same 404 as an unknown skill for everybody else. Not behind `GENOS_SKILLS` itself: the
    command (`skills_moderate`) must be able to stop a skill whatever the switch says (the API routes
    are 404 while it is off, because nothing runs then)."""
    if not is_operator(user):
        raise service.SkillNotFound


# -- report ------------------------------------------------------------------------------------


def _clean_text(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > REPORT_TEXT_MAX:
        raise service.SkillError(f"The note may be at most {REPORT_TEXT_MAX:,} characters.")
    return text


def report(
    user: CustomUser, skill_id: Any, reason: Any, note: Any = ""
) -> tuple[SkillReport, bool]:
    """File a report; `(report, created)`. A member reports only what they may read, and not their
    own skill. The skill is hidden when the third distinct reporter in 7 days files one."""
    service.require_enabled()
    reason = str(reason or "").strip().lower()
    if reason not in REASONS:
        raise service.SkillError(f"`reason` must be one of: {', '.join(REASONS)}.")
    text = _clean_text(note)
    skill = service.get_readable(user.pk, skill_id)
    if service.is_owner(skill, user.pk):
        raise service.SkillError("This is your own skill.")
    with transaction.atomic():
        skill = Skill.objects.select_for_update().get(pk=skill.pk)
        existing = SkillReport.objects.filter(
            skill=skill, reporter=user, status=REPORT_OPEN
        ).first()
        if existing is not None:
            return existing, False
        since = timezone.now() - timedelta(days=1)
        if (
            SkillReport.objects.filter(reporter=user, created_at__gte=since).count()
            >= REPORTS_PER_DAY
        ):
            raise ReportLimitReached
        version_no = skill.published_version_no
        row = SkillReport.objects.create(
            skill=skill, version_no=version_no, reporter=user, reason=reason, text=text
        )
        _maybe_hide(skill)
    return row, True


def _maybe_hide(skill: Skill) -> None:
    """Hide an `active` skill once enough different accounts have an open report in the window.
    Inside a transaction with the skill row locked."""
    if skill.status != STATUS_ACTIVE:
        return
    since = timezone.now() - HIDE_WINDOW
    # An account whose earlier report on THIS skill an operator dismissed does not count toward
    # hiding it again (MIP-2192): three accounts could otherwise re-hide a restored skill at once.
    # Only while the text is the one the operator judged: a report of a later version counts again.
    # `reporter__isnull=False` matters: `NOT IN (..., NULL)` matches nothing in SQL.
    dismissed = SkillReport.objects.filter(
        skill=skill,
        status=REPORT_DISMISSED,
        reporter__isnull=False,
        version_no__gte=skill.published_version_no,
    ).values("reporter_id")
    reporters = (
        SkillReport.objects.filter(skill=skill, status=REPORT_OPEN, created_at__gte=since)
        .exclude(reporter__isnull=True)
        .exclude(reporter_id__in=dismissed)
        .values("reporter_id")
        .distinct()
        .count()
    )
    if reporters >= HIDE_AFTER_REPORTERS:
        skill.status = STATUS_REPORTED_HIDDEN
        skill.moderation_reason = f"Hidden after {reporters} reports, waiting for an operator."
        skill.moderated_at = timezone.now()
        skill.save(update_fields=["status", "moderation_reason", "moderated_at", "updated_at"])
        logger.warning("skills: %s hidden after %d reports", skill.pk, reporters)
        notices.notify_author(skill, notices.HIDDEN)


# -- operator decisions ------------------------------------------------------------------------


def _clean_reason(value: Any, *, required: bool) -> str:
    reason = " ".join(str(value or "").split())
    if required and not reason:
        raise service.SkillError("A reason is required: the author is shown it.")
    if len(reason) > REASON_MAX:
        raise service.SkillError(f"The reason may be at most {REASON_MAX} characters.")
    return reason


def _decide(skill: Skill, operator: Any, status: str, reason: str, *, report_status: str) -> None:
    skill.status = status
    skill.moderation_reason = reason
    skill.moderated_at = timezone.now()
    skill.moderated_by = operator
    skill.save(
        update_fields=["status", "moderation_reason", "moderated_at", "moderated_by", "updated_at"]
    )
    SkillReport.objects.filter(skill=skill, status=REPORT_OPEN).update(
        status=report_status, resolved_by=operator, resolved_at=timezone.now()
    )


def take_down(operator: Any, skill_id: Any, reason: Any, *, harmful: bool = False) -> Skill:
    """Stop a skill for everybody but its owner, at once. Its open reports are upheld.

    `harmful` (MIP-2192): the operator judged it harmful, not merely unwanted. Copies made earlier
    (`forked_from`) are then marked (`source_harmful_at`): each shows a warning and its owner must
    confirm it before it runs. Calling it again on a skill already down is allowed, so an operator
    can add the mark later; it is not removed here, only by a restore."""
    require_operator(operator)
    reason = _clean_reason(reason, required=True)
    with transaction.atomic():
        skill = Skill.objects.select_for_update().filter(pk=pk_of(skill_id)).first()
        if skill is None:
            raise service.SkillNotFound
        already_down = skill.status == STATUS_TAKEN_DOWN
        _decide(skill, operator, STATUS_TAKEN_DOWN, reason, report_status=REPORT_UPHELD)
        if harmful:
            skill.takedown_harmful = True
            skill.save(update_fields=["takedown_harmful"])
            # A fresh moment even when the copy was marked before: a confirmation older than it
            # no longer counts, so a second harmful takedown asks again.
            Skill.objects.filter(pk__in=_copies_of(skill)).update(source_harmful_at=timezone.now())
        if not already_down:  # adding the harmful mark later is not a second notice
            notices.notify_author(skill, notices.TAKEN_DOWN)
    logger.warning(
        "skills: %s taken down by operator %s%s",
        skill.pk,
        operator.pk,
        " (harmful)" if harmful else "",
    )
    return skill


def restore(operator: Any, skill_id: Any, reason: Any = "") -> Skill:
    """Put a held, hidden or taken-down skill back (`active`). Open reports are dismissed."""
    require_operator(operator)
    reason = _clean_reason(reason, required=False)
    with transaction.atomic():
        skill = Skill.objects.select_for_update().filter(pk=pk_of(skill_id)).first()
        if skill is None:
            raise service.SkillNotFound
        if skill.status not in service.MODERATED_STATUSES:
            raise service.SkillConflict("This skill is not held, hidden or taken down.")
        _decide(skill, operator, STATUS_ACTIVE, reason, report_status=REPORT_DISMISSED)
        if skill.takedown_harmful:
            # The operator un-judged it: the copies' warning goes with the mark.
            skill.takedown_harmful = False
            skill.save(update_fields=["takedown_harmful"])
            Skill.objects.filter(pk__in=_copies_of(skill), source_harmful_at__isnull=False).update(
                source_harmful_at=None
            )
        notices.notify_author(skill, notices.RESTORED)
    logger.warning("skills: %s restored by operator %s", skill.pk, operator.pk)
    return skill


COPY_CHAIN_DEPTH = 50


def _copies_of(skill: Skill) -> list[Any]:
    """The ids of every copy of `skill`, copies of copies included (`forked_from`, one query a
    generation, bounded). A copy of a copy carries the same text, so it must warn too."""
    found: list[Any] = []
    seen = {skill.pk}
    frontier = [skill.pk]
    for _ in range(COPY_CHAIN_DEPTH):
        frontier = [
            pk
            for pk in Skill.objects.filter(forked_from_id__in=frontier).values_list("pk", flat=True)
            if pk not in seen
        ]
        if not frontier:
            break
        seen.update(frontier)
        found.extend(frontier)
    return found


def pk_of(skill_id: Any) -> Any:
    try:
        return uuid.UUID(str(skill_id))
    except ValueError:
        return None
