"""The ONE batch rule for "which of these notes can this member open now" (MIP-1281, MIP-1475).

Used by the timeline (`timeline.readable_notes`) and by the item lines (`item_lines`), so that a
note a member can open on one surface is a note they can open on the other. The per-note rule it
equals is `get_note`'s (`load_readable_note`: `may_read_note` / `get_effective_role`); the sets it
is built from are `note_role.readable_note_ids` and `channel_scope.chat_note_member_q`. A test pins
it equal to the per-note rule over a generated matrix (`test_timeline_note_rule`).
"""

from __future__ import annotations

from collections import defaultdict

from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, readable_note_ids

NOTE_MODELS = {1: PersonalNoteMaster, 2: TaskNoteMaster, 3: ChatNoteMaster}


def readable_note_rows(ctx: ToolContext, keys) -> dict[tuple[int, int], object]:
    """`{(note_type, note_id): note row}` for the notes among `keys` the member can open now: ONE
    rule, the one `get_note` applies (`load_readable_note`: the note is in this team, and the
    member owns it or holds a role on it: `may_read_note` / `get_effective_role`), in a fixed
    number of queries however many notes. A test pins it equal to the per-note rule over a
    generated matrix. The rows carry `note_id`, `owner_id` and `title` only."""
    me = str(ctx.user_id)
    by_type: dict[int, set[int]] = defaultdict(set)
    for note_type, note_id in keys:
        if note_type in NOTE_MODELS:
            by_type[note_type].add(int(note_id))
    out: dict[tuple[int, int], object] = {}
    for note_type, ids in by_type.items():
        rows = {
            row.note_id: row
            for row in NOTE_MODELS[note_type]
            .objects.filter(note_id__in=ids, team_id=ctx.team_id)
            .only("note_id", "owner_id", "title")
        }
        # Only a personal note has an author exception: a chat note's (MIP-1492) or a task note's
        # (MIP-1511) author who left is a stranger.
        owned = (
            {nid for nid, row in rows.items() if row.owner_id and str(row.owner_id) == me}
            if note_type == NOTE_TYPE_PERSONAL
            else set()
        )
        allowed = owned | readable_note_ids(me, note_type, set(rows) - owned)
        out.update({(note_type, n): rows[n] for n in allowed})
    return out


def readable_notes(ctx: ToolContext, keys) -> set[tuple[int, int]]:
    """The `(note_type, note_id)` among `keys` the member can open now (`readable_note_rows`
    without the rows)."""
    return set(readable_note_rows(ctx, keys))
