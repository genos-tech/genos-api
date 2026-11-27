"""The dashboards a member can open from Task Stats > "Custom dashboards by AI" (MIP-2356).

A dashboard is the living HTML note an `ai_dashboard` schedule keeps (MIP-2353): the note's id is
in `MemberSchedule.output`, an index row nobody but the schedule's owner can write. This module
lists two groups for one member in one team:

* `own`: the member's own `ai_dashboard` schedules, with their note when it exists and the member
  can still open it (a schedule that has not run yet has no note: `note` is None, so the page can
  offer "Run now" and nothing else).
* `shared`: dashboards of OTHER members' schedules whose note this member may read. The schedule
  itself stays private: only the note's id and title and its owner's display name are returned,
  and only after the note ACL (`may_read_note`, the rule every note read uses) said yes. A note
  that is missing, in another team, not an HTML page or not readable never appears, and nothing
  tells it apart from a note that never existed.

Reads only. No AI call, no write.
"""

from __future__ import annotations

from typing import Any

from origin.models.common.schedule_models import MemberSchedule
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.search_engine.agent.acl import may_read_note
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.note_context import _root_folder_ids
from origin.views.utils.note_folder_role import readable_team_folder_roles
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL

JOB = "ai_dashboard"
NOTE_TYPE = "personal"
# The most schedules looked at for the shared group, newest first: a bound on the ACL checks
# (one role lookup each), not a feature limit anyone is expected to reach.
MAX_SHARED_CANDIDATES = 100
MAX_SHARED = 50


def _note_id(sched: MemberSchedule) -> int | None:
    out = sched.output or {}
    note_id = out.get("note_id")
    if out.get("target") != "note" or isinstance(note_id, bool) or not isinstance(note_id, int):
        return None
    return note_id


def _html_notes(team_id: str, note_ids: set[int]) -> dict[int, PersonalNoteMaster]:
    if not note_ids:
        return {}
    rows = PersonalNoteMaster.objects.filter(
        note_id__in=note_ids, team_id=team_id, body_format="html"
    ).only("note_id", "title", "owner_id", "team_id", "body_format", "folder_id", "parent_note_id")
    return {n.note_id: n for n in rows}


def team_space_note_ids(user_id: str, team_id: str, notes: list[PersonalNoteMaster]) -> set[int]:
    """Of these notes, the ones in a team folder this member can open (MIP-2408). The app shows
    such a note in Team Notes, so its address is `/workspace/notes/team/<id>`; through the `my` or
    `shared` address the page still opens, but the sidebar does not show where it is. A note under
    another note sits where its root note sits."""
    folder_of = _root_folder_ids(notes)
    if not folder_of:
        return set()
    reachable = readable_team_folder_roles(user_id, team_id)
    return {note_id for note_id, folder_id in folder_of.items() if folder_id in reachable}


def in_team_space(user_id: str, team_id: str, note_id: int) -> bool:
    """`team_space_note_ids` for one note, by id."""
    notes = list(_html_notes(team_id, {note_id}).values())
    return bool(team_space_note_ids(user_id, team_id, notes))


def _readable(user_id: str, note: PersonalNoteMaster | None) -> bool:
    return note is not None and may_read_note(
        user_id=user_id,
        note_type_code=NOTE_TYPE_PERSONAL,
        note_id=note.note_id,
        owner_id=note.owner_id,
    )


def list_dashboards(ctx: ToolContext) -> dict[str, list[dict[str, Any]]]:
    own_rows = list(
        MemberSchedule.objects.filter(user_id=ctx.user_id, team_id=ctx.team_id, job=JOB).order_by(
            "ts_created_at"
        )
    )
    other_rows = list(
        MemberSchedule.objects.filter(team_id=ctx.team_id, job=JOB)
        .exclude(user_id=ctx.user_id)
        .select_related("user")
        .order_by("-ts_updated_at")[:MAX_SHARED_CANDIDATES]
    )
    notes = _html_notes(
        ctx.team_id,
        {i for i in (_note_id(s) for s in [*own_rows, *other_rows]) if i is not None},
    )

    own: list[dict[str, Any]] = []
    for sched in own_rows:
        note = notes.get(_note_id(sched) or -1)
        own.append(
            {
                "schedule": sched,
                "note": note if _readable(ctx.user_id, note) else None,
            }
        )

    shared: list[dict[str, Any]] = []
    seen: set[int] = set()
    for sched in other_rows:
        note_id = _note_id(sched)
        if note_id is None or note_id in seen:
            continue
        note = notes.get(note_id)
        if not _readable(ctx.user_id, note):
            continue
        seen.add(note_id)
        shared.append({"note": note, "owner": sched.user})
        if len(shared) >= MAX_SHARED:
            break
    in_team = team_space_note_ids(
        ctx.user_id, ctx.team_id, [r["note"] for r in [*own, *shared] if r["note"] is not None]
    )
    for row in [*own, *shared]:
        row["in_team_folder"] = row["note"] is not None and row["note"].note_id in in_team
    return {"own": own, "shared": shared}
