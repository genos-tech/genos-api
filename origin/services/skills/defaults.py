"""The skills every member has installed from the start (MIP-2589).

Only a member's own skills and the ones they installed can be picked or run. These few general
official ones are installed for everybody so the pickers are not empty on day one. They are ordinary
installs: a member can uninstall them, and an uninstall stays (nothing re-creates a row for an
existing account). Rows are made when an account is created (`signals/skill_signals.py`), when the
official sync first creates one of these skills (`official.sync`), and once for the accounts that
existed before (the data migration, which keeps its own copy of the list).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from django.db import transaction

from origin.models.common.user_models import CustomUser
from origin.models.skills.skill_models import (
    STATUS_ACTIVE,
    VISIBILITY_PUBLIC,
    Skill,
    SkillInstall,
)

logger = logging.getLogger(__name__)

DEFAULT_SKILL_SLUGS = (
    "unread-summary",
    "comments-waiting-on-me",
    "triage",
    "wrapup",
    "weekly-review",
)

_BATCH = 500


def default_skills(slugs: Iterable[str] | None = None) -> list[Skill]:
    """The default skills that exist and can be installed (empty before the official sync ran)."""
    wanted = [s for s in DEFAULT_SKILL_SLUGS if slugs is None or s in set(slugs)]
    return list(
        Skill.objects.filter(
            slug__in=wanted,
            is_official=True,
            visibility=VISIBILITY_PUBLIC,
            status=STATUS_ACTIVE,
            published_version_no__gt=0,
        ).only("id", "published_version_no")
    )


def install_defaults(user_ids: Iterable[Any], skills: list[Skill] | None = None) -> None:
    """Install the default skills for these accounts. Idempotent; a row that exists is kept (its
    pinned version too). `counted=False`: these do not move the Hub's install count."""
    skills = default_skills() if skills is None else skills
    if not skills:
        return
    rows = [
        SkillInstall(
            user_id=user_id,
            skill_id=skill.pk,
            version_no=skill.published_version_no,
            counted=False,
        )
        for user_id in user_ids
        for skill in skills
    ]
    SkillInstall.objects.bulk_create(rows, ignore_conflicts=True, batch_size=_BATCH)


def install_for_new_account(user: Any) -> None:
    """For an account that was just created. Never stops the signup: a failure is logged."""
    if user.is_system_user:
        return
    try:
        with transaction.atomic():
            install_defaults([user.pk])
    except Exception:  # noqa: BLE001 - the account matters more than its default skills
        logger.exception("skills: default installs failed for user=%s", user.pk)


def install_for_everyone(slug: str) -> None:
    """The official sync just CREATED a default skill, so nobody can have uninstalled it yet: give
    it to the accounts that exist."""
    if slug not in DEFAULT_SKILL_SLUGS:
        return
    skills = default_skills([slug])
    if not skills:
        return
    ids = CustomUser.objects.filter(is_system_user=False, is_deleted=False).values_list(
        "pk", flat=True
    )
    install_defaults(ids.iterator(chunk_size=_BATCH), skills)
