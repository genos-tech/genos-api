"""The skill tag catalog (MIP-2211): pre-defined chips a member picks in the skill editor, created,
renamed, recoloured and deleted in Manage tags, like to-do tags (`ToDoCategory`).

Scope: per owner and team (the same as to-do tags). A skill's tags are NAMES in its OWNER's
catalog, so a co-editor picks from the owner's list and a teammate reading a team skill sees the
owner's colours. There is no team-wide catalog: a team tag vocabulary is the Hub's business.

What stays as it was: `Skill.tags`, `SkillVersion.tags` and `Skill.hub_tags` are plain name lists.
A published version is immutable, so a rename or a delete rewrites only the owner's DRAFT tags;
the published version keeps the old name (shown as a grey chip) until the next publish. The Hub
filters on `hub_tags` names exactly as before.

Strict or ensure. The editor's REST writes are strict: a name that is not in the catalog is
refused with the list of names that are, so free text cannot come back through the API. Paths
that are not the editor (a copy, a hand-over, the official sync) ENSURE the names instead: they
add a missing name with a neutral colour, like the agent's by-name to-do tag.

Locking: every write that touches a catalog first locks the OWNER's user row (the same lock
`service.create` takes), then the skills, so a rename and a skill save cannot deadlock.
"""

from __future__ import annotations

import re
import zlib
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from origin.models.common.user_models import CustomUser
from origin.models.skills.skill_models import Skill, SkillTag
from origin.services.skills import text
from origin.services.skills.service import SkillError
from origin.views.utils.scope_guards import is_team_member

# The tag palette of the app (`TagColorPickerMenu`): hex values; a text colour that reads on each is
# chosen the way the frontend does (`getContrastTextColor`: black on light, white on dark).
PALETTE: tuple[str, ...] = (
    "#ff2323",
    "#1dc200",
    "#0044c2",
    "#ffd93b",
    "#ffa823",
    "#8e23ff",
    "#ff238a",
    "#808080",
    "#008080",
    "#4b0082",
    "#8b4513",
    "#2e8b57",
    "#4682b4",
    "#6a5acd",
)
NEUTRAL_COLOR = "#808080"
NEUTRAL_TEXT = "white"
# The built-in `template` tag (MIP-2398): a fixed slate-blue chip for every reader. An official
# skill's other chips are grey to a stranger (a personal palette would link an author's skills
# together); this one belongs to no author, so it keeps its colour.
TEMPLATE_TAG = text.TEMPLATE_TAG
TEMPLATE_COLOR = "#6a5acd"
TEMPLATE_TEXT = "white"

_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_TEXT_COLORS = {"black", "white"}


def contrast_text(color: str) -> str:
    """`black` on a light chip, `white` on a dark one (relative luminance, like the frontend)."""
    h = color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return "black" if (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.6 else "white"


def default_color_for(name: str) -> tuple[str, str]:
    """A stable palette colour for a name (the data migration and nothing random: `hash()` changes
    between runs)."""
    color = PALETTE[zlib.crc32(name.encode("utf-8")) % len(PALETTE)]
    return color, contrast_text(color)


class TagError(SkillError):
    """A refusal about a tag, worded for the person; the view maps `status` like any SkillError."""

    def __init__(self, message: str, status: int | None = None, **extra: Any) -> None:
        super().__init__(message, **extra)
        if status is not None:
            self.status = status


def clean_name(value: Any) -> str:
    """One tag name under the skill tag rules (lower case, a-z0-9 and hyphens, 24 characters)."""
    try:
        names = text.clean_tags([value])
    except text.SkillTextError as exc:
        raise TagError(str(exc)) from exc
    if not names:
        raise TagError("A tag needs a name.")
    if names[0] == TEMPLATE_TAG:
        raise TagError(
            f'"{TEMPLATE_TAG}" is a built-in tag of the official skills. Pick another name.'
        )
    return names[0]


def clean_colors(data: dict[str, Any]) -> dict[str, str]:
    """The `color` / `text_color` keys of a write, validated; an absent key is left out."""
    out: dict[str, str] = {}
    if "color" in data:
        color = data.get("color")
        if not isinstance(color, str) or not _HEX_COLOR.match(color.strip()):
            raise TagError("color must be a hex value like #1dc200.")
        out["color"] = color.strip().lower()
    if "text_color" in data:
        raw = data.get("text_color")
        tc = raw.strip().lower() if isinstance(raw, str) else None
        if tc not in _TEXT_COLORS and not (tc and _HEX_COLOR.match(tc)):
            raise TagError("text_color must be 'black', 'white' or a hex value.")
        out["text_color"] = tc  # type: ignore[assignment]
    return out


# -- reading --------------------------------------------------------------------------------------


def catalog(team_id: Any, owner_id: Any):
    return SkillTag.objects.filter(team_id=team_id, owner_id=owner_id).order_by(
        "sort_order", "tag_id"
    )


def color_map(rows: list[Skill], viewer_id: Any, editing_note_ids: set[Any] | None = None):
    """`{(team_id, owner_id): {name: (color, text_color)}}` for the rows whose chips may carry the
    owner's colours, in ONE query. A public skill read by anyone but its owner or an editor gets
    none: a personal palette would link an author's skills together (the card hides `teamId` for
    the same reason). Such a row's chips are grey."""
    editing = editing_note_ids or set()
    keys = {
        (str(s.team_id), str(s.owner_id))
        for s in rows
        if s.visibility != "public" or str(s.owner_id) == str(viewer_id) or s.note_id in editing
    }
    if not keys:
        return {}
    qs = SkillTag.objects.filter(
        owner_id__in={o for _, o in keys}, team_id__in={t for t, _ in keys}
    ).values_list("team_id", "owner_id", "name", "color", "text_color")
    out: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
    for team_id, owner_id, name, color, text_color in qs:
        key = (str(team_id), str(owner_id))
        if key in keys:
            out.setdefault(key, {})[name] = (color, text_color)
    return out


def chips(
    names: list[str], palette: dict[str, tuple[str, str]] | None, *, coloured: bool
) -> list[dict[str, str]]:
    """The chips of a tag list: the owner's colour when the reader may see it and the name is in
    the catalog, else neutral grey (a name the owner deleted or renamed since the version was cut,
    a public skill, an official one)."""
    out = []
    for name in names:
        if name == TEMPLATE_TAG:
            out.append({"name": name, "color": TEMPLATE_COLOR, "textColor": TEMPLATE_TEXT})
            continue
        color, text_color = (palette or {}).get(name, (NEUTRAL_COLOR, NEUTRAL_TEXT))
        if not coloured:
            color, text_color = NEUTRAL_COLOR, NEUTRAL_TEXT
        out.append({"name": name, "color": color, "textColor": text_color})
    return out


# -- the strict check and the ensure ---------------------------------------------------------------


def require_known(team_id: Any, owner_id: Any, names: list[str]) -> None:
    """Refuse a name that is not in the owner's catalog (the editor can only pick)."""
    if not names:
        return
    have = set(
        SkillTag.objects.filter(team_id=team_id, owner_id=owner_id, name__in=names).values_list(
            "name", flat=True
        )
    )
    missing = [n for n in names if n not in have]
    if missing:
        allowed = list(catalog(team_id, owner_id).values_list("name", flat=True))
        raise TagError(
            "Pick tags from your tag list (Manage tags creates them): "
            + ", ".join(missing)
            + " "
            + ("is" if len(missing) == 1 else "are")
            + " not in it.",
            status=400,
            missing=missing,
            allowed=allowed,
        )


def ensure(
    team_id: Any,
    owner_id: Any,
    names: list[str],
    *,
    colors_from: tuple[Any, Any] | None = None,
) -> None:
    """Add the names the owner's catalog lacks. A copied or handed-over skill keeps the colour
    the SOURCE catalog (`colors_from=(team_id, owner_id)`) gave a name, else a palette colour
    chosen from the name, so the chips do not all turn grey. The built-in `template` tag is never a
    catalog row: it is left out here, so no path (a copy, a hand-over, the official sync) makes one."""
    names = [n for n in names if n != TEMPLATE_TAG]
    if not names:
        return
    have = set(
        SkillTag.objects.filter(team_id=team_id, owner_id=owner_id, name__in=names).values_list(
            "name", flat=True
        )
    )
    missing = [n for n in names if n not in have]
    if not missing:
        return
    source: dict[str, tuple[str, str]] = {}
    if colors_from is not None:
        source = {
            n: (c, t)
            for n, c, t in SkillTag.objects.filter(
                team_id=colors_from[0], owner_id=colors_from[1], name__in=missing
            ).values_list("name", "color", "text_color")
        }
    top = (
        SkillTag.objects.filter(team_id=team_id, owner_id=owner_id)
        .order_by("-sort_order")
        .values_list("sort_order", flat=True)
        .first()
    )
    order = (top or 0) + 1
    for name in missing:
        color, text_color = source.get(name) or default_color_for(name)
        try:
            with transaction.atomic():
                SkillTag.objects.create(
                    team_id=team_id,
                    owner_id=owner_id,
                    name=name,
                    color=color,
                    text_color=text_color,
                    sort_order=order,
                )
        except IntegrityError:  # a racing ensure made it first
            continue
        order += 1


# -- the catalog's own writes ---------------------------------------------------------------------


def lock_owner(owner_id: Any) -> None:
    """Take the owner's user row lock: the order every tag write and a skill save with tags use."""
    CustomUser.objects.select_for_update().filter(pk=owner_id).first()


def lock_owner_of_skill(skill_id: Any) -> None:
    """Lock the owner of a skill given by id, if there is one (a bad id is left to the caller's own
    not-found)."""
    try:
        owner_id = Skill.objects.filter(pk=skill_id).values_list("owner_id", flat=True).first()
    except (ValidationError, ValueError):
        return
    if owner_id is not None:
        lock_owner(owner_id)


def create_tag(user: CustomUser, team_id: Any, name: Any, data: dict[str, Any]) -> SkillTag:
    clean = clean_name(name)
    colors = clean_colors(data)
    if "color" in colors and "text_color" not in colors:
        colors["text_color"] = contrast_text(colors["color"])
    if "color" not in colors:
        colors.update(zip(("color", "text_color"), default_color_for(clean), strict=True))
    with transaction.atomic():
        lock_owner(user.pk)
        if SkillTag.objects.filter(team_id=team_id, owner_id=user.pk, name=clean).exists():
            raise TagError("You already have a tag with that name.", status=409)
        top = (
            SkillTag.objects.filter(team_id=team_id, owner_id=user.pk)
            .order_by("-sort_order")
            .values_list("sort_order", flat=True)
            .first()
        )
        try:
            with transaction.atomic():
                return SkillTag.objects.create(
                    team_id=team_id,
                    owner_id=user.pk,
                    name=clean,
                    sort_order=(top or 0) + 1,
                    **colors,
                )
        except IntegrityError as exc:
            raise TagError("You already have a tag with that name.", status=409) from exc


def _rewrite(tag: SkillTag, old: str, new: str | None) -> int:
    """Replace `old` by `new` (or drop it) in the owner's DRAFT tag lists, keeping order and
    never duplicating. Published versions and the Hub columns are not touched."""
    changed = 0
    skills = Skill.objects.select_for_update().filter(
        team_id=tag.team_id, owner_id=tag.owner_id, tags__contains=[old]
    )
    for skill in skills:
        out: list[str] = []
        for n in skill.tags or []:
            n = new if n == old else n
            if n and n not in out:
                out.append(n)
        skill.tags = out
        skill.save(update_fields=["tags", "updated_at"])
        changed += 1
    return changed


SORT_LIMIT = 1_000_000  # well inside the integer column


def _own_tag(tag_id: Any, user: CustomUser) -> SkillTag:
    """The caller's own tag, locked, while they are still on its team (a member who left must not
    rewrite the drafts of the skills they still own there). Anything else is a 404."""
    tag = SkillTag.objects.select_for_update().filter(tag_id=tag_id, owner_id=user.pk).first()
    if tag is None or not is_team_member(tag.team_id, user.pk):
        raise TagError("Tag not found.", status=404)
    return tag


def update_tag(tag_id: Any, user: CustomUser, data: dict[str, Any]) -> SkillTag:
    colors = clean_colors(data)
    with transaction.atomic():
        lock_owner(user.pk)
        tag = _own_tag(tag_id, user)
        if "color" in colors and "text_color" not in colors:
            colors["text_color"] = contrast_text(colors["color"])
        old = tag.name
        if "name" in data:
            new = clean_name(data.get("name"))
            if new != old:
                if (
                    SkillTag.objects.filter(team_id=tag.team_id, owner_id=tag.owner_id, name=new)
                    .exclude(tag_id=tag.tag_id)
                    .exists()
                ):
                    raise TagError("You already have a tag with that name.", status=409)
                tag.name = new
                _rewrite(tag, old, new)
        for field, value in colors.items():
            setattr(tag, field, value)
        if "sort_order" in data:
            try:
                order = int(data.get("sort_order") or 0)
            except (TypeError, ValueError) as exc:
                raise TagError("sort_order must be a number.") from exc
            tag.sort_order = max(-SORT_LIMIT, min(SORT_LIMIT, order))
        tag.save()
        return tag


def delete_tag(tag_id: Any, user: CustomUser) -> None:
    with transaction.atomic():
        lock_owner(user.pk)
        tag = _own_tag(tag_id, user)
        _rewrite(tag, tag.name, None)
        tag.delete()
