"""The official Genos skills (MIP-2048): the Skills v2 runbooks, translated for the in-app agent, kept
as one public skill each under a Genos system account.

`official_catalog/` holds the translated texts and `manifest.json` (written by
`scripts/build_official_skills.py` from the runbooks of the public plugin release). `sync()` makes the
database match it, and is what `manage.py sync_official_skills` runs.

* WHO OWNS THEM. A system user (`is_system_user`, no password, never signs in) that owns its own
  team. Both are made by the first applied sync. A skill needs a team and an owner (the Hub, the
  `/` picker and the version history all read them), and a team of its own keeps every official
  skill out of every customer's team. The user is found by its address; an account with that address
  that is not a system user is refused, never reused.
* WHAT IT TOUCHES. Only skills owned by that account. Nobody else's skill is read for writing, whatever
  its name: `(team, owner, slug)` is unique, and the lookup is by that owner.
* HOW A SKILL IS WRITTEN. Not through `service.create`: that path enforces a plan, a verified email,
  a handle and the checklist for a member, and refuses the `official` source. The sync writes the same
  rows with the same helpers (`_create_note`, `cut_version`), public and `is_official`, so a skill is a
  note plus a metadata row and each text is an immutable version. A scan result is kept on each
  version; a flagged official text is NOT held (it is ours), the dry run reports it so a person reads it.
* IDEMPOTENT. A skill is compared with its newest version (the same hash `cut_version` uses): the same
  text and details cut nothing and write nothing. A new release changes the closing "Source" line, so a
  new release is a new version, and an update keeps the skill's id, stars, installs and counters.
* VISIBILITY. Nothing here changes who can see a public skill: that is `GENOS_SKILLS_PUBLIC`
  (`service.can_read`, `visible_to`, the picker and the by-name lookup all check it). With the switch
  off the sync can run and no member sees an official skill.
* A skill an operator moved to review, hidden or took down (`MODERATED_STATUSES`) is left alone and
  reported: the sync never undoes a moderation decision.
* A skill that is no longer in the catalog is reported ("orphan") and left as it is.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.db import IntegrityError, transaction

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.skills.skill_models import (
    SOURCE_OFFICIAL,
    TOOL_LEVEL_ACT,
    TOOL_LEVEL_READ,
    VISIBILITY_PUBLIC,
    Skill,
)
from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.services import blind_index
from origin.services.skills import guard, service, text

logger = logging.getLogger(__name__)

CATALOG_DIR = Path(__file__).resolve().parent / "official_catalog"

OFFICIAL_EMAIL = "official-skills@genos.tech"
OFFICIAL_USERNAME = "Genos"
OFFICIAL_TEAM_NAME = "Genos official skills"
OFFICIAL_TEAM_EMAIL = "official-skills-team@genos.tech"

CREATE = "create"
UPDATE = "update"
UNCHANGED = "unchanged"
SKIPPED = "skipped"


class CatalogError(Exception):
    """The committed catalog is not valid: nothing is written."""


@dataclass(frozen=True)
class Spec:
    """One official skill as the catalog describes it."""

    slug: str
    title: str
    description: str
    tags: list[str]
    icon: str
    tool_level: str
    body_markdown: str
    release: str

    @property
    def changelog(self) -> str:
        return f"Genos skills {self.release}"


@dataclass
class Action:
    slug: str
    kind: str
    detail: str = ""
    scan_codes: list[str] = field(default_factory=list)


@dataclass
class Report:
    actions: list[Action] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    account_exists: bool = False
    applied: bool = False

    def count(self, kind: str) -> int:
        return sum(1 for a in self.actions if a.kind == kind)


# -- the catalog ----------------------------------------------------------------------------------


def load_catalog(directory: Path | None = None) -> list[Spec]:
    """Read and validate the committed catalog. Every field goes through the same rules a member's
    skill does (length, characters, the 12,000 character body), so a bad catalog fails here, before
    anything is written."""
    base = directory or CATALOG_DIR
    try:
        manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"The official skill catalog cannot be read: {exc}") from exc
    release = str(manifest.get("release") or "")
    if not release:
        raise CatalogError("The catalog names no release.")
    specs: list[Spec] = []
    seen: set[str] = set()
    for row in manifest.get("skills") or []:
        try:
            slug = text.clean_slug(row.get("slug"), official=True)
            body = text.check_body_markdown((base / f"{slug}.md").read_text(encoding="utf-8"))
            tool_level = row.get("tool_level")
            if tool_level not in (TOOL_LEVEL_READ, TOOL_LEVEL_ACT):
                raise CatalogError(f"{slug}: unknown tool level {tool_level!r}.")
            if not body.strip():
                raise CatalogError(f"{slug}: the text is empty.")
            spec = Spec(
                slug=slug,
                title=text.clean_title(row.get("title")),
                description=text.clean_description(row.get("description")),
                # Every official skill is a template (MIP-2398), first, then the manifest's own: the
                # pack tag, and `dashboard` on those that can make a dashboard (MIP-2418).
                tags=text.clean_tags([text.TEMPLATE_TAG, *(row.get("tags") or [])]),
                icon=str(row.get("icon") or "")[:16],
                tool_level=tool_level,
                body_markdown=body.strip("\n"),
                release=release,
            )
        except (text.SkillTextError, OSError) as exc:
            raise CatalogError(f"{row.get('slug')}: {exc}") from exc
        if slug in seen:
            raise CatalogError(f"{slug} appears twice in the catalog.")
        if slug not in text.OFFICIAL_SLUGS:
            raise CatalogError(f"{slug} is not in official_slugs.py: rebuild the catalog.")
        seen.add(slug)
        specs.append(spec)
    if not specs:
        raise CatalogError("The catalog holds no skills.")
    if seen != set(text.OFFICIAL_SLUGS):
        raise CatalogError("official_slugs.py and the catalog differ: rebuild the catalog.")
    return specs


# -- the account ----------------------------------------------------------------------------------


def find_account() -> tuple[CustomUser | None, TeamMaster | None]:
    """The official user and team if they exist. Refuses an account that has the official address but
    is not ours: a real person's account is never adopted."""
    user = CustomUser.objects.filter(blind_index.USER_EMAIL.matches(OFFICIAL_EMAIL)).first()
    if user is None:
        return None, None
    if not user.is_system_user:
        raise CatalogError(
            "An account that is not a Genos system account already uses the official address. "
            "Nothing was written."
        )
    # By owner, not by name: a team name is unique across every customer, so a member could take it.
    return user, TeamMaster.objects.filter(owner=user).order_by("ts_created_at").first()


def ensure_account() -> tuple[CustomUser, TeamMaster]:
    user, team = find_account()
    if user is None:
        user = CustomUser.objects.create_user(
            email=OFFICIAL_EMAIL,
            username=OFFICIAL_USERNAME,
            password=None,
            is_system_user=True,
            is_email_verified=True,
        )
    if team is None:
        name = OFFICIAL_TEAM_NAME
        if TeamMaster.objects.filter(
            team_name=name
        ).exists():  # a member took the name: still works
            name = f"{OFFICIAL_TEAM_NAME} {uuid.uuid4().hex[:6]}"
        team = TeamMaster.objects.create(team_name=name, team_email=OFFICIAL_TEAM_EMAIL, owner=user)
    return user, team


# -- compare and write ----------------------------------------------------------------------------


def _fields(spec: Spec) -> dict[str, Any]:
    """The versioned metadata of a skill (`service._VERSIONED_FIELDS`) as the catalog gives it."""
    return {
        "slug": spec.slug,
        "title": spec.title,
        "description": spec.description,
        "tags": spec.tags,
        "icon": spec.icon,
        "tool_level": spec.tool_level,
        "argument_hint": "",
        "effort": "",
        "language": "",
    }


def _digest(spec: Spec, blocks: list[dict]) -> str:
    meta = {name: _fields(spec)[name] for name in service._VERSIONED_FIELDS}
    return service._hash(blocks, meta)


def _scan_codes(spec: Spec) -> list[str]:
    """What the publish-time scan finds in the text itself. The closing "Source" line is ours and
    always an external link, so it is left out: a code reported here is a line a person should read."""
    body, _, last = spec.body_markdown.rpartition("\nSource: [")
    text_only = body if last and "\n" not in last else spec.body_markdown
    return list(guard.scan(text_only, extra=(spec.title, spec.description)).codes)


def _classify(spec: Spec, existing: Skill | None, taken: frozenset[str] = frozenset()) -> Action:
    codes = _scan_codes(spec)
    if existing is None and spec.slug in taken:
        # MIP-2210: a public name is unique across all public skills. A member published it first, so
        # the official skill waits for that name; the member's skill is never touched.
        return Action(spec.slug, SKIPPED, "a member's public skill already uses this name", codes)
    if existing is None:
        return Action(spec.slug, CREATE, f"new, {spec.release}", codes)
    if existing.status in service.MODERATED_STATUSES:
        return Action(spec.slug, SKIPPED, f"an operator set it to {existing.status}", codes)
    newest = existing.versions.order_by("-version_no").first()
    blocks = markdown_to_blocks(spec.body_markdown)
    same = (
        newest is not None
        and newest.content_hash == _digest(spec, blocks)
        and existing.visibility == VISIBILITY_PUBLIC
        and existing.is_official
        and existing.published_version_no == newest.version_no
    )
    if same:
        return Action(spec.slug, UNCHANGED, f"v{existing.published_version_no}", codes)
    return Action(spec.slug, UPDATE, f"v{existing.published_version_no} -> new text", codes)


def _write(spec: Spec, user: CustomUser, team: TeamMaster, existing: Skill | None) -> None:
    blocks = markdown_to_blocks(spec.body_markdown)
    fields = _fields(spec)
    with transaction.atomic():
        service._check_tags("ensure", team.team_id, user.pk, list(spec.tags))
        if existing is None:
            note = service._create_note(user, team.team_id, spec.title, blocks)
            skill = Skill.objects.create(
                team=team,
                owner=user,
                note_id=note.note_id,
                source=SOURCE_OFFICIAL,
                is_official=True,
                visibility=VISIBILITY_PUBLIC,
                **fields,
            )
        else:
            skill = Skill.objects.select_for_update().get(pk=existing.pk)
            if skill.status in service.MODERATED_STATUSES:
                return  # an operator acted since the dry look: that decision stands
            if skill.note_id is not None:
                service._save_note(skill.note_id, title=spec.title, body=blocks)
            for name, value in fields.items():
                setattr(skill, name, value)
            skill.is_official = True
            skill.source = SOURCE_OFFICIAL
            skill.visibility = VISIBILITY_PUBLIC
            skill.save()
        version = service.cut_version(skill, user, changelog=spec.changelog)
        if version is not None:
            # The first version a stranger may open: our own text, written for everyone.
            skill.public_from_version_no = skill.public_from_version_no or version.version_no
            skill.save(update_fields=["public_from_version_no"])


def sync(*, apply: bool = False, only: set[str] | None = None) -> Report:
    """Make the database match the catalog. Without `apply` nothing is written, not even the account."""
    specs = load_catalog()
    if only:
        unknown = only - {s.slug for s in specs}
        if unknown:
            raise CatalogError(f"Not official skills: {', '.join(sorted(unknown))}.")
        specs = [s for s in specs if s.slug in only]
    user, team = find_account()
    report = Report(account_exists=user is not None)
    if apply:
        with transaction.atomic():
            user, team = ensure_account()
    existing: dict[str, Skill] = {}
    if user is not None and team is not None:
        existing = {s.slug: s for s in Skill.objects.filter(owner=user, team=team)}
    # Public names held by anyone else: a second public skill with the same name is refused by the
    # database (`skill_slug_unique_public`), so the sync skips such a name and says so.
    taken = frozenset(
        Skill.objects.filter(visibility=VISIBILITY_PUBLIC, slug__in=[s.slug for s in specs])
        .exclude(pk__in=[s.pk for s in existing.values()])
        .values_list("slug", flat=True)
    )
    for spec in specs:
        action = _classify(spec, existing.get(spec.slug), taken)
        if apply and action.kind in (CREATE, UPDATE):
            assert user is not None and team is not None
            try:
                _write(spec, user, team, existing.get(spec.slug))
            except IntegrityError:
                # A member published the same name between the check and the write.
                if existing.get(spec.slug) is not None:
                    raise
                detail = "a public skill took this name just now"
                action = Action(spec.slug, SKIPPED, detail, action.scan_codes)
        report.actions.append(action)
    if not only:
        wanted = {s.slug for s in specs}
        report.orphans = sorted(set(existing) - wanted)
    report.applied = apply
    return report
