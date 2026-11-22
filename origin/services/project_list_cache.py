"""Cache keys of the project list (`ProjectsView`), one place so an invalidation cannot miss one.

The list is cached per viewer in two variants: the default (archived projects hidden) and
`include_archived=true` (MIP-1693). Every invalidation clears both (MIP-1886).
"""

from __future__ import annotations


def project_list_keys(team_id, attendee_id) -> list[str]:
    base = f"project:list:{team_id}:{attendee_id}"
    return [base, f"{base}:archived"]


def project_list_key(team_id, attendee_id, *, include_archived: bool) -> str:
    return project_list_keys(team_id, attendee_id)[1 if include_archived else 0]
