"""Task tags for the agent tools (MIP-707).

A task's `tags` column stores `[{tagName, tagColor, tagTextColor}]`,
copied from its project's `ProjectTags` catalogue when a user picks a
tag in the UI. The agent tools take plain tag NAMES and copy the colours
across the same way, so an agent-set tag renders identically to a
hand-picked one.

A name the catalogue doesn't have is refused, never created: the
catalogue is the project's vocabulary, curated in project settings, and
an agent minting tags would litter it. The model picks from
`list_project_tags` or leaves the task untagged.

The `tags_changed` history row needs nothing here — `task_signals`
diffs `tags` on every save, as it does for a UI edit.
"""

from __future__ import annotations

from typing import Any

from origin.models.project.prj_models import ProjectTags
from origin.search_engine.agent.tools.base import ToolError


def project_tag_catalogue(project_id: int | None) -> list[dict[str, str]]:
    """The project's tags in the stored shape, newest first — the order
    the REST tag list (`ProjectTagsView.get`) returns them in."""
    if not project_id:
        return []
    rows = (
        ProjectTags.objects.filter(project_id=project_id)
        .order_by("-ts_updated_at")
        .values("tag_name", "tag_color", "tag_text_color")
    )
    return [
        {
            "tagName": row["tag_name"],
            "tagColor": row["tag_color"],
            "tagTextColor": row["tag_text_color"],
        }
        for row in rows
    ]


def resolve_tags(
    raw: Any, project_id: int | None, *, where: str = "`tags`"
) -> list[dict[str, str]]:
    """Turn a list of tag names into stored tag objects from the project's
    catalogue. `[]` clears. An exact name wins; otherwise a
    case-insensitive match is accepted when it picks out exactly one tag.
    Raises ToolError naming every unknown tag and the valid choices."""
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise ToolError(f"{where} must be a list of tag names (got {raw!r}).")
    catalogue = project_tag_catalogue(project_id)
    by_name = {tag["tagName"]: tag for tag in catalogue}
    by_folded: dict[str, list[dict[str, str]]] = {}
    for tag in catalogue:
        by_folded.setdefault(tag["tagName"].casefold(), []).append(tag)

    out: list[dict[str, str]] = []
    unknown: list[str] = []
    for name in raw:
        name = name.strip()
        if not name:
            continue
        tag = by_name.get(name)
        if tag is None:
            folded = by_folded.get(name.casefold()) or []
            tag = folded[0] if len(folded) == 1 else None
        if tag is None:
            unknown.append(name)
        elif tag not in out:
            out.append(tag)

    if unknown:
        choices = ", ".join(repr(tag["tagName"]) for tag in catalogue) or "none"
        raise ToolError(
            f"{where}: unknown tag(s) {', '.join(repr(n) for n in unknown)} — tags must "
            f"already exist on the project. This project's tags: {choices}. Pick from "
            "those, or leave the task untagged."
        )
    return out


def tag_names(tags: Any) -> list[str]:
    """The names in a stored `tags` value, for read payloads."""
    if not isinstance(tags, list):
        return []
    return [str(tag.get("tagName")) for tag in tags if isinstance(tag, dict) and tag.get("tagName")]
