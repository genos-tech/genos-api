"""What a brand-new team gets when it is created (MIP-1691, under MIP-1447).

Two things, both for the owner at creation time:

* every new team gets one open group chat called "general" (none existed, so a new team
  had nowhere to say hello); and
* the owner may name the departments (a short list). Each becomes the four things that,
  together, ARE a department in Genos (decided 2026-11-18): a project tag, a mention group,
  a team folder and an open group chat. Nothing else is made for them: no projects, no people.

Called from `TeamMasterView.post`. The list is validated BEFORE the team is made (a bad list is
a 400 and creates nothing); the setup itself is best-effort, each part in its own savepoint, so a
failure never costs the team and never leaves a half-made department.

Other people are not added to anything here. The chats are MARKED (`ChannelAutoJoin`, MIP-1708):
`general` as the team's default chat, a department's chat as the chat of its mention group, so
that a person who joins the team later, or is added to the group later, is put into them
(`services/team_default_chats`). The folders and tags are visible to the whole team.
"""

from __future__ import annotations

import logging
import re

from django.db import transaction

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectLabel
from origin.services.note_folder_manage import create_team_folder
from origin.services.organisation_examples import _create_group_chat, example_locale
from origin.services.team_default_chats import mark_default_chat, mark_department_chat

log = logging.getLogger(__name__)

MAX_DEPARTMENTS = 20
# `ProjectLabel.name` is 30 characters; the tag takes the department's name as typed.
MAX_NAME_LENGTH = 30
# `MentionGroupMaster.group_name` is 40.
MAX_SLUG_LENGTH = 40
GENERAL_TITLE = "general"
TAG_COLORS = [
    "#0ea5e9",
    "#10b981",
    "#f59e0b",
    "#8b5cf6",
    "#ef4444",
    "#14b8a6",
    "#ec4899",
    "#6366f1",
]
TAG_TEXT_COLOR = "#ffffff"

COPY = {
    "en": {
        "general_hello": (
            "👋 Welcome to the open chat for the whole team. Anyone in the team can find it "
            "by name and join. Use it for what concerns everybody; talk about one piece of "
            "work in that project's chat."
        ),
        "department_hello": (
            "👋 This is the open chat for {name}. Anyone in the team can find it by name and "
            "join. Talk here about what concerns the whole department; talk about one piece "
            "of work in that project's chat."
        ),
    },
    "ja": {
        "general_hello": (
            "👋 チーム全体のオープンなチャットへようこそ。チームの誰でも名前で見つけて参加"
            "できます。全員に関わることはここで、ひとつの仕事の話はそのプロジェクトの"
            "チャットで話します。"
        ),
        "department_hello": (
            "👋 {name}のオープンなチャットです。チームの誰でも名前で見つけて参加できます。"
            "部署全体に関わることはここで、ひとつの仕事の話はそのプロジェクトのチャットで"
            "話します。"
        ),
    },
}


class DepartmentsRefused(ValueError):
    """The list of departments cannot be used; the message is shown to the person."""


def department_slug(name: str) -> str:
    """The mention-group name for a department: lowercase, words joined with `-`, only letters
    (any language), digits, `-` and `_`. `R&D` -> `rd`, `Customer Support` -> `customer-support`."""
    slug = re.sub(r"[\s/]+", "-", name.strip().lower())
    slug = re.sub(r"[^\w-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:MAX_SLUG_LENGTH]


def parse_departments(raw) -> list[str]:
    """The department names to make, from what the client sent.

    `None` or an empty list means none. Names are trimmed, empty ones are dropped, and one whose
    mention-group name is already taken by an earlier one is dropped (`Sales` and `sales`). Raises
    `DepartmentsRefused` for anything that is not a list of names, a name that is too long or has
    no letter or digit in it, or more than `MAX_DEPARTMENTS`.
    """
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise DepartmentsRefused("departments must be a list of names.")
    names: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = item.strip()
        if not name:
            continue
        if len(name) > MAX_NAME_LENGTH:
            raise DepartmentsRefused(
                f"A department name can be at most {MAX_NAME_LENGTH} characters: {name[:20]}…"
            )
        slug = department_slug(name)
        if not slug:
            raise DepartmentsRefused(f"A department name needs a letter or a digit: {name}")
        if slug in seen:
            continue
        seen.add(slug)
        names.append(name)
    if len(names) > MAX_DEPARTMENTS:
        raise DepartmentsRefused(f"At most {MAX_DEPARTMENTS} departments at a time.")
    return names


def create_general_chat(team, user) -> Channel | None:
    """The open group chat every new team gets. None when the team already has one."""
    if Channel.objects.filter(
        team=team, kind=ChannelKind.GM, title=GENERAL_TITLE, is_deleted=False
    ).exists():
        return None
    copy = COPY[example_locale(user)]
    channel = _create_group_chat(team, user, GENERAL_TITLE, copy["general_hello"], False)
    mark_default_chat(team, channel)
    return channel


def create_departments(team, user, names: list[str]) -> int:
    """Make the four things for each department; returns how many departments were made (one that
    already exists is skipped).

    All or nothing: one savepoint around the whole list, so a failure leaves no department
    half-made. `names` comes from `parse_departments`.
    """
    copy = COPY[example_locale(user)]
    created = 0
    with transaction.atomic():
        for index, name in enumerate(names):
            slug = department_slug(name)
            # Already there (the sample data's own "Sample dept", say): leave it, make no second
            # one, and do not count it.
            if (
                ProjectLabel.objects.filter(team=team, name__iexact=name).exists()
                or MentionGroupMaster.objects.filter(
                    team=team, group_name=slug, is_deleted=False
                ).exists()
            ):
                continue
            created += 1
            ProjectLabel.objects.create(
                team=team,
                name=name,
                color=TAG_COLORS[index % len(TAG_COLORS)],
                text_color=TAG_TEXT_COLOR,
                created_by=user,
            )
            group = MentionGroupMaster.objects.create(
                team=team, group_name=slug, description=name, created_by=user
            )
            MentionGroupMembers.objects.create(team=team, group=group, user=user, added_by=user)
            create_team_folder(
                team_id=str(team.team_id), user_id=user.id, name=name, visibility="public"
            )
            chat = _create_group_chat(
                team,
                user,
                f"{slug} · general",
                copy["department_hello"].format(name=name),
                False,
            )
            mark_department_chat(team, chat, group)
    return created


def set_up_new_team(team, user, departments: list[str]) -> dict:
    """The default chat and the departments, best-effort. Returns what happened, for the reply:
    `{"requested": n, "created": n}` (the department count)."""
    # Membership first: a team folder is made only for a member of the team, and the client's
    # own follow-up join is a no-op against this row.
    TeamMembers.objects.get_or_create(team=team, attendee=user)
    try:
        with transaction.atomic():
            create_general_chat(team, user)
    except Exception:  # noqa: BLE001 — the team must survive
        log.exception("default general chat failed for team=%s", team.team_id)

    created = 0
    if departments:
        try:
            created = create_departments(team, user, departments)
        except Exception:  # noqa: BLE001 — the team must survive
            log.exception("departments failed for team=%s", team.team_id)
    return {"requested": len(departments), "created": created}
