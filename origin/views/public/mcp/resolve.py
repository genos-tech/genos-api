"""Turn however a human named a task or a note into the ids tools take.

Genos's own tools take `task_id` as an integer, which is right for an
LLM that just read the id out of a search result. It is wrong for this
surface, where the reference is typed by a person: they say "work on
PRJ-123", or paste the URL out of their browser. Numeric ids are the one
form nobody has to hand.

So the MCP task tools take a string reference and it is resolved here,
before the underlying tool ever sees it. Four forms, all of which a
person plausibly produces:

    PRJ-123                                          the display id
    https://app.genosai.dev/workspace/tasks/…/task/7 a pasted URL
    #7  /  7                                         the raw id

Resolution is team-scoped: `PRJ-123` is only unique within a team, and a
reference that resolved across teams would be a quiet ACL hole. Numeric
ids skip the lookup and go straight to the tool, which enforces its own
team check anyway.

**Notes need a second argument, not just a second resolver.** A note id
is only unique *within* its note family — the three note tables mint ids
independently, so id 12 is a personal note, a task note and a chat note
all at once. Every note tool therefore takes `note_type` beside
`note_id`, and neither alone identifies a note.

That makes a pasted note URL worth more here than a task URL is: the
note URL encodes the family in its path, so one paste answers both
arguments at once.

    /workspace/notes/my/12                      personal note 12
    /workspace/notes/task/project/5/task/9/note/12   task note 12
    /workspace/notes/chat/dm/<uuid>/thread/3/note/12 chat note 12

`resolve_note_ref` returns both halves, and the caller only overrides a
`note_type` the URL actually determined — a bare id leaves whatever the
model passed alone, so the tool's own validation still produces its own
error message rather than one invented here.
"""

from __future__ import annotations

import re

from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolError

# `/workspace/tasks/project/5/task/42`, with or without a trailing
# `/comment/3`. Anchored on `/task/` so the project id can't be mistaken
# for the task id — they are adjacent and both bare integers.
_URL_TASK = re.compile(r"/task/(\d+)")
# `PRJ-123`: a project code, then the per-project number. Split on the
# LAST hyphen, since codes may contain one.
_DISPLAY_ID = re.compile(r"^(?P<code>[A-Za-z0-9_\-]+)-(?P<number>\d+)$")


def resolve_task_ref(ref, team_id: str) -> int:
    """`ref` -> a task id, or `ToolError` naming what we tried.

    The error text is written for a model to act on, not a human to
    debug: it says which forms are accepted, so a wrong guess can be
    corrected on the next call instead of ending the turn.
    """
    if isinstance(ref, int):
        return ref
    if ref is None:
        raise ToolError("A task reference is required (e.g. PRJ-123, a task URL, or an id).")

    text = str(ref).strip()
    if not text:
        raise ToolError("A task reference is required (e.g. PRJ-123, a task URL, or an id).")

    url_match = _URL_TASK.search(text)
    if url_match:
        return int(url_match.group(1))

    if text.startswith("#"):
        text = text[1:].strip()

    if text.isdigit():
        return int(text)

    display = _DISPLAY_ID.match(text)
    if display:
        code = display.group("code")
        number = int(display.group("number"))
        task_id = (
            TaskMaster.objects.filter(
                team_id=team_id,
                project__code__iexact=code,
                project_task_number=number,
                is_deleted=False,
            )
            .values_list("task_id", flat=True)
            .first()
        )
        if task_id is None:
            raise ToolError(
                f"No task {text} in this team. Check the project code, or find "
                f"the task with `list_tasks` or `search_workspace`."
            )
        return task_id

    raise ToolError(
        f"Could not read {ref!r} as a task. Use a display id (PRJ-123), a task "
        f"URL, or a numeric id."
    )


# `/workspace/notes/<bucket>/…/note/12`, or `/workspace/notes/my/12` for a
# personal note, which has no trailing `/note/` segment. Anchored on the
# LAST id in the path so the project / task / thread ids a task- or
# chat-note URL carries can't be mistaken for the note id.
_URL_NOTE_TRAILING = re.compile(r"/notes?/.*/note/(\d+)")
_URL_NOTE_PERSONAL = re.compile(r"/notes?/(?:my|team|shared)/(\d+)")
# Which family a note URL's bucket segment names. `my`, `team` and
# `shared` are all PersonalNoteMaster rows — the three are sidebar
# buckets, not note tables (see the frontend's activity router) — so all
# three resolve to `personal`, the `note_type` the tools accept.
_URL_NOTE_BUCKET = re.compile(r"/notes?/(my|team|shared|task|chat)\b")
_BUCKET_TO_NOTE_TYPE = {
    "my": "personal",
    "team": "personal",
    "shared": "personal",
    "task": "task",
    "chat": "chat",
}


def resolve_note_ref(ref, _team_id: str) -> tuple[int, str | None]:
    """`ref` -> `(note_id, note_type or None)`.

    `note_type` is None whenever the reference did not determine one — a
    bare numeric id, or a URL shaped unlike any we know. The caller must
    leave the model's own `note_type` argument untouched in that case:
    guessing a family here would turn "I passed the wrong type" into a
    confident write against the wrong table.

    Unlike `resolve_task_ref` this runs no query, so it takes no team
    scope — there is no display-id form for notes to look up, and the
    tool's own tenant guard is what proves the note belongs to the team.
    The argument is kept so both resolvers read the same at the call site.
    """
    if isinstance(ref, int):
        return ref, None
    if ref is None:
        raise ToolError("A note reference is required (a note URL or a numeric note id).")

    text = str(ref).strip()
    if not text:
        raise ToolError("A note reference is required (a note URL or a numeric note id).")

    url_match = _URL_NOTE_TRAILING.search(text) or _URL_NOTE_PERSONAL.search(text)
    if url_match:
        note_type = None
        bucket = _URL_NOTE_BUCKET.search(text)
        if bucket:
            note_type = _BUCKET_TO_NOTE_TYPE.get(bucket.group(1))
        return int(url_match.group(1)), note_type

    if text.startswith("#"):
        text = text[1:].strip()

    if text.isdigit():
        return int(text), None

    raise ToolError(
        f"Could not read {ref!r} as a note. Use a note URL or a numeric note id — "
        f"and pass `note_type` ('personal', 'task' or 'chat'), since the three "
        f"note families number their notes independently. `search_workspace` "
        f"returns both for every note it finds."
    )
