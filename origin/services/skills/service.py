"""Genos Skills (MIP-2040): who may read a skill, and every change to one.

ONE place for the rules, so the REST views (and later the runner, the MCP tools and the Hub)
cannot disagree about them. A view translates `SkillError` to a status code and a sentence.

Access, in one table. "Published" means `published_version_no > 0`; `status` must be `active` for
anyone but the owner.

| visibility | read, run (when running ships)             | text, details, publish    | rename, share, delete, editors |
| private    | the owner, the co-editors                   | the owner, the co-editors | the owner |
| team       | a member of the skill's team, NOT a guest   | the owner, the co-editors | the owner |
| public     | any signed-in user (needs a version)        | the owner, the co-editors | the owner |

Team sharing (MIP-2043):

* A CO-EDITOR is a member of the skill's team the owner invited: an Editor row
  (`NotePermissionMaster`, role 2) on the skill's body note, valid only while the person is on the
  team. They read the owner's live draft, edit the text and the details, and publish a new
  version. They cannot rename the skill, change who it is shared with, delete it or add editors.
* The TEAM OWNER may hide a team skill from the team (`hidden_by_team`) and show it again. A hidden
  skill is a 404 for every member but its owner and co-editors; the team owner still sees it, to
  unhide it. Moving the skill private and back does not clear the hide.
* The body note answers to the skill, not to the note rules (`skill_note_role`): the owner is its
  Owner, a co-editor its Editor, everybody else (a stray Viewer row too) has no role.
* When a member leaves the team or deletes the account, their team skills move to the team owner
  (`hand_over_team_skills`); their private skills are deleted with the account.

A stranger's id is the same "not found" as a missing one (`get_readable`), so an id never
confirms that a private or other team's skill exists.

READING THE TEXT. The owner reads the LIVE note (what the editor shows). Everybody else reads the
published version: a team or public skill's text moves under a reader only when a new version is
cut (`cut_version`), and that happens only when the owner publishes or first shares the skill.
Saving (`update`) keeps the edit as the owner's draft for a team skill too (MIP-2177, Ken
2026-11-23): the live note and the skill row ARE the draft, the version is the published copy, and
`unpublished_changes` says how they differ.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, OuterRef, Q, QuerySet, Subquery
from django.utils import timezone

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.skills.skill_models import (
    EFFORT_CHOICES,
    SOURCE_CHOICES,
    STATUS_ACTIVE,
    STATUS_HIDDEN_BY_TEAM,
    STATUS_IN_REVIEW,
    STATUS_REPORTED_HIDDEN,
    STATUS_TAKEN_DOWN,
    TOOL_LEVEL_ACT,
    TOOL_LEVEL_READ,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_TEAM,
    Skill,
    SkillInstall,
    SkillStar,
    SkillVersion,
)
from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.services import collab_push
from origin.services.note_delete import DeleteRefused, delete_note
from origin.services.skills import guard, text
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_EDITOR, ROLE_OWNER
from origin.views.utils.scope_guards import is_team_member

logger = logging.getLogger(__name__)

# How many co-editors one skill may have. A skill is a short document one team works on.
MAX_EDITORS = 20

SKILL_FOLDER_NAME = "Skills"

# How many skills a member may own, by their EFFECTIVE tier (design K9; money and product
# numbers the maintainer confirmed on 2026-11-23). None = unlimited. Kept here until the plan
# table (`TIER_QUOTAS`, the billing page) grows a `skills_max` key.
SKILLS_MAX_BY_TIER: dict[str, int | None] = {
    "free": 3,
    "core": 25,
    "pro": 100,
    "max": None,
    "enterprise": None,
}

# The states an operator (or the publish-time scan) has put a skill in. Readers and runs stop at
# once (`can_read` wants `active`); the owner cannot move the skill out of one by editing, and
# cannot share it again, until an operator restores it (MIP-2044, `moderation.py`).
MODERATED_STATUSES = frozenset({STATUS_IN_REVIEW, STATUS_REPORTED_HIDDEN, STATUS_TAKEN_DOWN})

_VISIBILITIES = (VISIBILITY_PRIVATE, VISIBILITY_TEAM, VISIBILITY_PUBLIC)
_TOOL_LEVELS = (TOOL_LEVEL_READ, TOOL_LEVEL_ACT)
_EFFORTS = tuple(value for value, _label in EFFORT_CHOICES)
_SOURCES = tuple(value for value, _label in SOURCE_CHOICES)

# What a PUBLIC skill's body may not contain (design section 3): media and attachments (tracking
# pixels, other people's hosting) and chips (they leak titles and mean nothing to another team).
_MEDIA_BLOCKS = frozenset({"image", "video", "audio", "file"})
_CHIP_NODES = frozenset(
    {"mention", "mentionGroup", "hashTask", "hashProject", "hashNote", "hashChat"}
)
_INTERNAL_SCHEMES = ("task:", "user:", "group:", "project:", "note:", "chat:", "milestone:")


# -- errors ---------------------------------------------------------------------------------------


class SkillError(Exception):
    """A refusal, worded for the person. `status` is the HTTP answer a view gives."""

    status = 400
    code = ""

    def __init__(self, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.extra = extra


class SkillNotFound(SkillError):
    status = 404

    def __init__(self, message: str = "Skill not found.") -> None:
        super().__init__(message)


class SkillForbidden(SkillError):
    status = 403


class SkillConflict(SkillError):
    status = 409


class SkillLimitReached(SkillError):
    """The plan's count of skills is used up."""

    status = 429
    code = "plan_limit"

    def __init__(self, limit: int, used: int) -> None:
        super().__init__(
            f"Your plan keeps up to {limit} skills and you have {used}. Delete one or upgrade.",
            limit=limit,
            used=used,
        )


class SkillDisabled(SkillError):
    """The feature switch is off: the same 404 as an unknown route."""

    status = 404

    def __init__(self) -> None:
        super().__init__("Not found.")


def enabled() -> bool:
    return bool(getattr(settings, "GENOS_SKILLS", False))


def public_enabled() -> bool:
    return bool(getattr(settings, "GENOS_SKILLS_PUBLIC", False))


def require_enabled() -> None:
    if not enabled():
        raise SkillDisabled


def refuse_if_moderated(skill: Skill) -> None:
    """The owner of a held, hidden or taken-down skill may not share it again themselves."""
    if skill.status in MODERATED_STATUSES:
        raise SkillForbidden(
            "This skill is waiting for a Genos operator, so it cannot be shared right now."
            + (f" Reason: {skill.moderation_reason}" if skill.moderation_reason else "")
        )


# -- access ---------------------------------------------------------------------------------------


def is_owner(skill: Skill, user_id: Any) -> bool:
    return str(skill.owner_id) == str(user_id)


def _published(skill: Skill) -> bool:
    return skill.published_version_no > 0 and skill.status == STATUS_ACTIVE


def _editor_rows(note_id: Any):
    return NotePermissionMaster.objects.filter(
        note_type=NOTE_TYPE_PERSONAL, note_id=note_id, role_id=ROLE_EDITOR
    )


def is_editor(skill: Skill, user_id: Any) -> bool:
    """A co-editor: an Editor row on the body note, held by someone who is still on the skill's
    team. The row alone is not enough (a person who left the team keeps no access even if a row
    survived), and the owner is not counted here."""
    if skill.note_id is None or user_id is None or is_owner(skill, user_id):
        return False
    if not _editor_rows(skill.note_id).filter(user_id=user_id).exists():
        return False
    return is_team_member(skill.team_id, user_id)


def is_team_owner(skill: Skill, user_id: Any) -> bool:
    return TeamMaster.objects.filter(team_id=skill.team_id, owner_id=user_id).exists()


def can_edit(skill: Skill, user_id: Any) -> bool:
    """May this member change the text and details and publish: the owner and the co-editors."""
    return is_owner(skill, user_id) or (
        skill.status != STATUS_TAKEN_DOWN and is_editor(skill, user_id)
    )


def reads_draft(skill: Skill, user_id: Any) -> bool:
    """Whose reading is the live note and the owner's metadata, not the published version."""
    return is_owner(skill, user_id) or is_editor(skill, user_id)


def can_hide(skill: Skill, user_id: Any) -> bool:
    """May this member hide the skill from, or show it to, the team: the team owner, for a team
    skill someone else wrote, that no operator has taken over."""
    return (
        skill.visibility == VISIBILITY_TEAM
        and skill.status in (STATUS_ACTIVE, STATUS_HIDDEN_BY_TEAM)
        and not is_owner(skill, user_id)
        and is_team_owner(skill, user_id)
    )


def skill_note_role(user_id: Any, note_id: Any) -> tuple[bool, int | None]:
    """`(is_skill_body, role)` for the note rules (`note_role.get_effective_role`). The body of a
    skill answers to the skill: its owner is the note's Owner, a co-editor its Editor, and nobody
    else has a role, whatever row they hold (a Viewer row, a share, an access request)."""
    skill = Skill.objects.filter(note_id=note_id).first()
    if skill is None:
        return False, None
    if is_owner(skill, user_id):
        # An owner who has left the team may only delete (`get_owned`), not write into it.
        return True, ROLE_OWNER if is_team_member(skill.team_id, user_id) else None
    if skill.status != STATUS_TAKEN_DOWN and is_editor(skill, user_id):
        return True, ROLE_EDITOR
    return True, None


def skill_body_ids(note_ids: Any) -> set[int]:
    """Which of these note ids are skill bodies, in one query."""
    ids = list(note_ids)
    if not ids:
        return set()
    return set(Skill.objects.filter(note_id__in=ids).values_list("note_id", flat=True))


def can_read(skill: Skill, user_id: Any) -> bool:
    """May this member see the skill (its metadata and its published text)."""
    if is_owner(skill, user_id):
        return True
    if is_editor(skill, user_id) and skill.status != STATUS_TAKEN_DOWN:
        return True  # an invited co-editor reads the draft, private or not
    if (
        skill.visibility == VISIBILITY_TEAM
        and skill.status == STATUS_HIDDEN_BY_TEAM
        and skill.published_version_no > 0
        and is_team_owner(skill, user_id)
    ):
        return True  # the team owner sees what they hid, to show it again
    if not _published(skill):
        return False
    if skill.visibility == VISIBILITY_PUBLIC:
        # GENOS_SKILLS_PUBLIC is the kill switch: off, public skills are readable by their
        # owners only, whenever they were made public.
        return public_enabled()
    if skill.visibility == VISIBILITY_TEAM:
        return is_team_member(skill.team_id, user_id)
    return False


def visible_to(user_id: Any) -> Q:
    """The `can_read` rule as a filter, for lists: one query however many skills."""
    in_my_teams = Q(
        team_id__in=TeamMembers.objects.filter(attendee_id=user_id, is_deleted=False).values(
            "team_id"
        )
    ) | Q(team_id__in=TeamMaster.objects.filter(owner_id=user_id).values("team_id"))
    published = Q(published_version_no__gt=0, status=STATUS_ACTIVE)
    public = Q(visibility=VISIBILITY_PUBLIC) if public_enabled() else Q(pk__in=[])
    # A co-editor (an Editor row on the body note, still on the team) reads the draft, private or
    # not; the team owner sees the team skills they hid. The same two rules as `can_read`.
    editing = (
        Q(
            note_id__in=NotePermissionMaster.objects.filter(
                user_id=user_id, note_type=NOTE_TYPE_PERSONAL, role_id=ROLE_EDITOR
            ).values("note_id")
        )
        & in_my_teams
        & ~Q(status=STATUS_TAKEN_DOWN)
    )
    moderating = Q(
        visibility=VISIBILITY_TEAM,
        status=STATUS_HIDDEN_BY_TEAM,
        published_version_no__gt=0,
        team_id__in=TeamMaster.objects.filter(owner_id=user_id).values("team_id"),
    )
    return (
        Q(owner_id=user_id)
        | editing
        | moderating
        | (published & (public | (Q(visibility=VISIBILITY_TEAM) & in_my_teams)))
    )


def get_readable(user_id: Any, skill_id: Any) -> Skill:
    """The skill when the member may read it, else the same "not found" a missing id gets."""
    try:
        skill = (
            Skill.objects.select_related("owner", "owner__skill_profile")
            .filter(pk=skill_id)
            .first()
        )
    except (ValueError, TypeError):  # not a UUID
        skill = None
    if skill is None or not can_read(skill, user_id):
        raise SkillNotFound
    return skill


def get_owned(user_id: Any, skill_id: Any, *, lock: bool = False, need_team: bool = True) -> Skill:
    """The skill when the member may CHANGE it. A reader gets a 403 (they already know it
    exists); a stranger gets the "not found". An owner who has left the skill's team may only
    delete it (`need_team=False`): they must not keep publishing text into a team they are not in."""
    skill = get_readable(user_id, skill_id)
    if not is_owner(skill, user_id):
        raise SkillForbidden("Only the owner can change this skill.")
    if need_team and not is_team_member(skill.team_id, user_id):
        raise SkillForbidden("You are no longer a member of this skill's team.")
    if lock:
        skill = Skill.objects.select_for_update().get(pk=skill.pk)
    return skill


def get_editable(user_id: Any, skill_id: Any, *, lock: bool = False) -> Skill:
    """The skill when the member may change its text and details and publish it: the owner or a
    co-editor, both still on the team. A reader gets a 403, a stranger the "not found". With
    `lock`, the row is locked and the check is made again on the locked row, so a removal that
    raced the call is honoured."""
    skill = get_readable(user_id, skill_id)

    def check(row: Skill) -> None:
        if not can_edit(row, user_id):
            raise SkillForbidden("Only the owner and the editors can change this skill.")
        if not is_team_member(row.team_id, user_id):
            raise SkillForbidden("You are no longer a member of this skill's team.")

    check(skill)
    if lock:
        skill = Skill.objects.select_for_update().get(pk=skill.pk)
        check(skill)
    return skill


# -- plan limits ----------------------------------------------------------------------------------


def skills_limit(user_id: Any) -> int | None:
    """How many skills this member may own: their plan's count (None = unlimited). An error while
    reading the plan reads as unlimited, like the other plan gates: a member is never blocked by
    our failure."""
    from origin.search_engine import quota  # noqa: PLC0415 - search_engine is heavy

    try:
        tier = quota.get_effective_tier(str(user_id))
    except Exception:  # noqa: BLE001
        logger.exception("skills_limit: tier lookup failed for user=%s", user_id)
        return None
    return SKILLS_MAX_BY_TIER.get(tier)


def _require_room(user_id: Any) -> None:
    limit = skills_limit(user_id)
    if limit is None:
        return
    used = Skill.objects.filter(owner_id=user_id).count()
    if used >= limit:
        raise SkillLimitReached(limit, used)


# -- the body note --------------------------------------------------------------------------------


def _skill_folder(user: CustomUser, team_id: Any) -> PersonalNoteFolder:
    folder = PersonalNoteFolder.objects.filter(
        team_id=team_id, owner=user, scope=PersonalNoteFolder.SCOPE_SKILL
    ).first()
    if folder is None:
        folder = PersonalNoteFolder.objects.create(
            team_id=team_id,
            owner=user,
            name=SKILL_FOLDER_NAME,
            scope=PersonalNoteFolder.SCOPE_SKILL,
        )
    return folder


def _create_note(
    user: CustomUser, team_id: Any, title: str, blocks: list[dict]
) -> PersonalNoteMaster:
    folder = _skill_folder(user, team_id)
    return PersonalNoteMaster.objects.create(
        team_id=team_id, owner=user, title=title, body=blocks, folder_id=folder.folder_id
    )


def _live_blocks(skill: Skill) -> list[dict]:
    if skill.note_id is None:
        return []
    body = (
        PersonalNoteMaster.objects.filter(note_id=skill.note_id)
        .values_list("body", flat=True)
        .first()
    )
    return body if isinstance(body, list) else []


def live_markdown(skill: Skill) -> str:
    return blocks_to_markdown(_live_blocks(skill))


def _save_note(note_id: int, **fields: Any) -> None:
    """Write the body note through `save()` so `ts_updated_at` moves: the indexer picks a note up
    by that column, and the owner's search must not keep returning the old text."""
    note = PersonalNoteMaster.objects.get(note_id=note_id)
    for name, value in fields.items():
        setattr(note, name, value)
    note.save(update_fields=[*fields, "ts_updated_at"])


def _push_to_editor(note_id: int, blocks: list[dict]) -> None:
    """Best-effort: an editor that has the note open converges on the new text."""
    try:
        collab_push.push_note_body("personal", note_id, blocks)
    except Exception:  # noqa: BLE001 - the body is saved; a collab outage must not fail the call
        logger.exception("skills: collab push failed for note %s", note_id)


# -- versions -------------------------------------------------------------------------------------


def _hash(blocks: list[dict], meta: dict[str, Any]) -> str:
    payload = json.dumps([blocks, meta], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


# The metadata a version freezes (the same names on `Skill` and `SkillVersion`). The text is the
# other half of a version.
_VERSIONED_FIELDS = (
    "slug",
    "title",
    "description",
    "tags",
    "icon",
    "tool_level",
    "argument_hint",
    "effort",
    "language",
)


def unpublished_changes(skill: Skill, *, live_text: str | None = None) -> list[str]:
    """How the owner's draft differs from what readers get: the `Skill` field names that changed
    since the published version, plus `"body_markdown"` when the text did. Empty for a private
    skill (nobody reads it, there is nothing to publish) and for one with nothing new. Compared on
    the markdown, not the block JSON, so opening the editor never counts as an edit.
    `live_text` saves rendering the note again when the caller has done it."""
    if skill.visibility == VISIBILITY_PRIVATE or skill.published_version_no <= 0:
        return []
    version = skill.versions.filter(version_no=skill.published_version_no).first()
    if version is None:
        return []
    changed = [name for name in _VERSIONED_FIELDS if getattr(skill, name) != getattr(version, name)]
    if (live_markdown(skill) if live_text is None else live_text) != version.body_markdown:
        changed.append("body_markdown")
    return changed


def cut_version(skill: Skill, user: CustomUser, *, changelog: str = "") -> SkillVersion | None:
    """Snapshot the live note and metadata as the next immutable version, and make it the
    skill's current AND published one. Returns None when nothing changed since the newest
    version (the same text and metadata twice is not a new version). Call inside a transaction
    with the skill row locked."""
    blocks = _live_blocks(skill)
    # The editor can grow the note past the limit that the API enforces: a sentence, not a 500.
    markdown = _wrap(lambda: text.check_body_markdown(blocks_to_markdown(blocks)))
    meta = {name: getattr(skill, name) for name in _VERSIONED_FIELDS}
    digest = _hash(blocks, meta)
    newest = skill.versions.order_by("-version_no").first()
    if newest is not None and newest.content_hash == digest:
        return None
    # What a scan of the text finds is kept on the version (MIP-2044): it is what an operator reads
    # when a PUBLIC version is held, and what a later reader of the version can be told.
    scan = guard.scan(markdown, extra=(meta["title"], meta["description"]))
    version = SkillVersion.objects.create(
        skill=skill,
        version_no=(newest.version_no if newest else 0) + 1,
        body=blocks,
        body_markdown=markdown,
        content_hash=digest,
        changelog=" ".join(str(changelog or "").split())[: text.DESCRIPTION_MAX],
        created_by=user,
        scan_result=scan.as_json(),
        **meta,
    )
    skill.current_version_no = version.version_no
    skill.published_version_no = version.version_no
    if skill.published_at is None:
        skill.published_at = timezone.now()
    skill.save(update_fields=["current_version_no", "published_version_no", "published_at"])
    from origin.services.skills import hub  # noqa: PLC0415 - hub imports this module

    hub.refresh_listing(skill, version)  # the directory reads the published version's columns
    return version


def hold_if_flagged(skill: Skill) -> bool:
    """A PUBLIC skill whose newest version the scan flagged goes `in_review`: nobody but the owner
    reads or runs it until an operator decides (`moderation.restore` lets it out). Team and private
    skills are only recorded: their readers are people the author works with, and the fence and the
    caller's own permissions still apply. True when the skill was held. Inside a transaction."""
    if skill.visibility != VISIBILITY_PUBLIC or skill.status != STATUS_ACTIVE:
        return False
    newest = skill.versions.order_by("-version_no").first()
    if newest is None:
        return False
    result = newest.scan_result or {}
    if not result:
        # A version cut before the scan existed (migration 0272 shipped first): scan it now, and keep
        # the result on the row (a queryset update: the instance itself is immutable).
        result = guard.scan(
            newest.body_markdown, extra=(newest.title, newest.description)
        ).as_json()
        SkillVersion.objects.filter(pk=newest.pk).update(scan_result=result)
    if not result.get("held"):
        return False
    codes = ", ".join(result.get("codes") or [])
    skill.status = STATUS_IN_REVIEW
    skill.moderation_reason = (
        f"The automatic check flagged version {newest.version_no} ({codes}). "
        "An operator will look at it before it is shared."
    )[:500]
    skill.moderated_at = timezone.now()
    skill.moderated_by = None
    skill.save(update_fields=["status", "moderation_reason", "moderated_at", "moderated_by"])
    logger.warning("skills: %s held for review (%s)", skill.pk, codes)
    return True


def public_problems(skill: Skill, blocks: list[dict]) -> list[str]:
    """What stops this skill from being PUBLIC, as sentences. The account gates (plan, verified
    email, account age), the secrets / injection scan and the license confirmation belong to the
    Hub and Safety tasks (MIP-2047, MIP-2044); this is the content part of the checklist."""
    problems: list[str] = []
    if not skill.title.strip():
        problems.append("Add a title.")
    if not skill.description.strip():
        problems.append("Add a description.")
    if not skill.tags:
        problems.append("Add at least one tag.")
    if not blocks:
        problems.append("The skill text is empty.")
    found: set[str] = set()

    def walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            kind = node.get("type")
            if kind in _MEDIA_BLOCKS:
                found.add("images and attachments")
            if kind in _CHIP_NODES:
                found.add("@ and # chips")
            href = node.get("href")
            if isinstance(href, str) and href.lower().startswith(_INTERNAL_SCHEMES):
                found.add("links into your workspace")
            walk(node.get("content"))
            walk(node.get("children"))
            if isinstance(node.get("content"), dict):  # table content
                walk((node["content"] or {}).get("rows"))
            for row in node.get("rows") or []:
                walk((row or {}).get("cells"))

    walk(blocks)
    for what in sorted(found):
        problems.append(f"A public skill cannot contain {what}. Remove them.")
    return problems


# -- create, read, change, delete -----------------------------------------------------------------


@dataclass
class Fields:
    """What a create or update may carry. `None` = not given."""

    title: Any = None
    slug: Any = None
    description: Any = None
    tags: Any = None
    icon: Any = None
    argument_hint: Any = None
    tool_level: Any = None
    effort: Any = None
    language: Any = None
    body_markdown: Any = None
    visibility: Any = None
    # Making a skill public (MIP-2047): the author's confirmation of the content terms, and the
    # public handle and display name when they have none yet. Not stored on the skill.
    accept_terms: Any = None
    handle: Any = None
    display_name: Any = None


def _clean_choice(value: Any, allowed: tuple[str, ...], what: str) -> str:
    value = str(value or "").strip().lower()
    if value not in allowed:
        raise SkillError(f"{what} must be one of: {', '.join(a for a in allowed if a)}.")
    return value


def _clean_icon(value: Any) -> str:
    icon = str(value or "").strip()
    if len(icon) > 16:
        raise SkillError("The icon must be a single emoji.")
    return icon


def _slug_free(team_id: Any, owner_id: Any, slug: str, *, exclude: Any = None) -> bool:
    qs = Skill.objects.filter(team_id=team_id, owner_id=owner_id, slug=slug)
    if exclude is not None:
        qs = qs.exclude(pk=exclude)
    return not qs.exists()


def _free_slug(team_id: Any, owner_id: Any, base: str) -> str:
    """`base`, or `base-2`, `base-3`... for a skill made without a name (an import, a copy)."""
    candidate = base
    n = 1
    while True:
        try:
            clean = text.clean_slug(candidate)
        except text.SkillTextError:
            clean = None
        if clean and _slug_free(team_id, owner_id, clean):
            return clean
        n += 1
        suffix = f"-{n}"
        candidate = f"{base[: text.SLUG_MAX - len(suffix)]}{suffix}"
        if n > 200:
            raise SkillError("Could not find a free name for this skill. Give it a name.")


def _wrap(call):
    try:
        return call()
    except text.SkillTextError as exc:
        raise SkillError(str(exc)) from exc


def _require_member(user: CustomUser, team_id: Any) -> None:
    if not is_team_member(team_id, user.pk):
        # The same sentence as a missing team: it must not confirm that a team exists.
        raise SkillNotFound("Team not found.")


def create(
    user: CustomUser,
    team_id: Any,
    fields: Fields,
    *,
    source: str = "user",
    forked_from: Skill | None = None,
    forked_from_version_no: int | None = None,
) -> Skill:
    """A new skill, private unless `fields.visibility` says otherwise (and may be set)."""
    require_enabled()
    _require_member(user, team_id)
    if source not in _SOURCES or source == "official":
        raise SkillError("Unknown source.")
    title = _wrap(lambda: text.clean_title(fields.title))
    slug = (
        _wrap(lambda: text.clean_slug(fields.slug))
        if fields.slug
        else _free_slug(team_id, user.pk, text.slugify(title))
    )
    description = _wrap(lambda: text.clean_description(fields.description))
    tags = _wrap(lambda: text.clean_tags(fields.tags))
    hint = _wrap(lambda: text.clean_argument_hint(fields.argument_hint))
    tool_level = _clean_choice(fields.tool_level or TOOL_LEVEL_READ, _TOOL_LEVELS, "The tool level")
    effort = _clean_choice(fields.effort, _EFFORTS, "The effort") if fields.effort else ""
    visibility = _clean_choice(
        fields.visibility or VISIBILITY_PRIVATE, _VISIBILITIES, "The visibility"
    )
    markdown = _wrap(lambda: text.check_body_markdown(str(fields.body_markdown or "")))
    blocks = markdown_to_blocks(markdown)

    with transaction.atomic():
        # Serialise one member's creates so two at once cannot both pass the plan count.
        CustomUser.objects.select_for_update().filter(pk=user.pk).first()
        _require_room(user.pk)
        if not _slug_free(team_id, user.pk, slug):
            raise SkillConflict(f"You already have a skill named `{slug}`.")
        note = _create_note(user, team_id, title, blocks)
        try:
            with transaction.atomic():
                skill = Skill.objects.create(
                    team_id=team_id,
                    owner=user,
                    slug=slug,
                    title=title,
                    description=description,
                    tags=tags,
                    icon=_clean_icon(fields.icon),
                    note_id=note.note_id,
                    argument_hint=hint,
                    tool_level=tool_level,
                    effort=effort,
                    language=str(fields.language or "")[:16],
                    source=source,
                    forked_from=forked_from,
                    forked_from_version_no=forked_from_version_no,
                )
        except IntegrityError as exc:  # a racing create took the name
            raise SkillConflict(f"You already have a skill named `{slug}`.") from exc
        if visibility != VISIBILITY_PRIVATE:
            _set_visibility(skill, user, visibility, fields)
    return skill


def _set_visibility(
    skill: Skill, user: CustomUser, visibility: str, fields: Fields | None = None
) -> None:
    """Move a skill between private / team / public. Team and public need a published version, so
    going there cuts one (a public skill must also pass the content checklist). Going back to
    private keeps the versions and stops everyone else's access at once. Inside a transaction."""
    if visibility == skill.visibility:
        return
    if visibility != VISIBILITY_PRIVATE:
        refuse_if_moderated(skill)
        if skill.forked_from_id and skill.forked_from.status in MODERATED_STATUSES:
            # A copy of a stopped skill is the same text: it does not get shared around the stop.
            raise SkillForbidden("The skill this one was copied from is waiting for an operator.")
    if visibility == VISIBILITY_PUBLIC:
        if not public_enabled():
            raise SkillForbidden("Public skills are not available yet.")
        problems = public_problems(skill, _live_blocks(skill))
        if problems:
            raise SkillError(
                "This skill cannot be public yet: " + " ".join(problems), problems=problems
            )
        # The Hub's gates (MIP-2047): plan, verified email, account age, a handle, no secrets or
        # personal data, the daily and per-account limits, the author's confirmation of the terms.
        from origin.services.skills import hub  # noqa: PLC0415 - hub imports this module

        hub.require_public_gate(
            user,
            skill,
            new_public=True,
            accept_terms=getattr(fields, "accept_terms", None),
            handle=getattr(fields, "handle", None),
            display_name=getattr(fields, "display_name", None),
        )
    skill.visibility = visibility
    skill.save(update_fields=["visibility", "updated_at"])
    if visibility == VISIBILITY_PRIVATE:
        return
    cut_version(skill, user)  # None when the text is the newest version's already
    if visibility == VISIBILITY_PUBLIC:
        # The version just checked is the first a stranger may open; the team-only history before
        # it never passed the public checklist.
        skill.public_from_version_no = skill.published_version_no
        skill.save(update_fields=["public_from_version_no"])
        hold_if_flagged(skill)
        newest = skill.versions.filter(version_no=skill.published_version_no).first()
        if newest is not None:  # a version cut while team-only has no directory columns yet
            hub.refresh_listing(skill, newest)


def update(user: CustomUser, skill_id: Any, fields: Fields) -> Skill:
    """Change what is given; the owner only. This SAVES THE OWNER'S DRAFT and never cuts a version,
    whatever the visibility (MIP-2177): readers keep the published version until the owner calls
    `publish`. The one exception is an explicit change of visibility to team or public, which
    shares the skill and so publishes its current text (`_set_visibility`)."""
    require_enabled()
    with transaction.atomic():
        skill = get_editable(user.pk, skill_id, lock=True)
        if not is_owner(skill, user.pk):
            # A co-editor writes the text and the details. The name teammates type and who the
            # skill is shared with stay the owner's.
            if fields.slug is not None and fields.slug != skill.slug:
                raise SkillForbidden("Only the owner can rename this skill.")
            if fields.visibility is not None and fields.visibility != skill.visibility:
                raise SkillForbidden("Only the owner can change who this skill is shared with.")
            fields.visibility = None
            fields.slug = None
        if fields.title is not None:
            skill.title = _wrap(lambda: text.clean_title(fields.title))
        if fields.slug is not None:
            slug = _wrap(lambda: text.clean_slug(fields.slug))
            if slug != skill.slug:
                if not _slug_free(skill.team_id, skill.owner_id, slug, exclude=skill.pk):
                    raise SkillConflict(f"You already have a skill named `{slug}`.")
                skill.slug = slug
        if fields.description is not None:
            skill.description = _wrap(lambda: text.clean_description(fields.description))
        if fields.tags is not None:
            skill.tags = _wrap(lambda: text.clean_tags(fields.tags))
        if fields.argument_hint is not None:
            skill.argument_hint = _wrap(lambda: text.clean_argument_hint(fields.argument_hint))
        if fields.tool_level is not None:
            skill.tool_level = _clean_choice(fields.tool_level, _TOOL_LEVELS, "The tool level")
        if fields.effort is not None:
            skill.effort = (
                _clean_choice(fields.effort, _EFFORTS, "The effort") if fields.effort else ""
            )
        if fields.icon is not None:
            skill.icon = _clean_icon(fields.icon)
        if fields.language is not None:
            skill.language = str(fields.language or "")[:16]
        push: tuple[int, list[dict]] | None = None
        if fields.body_markdown is not None:
            markdown = _wrap(lambda: text.check_body_markdown(str(fields.body_markdown)))
            blocks = markdown_to_blocks(markdown)
            if skill.note_id is not None:
                _save_note(skill.note_id, title=skill.title, body=blocks)
                push = (skill.note_id, blocks)
        elif fields.title is not None and skill.note_id is not None:
            _save_note(skill.note_id, title=skill.title)
        try:
            with transaction.atomic():
                skill.save()
        except IntegrityError as exc:  # a racing write took the new name
            raise SkillConflict(f"You already have a skill named `{skill.slug}`.") from exc
        if fields.visibility is not None:
            _set_visibility(
                skill,
                user,
                _clean_choice(fields.visibility, _VISIBILITIES, "The visibility"),
                fields,
            )
        if push:
            transaction.on_commit(lambda: _push_to_editor(*push))
    skill.refresh_from_db()
    return skill


def publish(user: CustomUser, skill_id: Any, *, changelog: str = "") -> SkillVersion:
    """Cut a new version from the live text and make it the one readers get. The owner only; a
    private skill has no readers, so it refuses. A public skill passes the content checklist
    again: the text may have changed since it went public."""
    require_enabled()
    with transaction.atomic():
        skill = get_editable(user.pk, skill_id, lock=True)
        if skill.visibility == VISIBILITY_PRIVATE:
            raise SkillError("A private skill has no readers: share it with your team first.")
        refuse_if_moderated(skill)
        if skill.visibility == VISIBILITY_PUBLIC:
            problems = public_problems(skill, _live_blocks(skill))
            if problems:
                raise SkillError(
                    "This skill cannot be published yet: " + " ".join(problems), problems=problems
                )
            from origin.services.skills import hub  # noqa: PLC0415 - hub imports this module

            # The same gates as going public (the account may have changed since), but not the
            # terms: they were confirmed when the skill first went public.
            hub.require_public_gate(user, skill, new_public=False)
        version = cut_version(skill, user, changelog=changelog)
        if version is None:
            raise SkillConflict("Nothing changed since the last version.")
        hold_if_flagged(skill)
    return version


def delete(user: CustomUser, skill_id: Any) -> None:
    """Delete a skill, its versions, installs and stars, and its body note. The owner only."""
    require_enabled()
    with transaction.atomic():
        skill = get_owned(user.pk, skill_id, lock=True, need_team=False)
        _delete_skill_and_note(skill)


def _delete_skill_and_note(skill: Skill) -> None:
    note_id, team_id = skill.note_id, skill.team_id
    skill.delete()
    if note_id is not None:
        try:
            delete_note(team_id=team_id, family="personal", note_id=note_id, skill_body=True)
        except DeleteRefused:  # a body that is already gone is not a failure
            logger.warning("skills: body note %s was already gone", note_id)


def erase_for_user(user: CustomUser) -> None:
    """Account erasure. The person's TEAM skills move to the team owner (design section 6: the
    team keeps what it shares); every other skill of theirs goes with them (private, and a team
    skill with no live team owner to take it), and so do their stars, installs and co-editor
    rows (counters are corrected)."""
    for team_id in set(
        Skill.objects.filter(owner=user, visibility=VISIBILITY_TEAM).values_list(
            "team_id", flat=True
        )
    ):
        hand_over_team_skills(team_id, user.pk)
    drop_editor_rows(user.pk)
    for skill in Skill.objects.filter(owner=user):
        _delete_skill_and_note(skill)
    for star in SkillStar.objects.filter(user=user).select_related("skill"):
        Skill.objects.filter(pk=star.skill_id, star_count__gt=0).update(
            star_count=F("star_count") - 1
        )
    SkillStar.objects.filter(user=user).delete()
    for install in SkillInstall.objects.filter(user=user):
        Skill.objects.filter(pk=install.skill_id, install_count__gt=0).update(
            install_count=F("install_count") - 1
        )
    SkillInstall.objects.filter(user=user).delete()


# -- team sharing: co-editors, hide, hand-over ------------------------------------------------------


def editors_of(skill: Skill) -> list[CustomUser]:
    """The co-editors, by name: the Editor rows whose holders are still on the skill's team."""
    if skill.note_id is None:
        return []
    ids = list(_editor_rows(skill.note_id).values_list("user_id", flat=True))
    if not ids:
        return []
    # Who of them is on the team, in one query (the team owner counts as a member).
    on_team = {
        str(i)
        for i in TeamMembers.objects.filter(
            team_id=skill.team_id, attendee_id__in=ids, is_deleted=False
        ).values_list("attendee_id", flat=True)
    } | {
        str(i)
        for i in TeamMaster.objects.filter(team_id=skill.team_id, owner_id__in=ids).values_list(
            "owner_id", flat=True
        )
    }
    return [
        u
        for u in CustomUser.objects.filter(pk__in=ids).order_by("username")
        if str(u.pk) in on_team
    ]


def list_editors(user_id: Any, skill_id: Any) -> list[CustomUser]:
    """Who edits this skill with its owner. The owner and the co-editors may ask."""
    require_enabled()
    skill = get_readable(user_id, skill_id)
    if not (is_owner(skill, user_id) or is_editor(skill, user_id)):
        raise SkillForbidden("Only the owner and the editors can see who edits this skill.")
    return editors_of(skill)


def add_editor(user: CustomUser, skill_id: Any, target_id: Any) -> list[CustomUser]:
    """Invite a member of the skill's team to edit it. The owner only. The person must be on the
    team now (a guest is not), the owner cannot invite themself, and a skill an operator took down
    takes no new editors. Idempotent."""
    require_enabled()
    with transaction.atomic():
        skill = get_owned(user.pk, skill_id, lock=True)
        if skill.status == STATUS_TAKEN_DOWN:
            raise SkillForbidden("This skill was taken down.")
        if skill.note_id is None:
            raise SkillError("This skill has no text to edit.")
        target = CustomUser.objects.filter(pk=target_id, is_deleted=False).first()
        # Lock the person's membership row, so a removal that races this invitation either ran
        # first (and the check below refuses) or runs after (and `drop_editor_rows` ends the row).
        TeamMembers.objects.select_for_update().filter(
            team_id=skill.team_id, attendee_id=target_id
        ).first()
        # The same sentence for "no such person" and "not on this team": it must not tell which.
        if target is None or not is_team_member(skill.team_id, target.pk):
            raise SkillNotFound("That person is not on this team.")
        if is_owner(skill, target.pk):
            raise SkillError("The owner already edits this skill.")
        rows = _editor_rows(skill.note_id)
        if not rows.filter(user_id=target.pk).exists() and rows.count() >= MAX_EDITORS:
            raise SkillError(f"A skill may have up to {MAX_EDITORS} editors.")
        NotePermissionMaster.objects.update_or_create(
            user=target,
            note_type=NOTE_TYPE_PERSONAL,
            note_id=skill.note_id,
            defaults={"team_id": skill.team_id, "role_id": ROLE_EDITOR},
        )
    return editors_of(skill)


def remove_editor(user: CustomUser, skill_id: Any, target_id: Any) -> list[CustomUser]:
    """Take a co-editor's role away. The owner may remove anyone; a co-editor may remove only
    themself (leave the skill). The note's search chunks are purged: the index stamped the
    editor as a reader, and the reindex restores them with the narrower list."""
    require_enabled()
    with transaction.atomic():
        skill = get_readable(user.pk, skill_id)
        leaving = str(target_id) == str(user.pk)
        if not (is_owner(skill, user.pk) or (leaving and is_editor(skill, user.pk))):
            raise SkillForbidden("Only the owner can remove an editor.")
        skill = Skill.objects.select_for_update().get(pk=skill.pk)
        deleted, _ = _editor_rows(skill.note_id).filter(user_id=target_id).delete()
        if not deleted:
            raise SkillNotFound("That person is not an editor of this skill.")
    _search_narrowed([skill.note_id])
    return editors_of(skill)


def _search_narrowed(note_ids: list[int]) -> None:
    """Who may read these body notes got smaller: drop their chunks and touch the notes, so the
    incremental reindex restores them with the narrower ACL. Best effort; the sweep retries."""
    from origin.search_engine.purge import purge_note  # noqa: PLC0415 - search_engine is heavy

    for note_id in note_ids:
        purge_note("personal", note_id)
    PersonalNoteMaster.objects.filter(note_id__in=note_ids).update(ts_updated_at=timezone.now())


def drop_editor_rows(user_id: Any, team_id: Any = None) -> int:
    """The person left a team (or the account): end their co-editing there. `team_id=None` = every
    team. `revoke_team_note_roles` does not reach a skill's hidden folder, so this does."""
    skills = Skill.objects.filter(note_id__isnull=False)
    if team_id is not None:
        skills = skills.filter(team_id=team_id)
    rows = NotePermissionMaster.objects.filter(
        user_id=user_id,
        note_type=NOTE_TYPE_PERSONAL,
        role_id=ROLE_EDITOR,
        note_id__in=skills.values("note_id"),
    )
    note_ids = list(rows.values_list("note_id", flat=True))
    if not note_ids:
        return 0
    deleted, _ = rows.delete()
    _search_narrowed(note_ids)
    return deleted


def set_hidden(user: CustomUser, skill_id: Any, hidden: bool) -> Skill:
    """The team owner hides a team skill from the team, or shows it again. The status is
    `hidden_by_team`; it never overwrites an operator's (`reported_hidden`, `taken_down`) and an
    owner's own change of visibility does not clear it. The skill's owner and co-editors keep
    editing it; everybody else gets the "not found"."""
    require_enabled()
    with transaction.atomic():
        skill = get_readable(user.pk, skill_id)
        if is_owner(skill, user.pk):
            raise SkillForbidden("This is your own skill: make it private instead of hiding it.")
        if not is_team_owner(skill, user.pk):
            raise SkillForbidden("Only the team owner can hide a team skill.")
        skill = Skill.objects.select_for_update().get(pk=skill.pk)
        if skill.visibility != VISIBILITY_TEAM:
            raise SkillError("Only a team skill can be hidden from the team.")
        if skill.status not in (STATUS_ACTIVE, STATUS_HIDDEN_BY_TEAM):
            raise SkillConflict("An operator is looking at this skill: it cannot be changed here.")
        wanted = STATUS_HIDDEN_BY_TEAM if hidden else STATUS_ACTIVE
        if skill.status != wanted:
            skill.status = wanted
            skill.save(update_fields=["status", "updated_at"])
            logger.info("skills: team owner %s set skill %s to %s", user.pk, skill.pk, skill.status)
    return skill


def hand_over_team_skills(team_id: Any, leaver_id: Any) -> int:
    """The leaver's TEAM skills in this team move to the team owner (design section 6). Each moves
    with its body note (owner, hidden folder), its versions, stars and copies; the co-editors stay;
    the name is made free for the new owner if they already use it. The new owner's plan count is
    not checked: this is a hand-over, not a new skill. With no other live team owner there is
    nobody to take them and nothing changes (the caller then deletes). Returns how many moved."""
    team = (
        TeamMaster.objects.filter(team_id=team_id, is_deleted=False).select_related("owner").first()
    )  # a team that is closing has nobody to hand over to: its skills go with it
    heir = team.owner if team else None
    if heir is None or str(heir.pk) == str(leaver_id) or heir.is_deleted:
        return 0
    moved = 0
    with transaction.atomic():
        skills = list(
            Skill.objects.select_for_update().filter(
                team_id=team_id, owner_id=leaver_id, visibility=VISIBILITY_TEAM
            )
        )
        for skill in skills:
            if skill.note_id is not None:
                folder = _skill_folder(heir, team_id)
                NotePermissionMaster.objects.filter(
                    note_type=NOTE_TYPE_PERSONAL, note_id=skill.note_id, user_id=heir.pk
                ).delete()  # an Editor row of the heir: they own it now
                note = PersonalNoteMaster.objects.filter(note_id=skill.note_id).first()
                if note is not None:
                    note.owner = heir
                    note.folder_id = folder.folder_id
                    note.save(update_fields=["owner", "folder_id", "ts_updated_at"])
            if not _slug_free(team_id, heir.pk, skill.slug):
                skill.slug = _free_slug(team_id, heir.pk, skill.slug)
            skill.owner = heir
            fields = ["owner", "slug", "updated_at"]
            if skill.status == STATUS_HIDDEN_BY_TEAM:
                # The team owner is the one who hid it and is now its owner, who cannot show their
                # own skill again (`set_hidden`): the hide ends with the hand-over.
                skill.status = STATUS_ACTIVE
                fields.append("status")
            skill.save(update_fields=fields)
            moved += 1
        note_ids = [s.note_id for s in skills if s.note_id is not None]
        if note_ids:
            # The index stamped the old owner as the note's reader.
            transaction.on_commit(lambda: _search_narrowed(note_ids))
    return moved


def attribution(skill: Skill, user_id: Any) -> dict[str, Any] | None:
    """Where a copy came from, for the page of the copy: `None` for a skill that is not a copy (or
    whose original is gone), the original's title, author and version when the viewer may still read
    it, else `{"available": False}`: a skill that went private must not keep telling people its
    title."""
    if skill.forked_from_id is None:
        return None
    origin = (
        Skill.objects.select_related("owner", "owner__skill_profile")
        .filter(pk=skill.forked_from_id)
        .first()
    )
    out: dict[str, Any] = {"versionNo": skill.forked_from_version_no}
    if origin is None or not can_read(origin, user_id):
        return {**out, "available": False}
    version = None if reads_draft(origin, user_id) else version_for(origin, user_id)
    out.update(
        available=True,
        id=str(origin.pk),
        title=(version.title if version else origin.title),
        ownerName=(
            origin.owner.username if origin.owner_id and origin.visibility != "public" else ""
        ),
    )
    if origin.visibility == VISIBILITY_PUBLIC:
        # A public skill is credited by its public handle (never the account name); a copy keeps
        # the credit even after the original's author changes the text.
        from origin.services.skills import hub  # noqa: PLC0415 - hub imports this module

        author = hub.author_of(origin)
        if author:
            out["author"] = author
    return out


# -- reading --------------------------------------------------------------------------------------


def version_for(skill: Skill, user_id: Any) -> SkillVersion | None:
    """The version a reader gets: the published one (None when the owner has cut none yet)."""
    if skill.published_version_no <= 0:
        return None
    return skill.versions.filter(version_no=skill.published_version_no).first()


def body_markdown_for(skill: Skill, user_id: Any) -> str:
    """The owner and the co-editors read the live note; everybody else the published version."""
    if reads_draft(skill, user_id):
        return live_markdown(skill)
    version = version_for(skill, user_id)
    return version.body_markdown if version else ""


def _readable_versions(skill: Skill, user_id: Any) -> QuerySet[SkillVersion]:
    """The versions this member may open. The owner and the co-editors: all. A reader: up to the
    published one, never a draft cut ahead of it; and for a PUBLIC skill only from the version that
    passed the public checklist (`public_from_version_no`), never the team-only history before it."""
    qs = skill.versions.all()
    if reads_draft(skill, user_id):
        return qs
    qs = qs.filter(version_no__lte=skill.published_version_no)
    if skill.visibility == VISIBILITY_PUBLIC:
        qs = qs.filter(version_no__gte=skill.public_from_version_no)
    return qs


def get_version(user_id: Any, skill_id: Any, version_no: int) -> tuple[Skill, SkillVersion]:
    skill = get_readable(user_id, skill_id)
    version = _readable_versions(skill, user_id).filter(version_no=version_no).first()
    if version is None:
        raise SkillNotFound("Version not found.")
    return skill, version


def versions_of(user_id: Any, skill_id: Any) -> tuple[Skill, list[SkillVersion]]:
    skill = get_readable(user_id, skill_id)
    return skill, list(_readable_versions(skill, user_id))


TABS = ("mine", "team", "installed", "starred")


def list_for(user: CustomUser, team_id: Any, tab: str) -> QuerySet[Skill]:
    """A tab of the skills page. `mine`: the member's own in this team. `team`: teammates'
    published team skills in this team. `installed`: the public skills the member installed.
    `starred`: everything they starred that they can still read."""
    require_enabled()
    if tab not in TABS:
        raise SkillError(f"`tab` must be one of: {', '.join(TABS)}.")
    # Readers' tabs sort by when the PUBLISHED version was cut, never by `updated_at`: a draft save
    # moves that column, and a teammate must not see a skill jump to the top for an edit they
    # cannot read yet (MIP-2177). A skill with no published version (the owner's own) sorts last.
    published_at = Subquery(
        SkillVersion.objects.filter(
            skill_id=OuterRef("pk"), version_no=OuterRef("published_version_no")
        ).values("created_at")[:1]
    )
    base = Skill.objects.select_related("owner", "owner__skill_profile")
    if tab == "installed":
        return (
            base.filter(visible_to(user.pk), installs__user=user)
            .annotate(shown_at=published_at)
            .order_by(F("shown_at").desc(nulls_last=True), "-updated_at")
        )
    if tab == "starred":
        return (
            base.filter(visible_to(user.pk), stars__user=user)
            .annotate(shown_at=published_at)
            .order_by(F("shown_at").desc(nulls_last=True), "-updated_at")
        )
    _require_member(user, team_id)
    if tab == "mine":
        return base.filter(owner=user, team_id=team_id).order_by("-updated_at")
    # What the team page shows: the published team skills of teammates, plus the skills the member
    # co-edits (a private one the owner invited them to included) and, for the team owner, the team
    # skills they hid. `visible_to` already holds exactly those rules; this narrows it to the team
    # and to somebody else's skills.
    return (
        base.filter(team_id=team_id)
        .filter(visible_to(user.pk))
        .exclude(visibility=VISIBILITY_PUBLIC)
        .exclude(owner=user)
        .annotate(shown_at=published_at)
        .order_by(F("shown_at").desc(nulls_last=True), "-updated_at")
    )


# -- copy, install, star --------------------------------------------------------------------------


def copy(user: CustomUser, skill_id: Any, team_id: Any) -> Skill:
    """A new PRIVATE skill owned by the copier, in `team_id`, from what they can read. It keeps
    `forked_from` and starts at no version; it does not follow the original afterwards. Anyone
    who can read a skill can copy it (the owner included: a fork of one's own)."""
    require_enabled()
    source = get_readable(user.pk, skill_id)
    refuse_if_moderated(
        source
    )  # the owner reads a stopped skill, but cannot copy it around the stop
    if not is_owner(source, user.pk):
        if source.visibility == VISIBILITY_PRIVATE:
            # A co-editor was invited to work on it, not to take it elsewhere (design section 6).
            raise SkillForbidden("Only the owner can copy a private skill.")
        if source.status != STATUS_ACTIVE:
            raise SkillForbidden("This skill is hidden from the team.")
        if source.visibility == VISIBILITY_TEAM and str(source.team_id) != str(team_id):
            # A team's skill stays in its team: cross-team sharing is the Hub's job (public skills).
            raise SkillForbidden("A team skill can only be copied into its own team.")
    version = version_for(source, user.pk) if not is_owner(source, user.pk) else None
    if is_owner(source, user.pk):
        markdown = live_markdown(source)
        version_no = source.current_version_no or None
    elif version is not None:
        markdown = version.body_markdown
        version_no = version.version_no
    else:  # a reader of a skill with no version cannot exist (`can_read`), kept for safety
        raise SkillNotFound
    title = version.title if version else source.title
    copy_ = create(
        user,
        team_id,
        Fields(
            title=title,
            slug=None,
            description=version.description if version else source.description,
            tags=version.tags if version else source.tags,
            icon=source.icon,
            argument_hint=version.argument_hint if version else source.argument_hint,
            tool_level=version.tool_level if version else source.tool_level,
            effort=source.effort,
            language=source.language,
            body_markdown=markdown,
        ),
        forked_from=source,
        forked_from_version_no=version_no,
    )
    if not is_owner(source, user.pk):  # a fork of one's own skill is not a use by someone else
        Skill.objects.filter(pk=source.pk).update(copy_count=F("copy_count") + 1)
    return copy_


def install(user: CustomUser, skill_id: Any) -> SkillInstall:
    """Pin the published version of a PUBLIC skill into the member's list. The owner's own skill
    and a team skill are already theirs to run, so they refuse."""
    require_enabled()
    skill = get_readable(user.pk, skill_id)
    if skill.visibility != VISIBILITY_PUBLIC:
        raise SkillError("Only public skills are installed. Copy a team skill to keep your own.")
    if is_owner(skill, user.pk):
        raise SkillError("This is your own skill.")
    with transaction.atomic():
        row, created = SkillInstall.objects.get_or_create(
            user=user, skill=skill, defaults={"version_no": skill.published_version_no}
        )
        if created:
            Skill.objects.filter(pk=skill.pk).update(install_count=F("install_count") + 1)
    return row


def uninstall(user: CustomUser, skill_id: Any) -> None:
    require_enabled()
    with transaction.atomic():
        deleted, _ = SkillInstall.objects.filter(user=user, skill_id=skill_id).delete()
        if deleted:
            Skill.objects.filter(pk=skill_id, install_count__gt=0).update(
                install_count=F("install_count") - 1
            )


def accept_update(user: CustomUser, skill_id: Any) -> SkillInstall:
    """Move an install to the skill's newest published version (the member saw the diff)."""
    require_enabled()
    skill = get_readable(user.pk, skill_id)
    row = SkillInstall.objects.filter(user=user, skill=skill).first()
    if row is None:
        raise SkillNotFound("You have not installed this skill.")
    row.version_no = skill.published_version_no
    row.save(update_fields=["version_no"])
    return row


def installed_version(user_id: Any, skill: Skill) -> int | None:
    return (
        SkillInstall.objects.filter(user_id=user_id, skill=skill)
        .values_list("version_no", flat=True)
        .first()
    )


def star(user: CustomUser, skill_id: Any) -> bool:
    """Star a skill the member can read. One per account; idempotent; at most 100 a day. The star
    is always a bookmark, and it moves the PUBLIC total only when the account is at least 3 days old
    with a verified email and is not the author's (`hub.star_counts`, MIP-2047)."""
    require_enabled()
    from origin.services.skills import hub  # noqa: PLC0415 - hub imports this module

    skill = get_readable(user.pk, skill_id)
    with transaction.atomic():
        if SkillStar.objects.filter(user=user, skill=skill).exists():
            return False
        hub.require_star_room(user)
        counted = hub.star_counts(user, skill)
        try:
            with transaction.atomic():
                SkillStar.objects.create(user=user, skill=skill, counted=counted)
        except IntegrityError:  # a racing click made it first
            return False
        if counted:
            Skill.objects.filter(pk=skill.pk).update(star_count=F("star_count") + 1)
    return True


def unstar(user: CustomUser, skill_id: Any) -> None:
    require_enabled()
    with transaction.atomic():
        row = SkillStar.objects.filter(user=user, skill_id=skill_id).first()
        if row is None:
            return
        row.delete()
        if row.counted:
            Skill.objects.filter(pk=skill_id, star_count__gt=0).update(
                star_count=F("star_count") - 1
            )


def is_starred(user_id: Any, skill: Skill) -> bool:
    return SkillStar.objects.filter(user_id=user_id, skill=skill).exists()


# -- SKILL.md in and out --------------------------------------------------------------------------


def export_file(user_id: Any, skill_id: Any) -> tuple[str, str]:
    """`(slug, SKILL.md text)`: a reader gets the published version's name and words."""
    require_enabled()
    skill = get_readable(user_id, skill_id)
    version = None if reads_draft(skill, user_id) else version_for(skill, user_id)
    markdown = body_markdown_for(skill, user_id)
    description = version.description if version else skill.description
    slug = (version.slug if version else "") or skill.slug
    return slug, text.build_skill_file(slug=slug, description=description, body_markdown=markdown)


def import_file(user: CustomUser, team_id: Any, content: str, *, source: str = "user") -> Skill:
    """A new private skill from a SKILL.md file: `name` is the slug, `description` the
    description, the rest the text. The title is the first heading, else the name."""
    require_enabled()
    parsed = _wrap(lambda: text.parse_skill_file(content))
    body = parsed["body"]
    title = ""
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            break
        if stripped:
            break
    name = parsed["name"] or ""
    title = title or name.replace("-", " ").strip().capitalize() or "Imported skill"
    slug = name.strip().lower() if name else ""
    if slug:
        try:
            slug = text.clean_slug(slug)
        except text.SkillTextError:
            slug = text.slugify(slug)
        slug = _free_slug(team_id, user.pk, slug)
    return create(
        user,
        team_id,
        Fields(
            title=title[: text.TITLE_MAX],
            slug=slug or None,
            description=parsed["description"][: text.DESCRIPTION_MAX],
            body_markdown=body,
        ),
        source=source,
    )
