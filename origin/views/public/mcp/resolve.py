"""Turn however a human named a task or a note into the ids tools take.

Genos's own tools take `task_id` as an integer, which is right for code
that just read the id out of a result. It is wrong wherever the reference
comes from a person: they say "work on PRJ-123", or paste the URL out of
their browser. Numeric ids are the one form nobody has to hand. That goes
for the in-app agent as much as for MCP (`agent/references.py`).

So the task tools take a string reference and it is resolved here, before
the underlying tool ever sees it. Four forms, all of which a person
plausibly produces:

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
from urllib.parse import parse_qs

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.project_gone import live_tasks_q

# `/workspace/tasks/project/5/task/42`, with or without a trailing
# `/comment/3`. Anchored on `/task/` so the project id can't be mistaken
# for the task id — they are adjacent and both bare integers.
_URL_TASK = re.compile(r"/task/(\d+)")
# `PRJ-123`: a project code, then the per-project number. Split on the
# LAST hyphen, since codes may contain one.
_DISPLAY_ID = re.compile(r"^(?P<code>[A-Za-z0-9_\-]+)-(?P<number>\d+)$")


# A reference is a display id, a number, or a URL; the longest real one (an app
# URL with a thread and a comment) is a few hundred characters. Anything past
# this is not one, and is refused before any pattern runs over it, so a
# crafted argument cannot make a worker spend seconds in a regex.
MAX_REF_CHARS = 2048


def _checked_text(ref) -> str:
    text = str(ref).strip()
    if len(text) > MAX_REF_CHARS:
        raise ToolError(
            f"That reference is too long ({len(text)} characters; the limit is {MAX_REF_CHARS}). "
            "Pass a task or note URL, a display id like PRJ-123, or a numeric id."
        )
    return text


def resolve_task_ref(ref, team_id: str, user_id: str | None = None) -> int:
    """`ref` -> a task id, or `ToolError` naming what we tried.

    The error text is written for a model to act on, not a human to
    debug: it says which forms are accepted, so a wrong guess can be
    corrected on the next call instead of ending the turn.

    With `user_id`, a display id whose task that user cannot open answers exactly like a code
    nobody has (MIP-1957): the tool it is passed to would otherwise say "Not authorized ... task
    12345", which confirms a hidden project's code and hands over a task id.
    """
    if isinstance(ref, int):
        return ref
    if ref is None:
        raise ToolError("A task reference is required (e.g. PRJ-123, a task URL, or an id).")

    text = _checked_text(ref)
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
        row = (
            TaskMaster.objects.filter(
                # A task of a soft-deleted project has no display id to find, as a missing one has none
                # (MIP-1621, flag `AGENT_PARITY_FOLLOWUPS`): the tool it is passed to would otherwise answer
                # "not found" where this answers with a task, and the difference would say it exists.
                live_tasks_q(),
                team_id=team_id,
                project__code__iexact=code,
                project_task_number=number,
                is_deleted=False,
            )
            .values_list("task_id", "project_id", "assignee_id", "reporter_id")
            .first()
        )
        task_id = None
        if row is not None:
            task_id = row[0]
            if user_id is not None and str(user_id) not in task_acl_user_ids(*row[1:]):
                task_id = None
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


# A milestone id no row has. What a QUIET resolution (search) returns for a
# well-formed reference that names no milestone the caller could be told about,
# so the scope matches nothing instead of producing an error that would confirm
# a task or milestone exists (search_kb.py, MIP-797).
NO_SUCH_MILESTONE_ID = 2**31 - 1


def resolve_milestone_ref(ref, team_id: str, *, quiet: bool = False):
    """`ref` -> a milestone id (int), or `''` when `ref` is `''`.

    A milestone is also a task, and people name it by that task's display id
    (`MIP-617`) or URL, neither of which is the milestone id the tools take.
    Those two resolve to the backing task, then to its milestone, team-scoped.

    Rules that keep existing callers unchanged:
    * a bare number (`98`, `#98`) is a MILESTONE id, never a backing task id;
    * `''` passes through, because `update_task` / `update_tasks_bulk` use it
      for "take the task out of its milestone" (`resolve_task_ref` raises on
      it);
    * anything unreadable is left for the tool to refuse in its own words.

    `quiet` is for search: a well-formed reference that does not lead to a
    milestone yields `NO_SUCH_MILESTONE_ID` rather than an error, so the call
    cannot be used to probe which tasks exist.
    """
    if isinstance(ref, int):
        return ref
    if ref is None:
        raise ToolError("A milestone reference is required (e.g. PRJ-123 or a milestone id).")

    text = _checked_text(ref)
    if text == "":
        return ""
    bare = text[1:].strip() if text.startswith("#") else text
    if bare.isdigit():
        return int(bare)
    if not (_URL_TASK.search(text) or _DISPLAY_ID.match(text)):
        # Not something we read: the tool's own "must be an integer" error is
        # the feedback, and it says nothing about what exists.
        return text

    def _fail(message: str):
        if quiet:
            return NO_SUCH_MILESTONE_ID
        raise ToolError(message)

    try:
        task_id = resolve_task_ref(text, team_id)
    except ToolError as exc:
        return _fail(str(exc))
    milestone_id = (
        MilestoneMaster.objects.filter(task_id=task_id, team_id=team_id, is_deleted=False)
        .values_list("milestone_id", flat=True)
        .first()
    )
    if milestone_id is None:
        return _fail(
            f"{text} is not a milestone (or is not one in this team), so it has no "
            "milestone id. Use `list_milestones` to find the milestone, or pass its "
            "numeric `milestone_id`."
        )
    return milestone_id


# `/workspace/notes/<bucket>/…/note/12`, or `/workspace/notes/my/12` for a
# personal note, which has no trailing `/note/` segment. Anchored on the
# LAST id in the path so the project / task / thread ids a task- or
# chat-note URL carries can't be mistaken for the note id.
#
# The `.*` between the two literals is bounded: unbounded, a string of `/note/`
# segments that never ends in digits made the engine retry the whole tail from
# every earlier `/notes/`, quadratic in the input (CodeQL py/polynomial-redos,
# MIP-819). A real URL's path between the bucket and the note id is a few
# segments; 1000 characters is far above that. `_resolve_text` also caps the
# whole reference, which is what protects the next pattern added here.
_URL_NOTE_TRAILING = re.compile(r"/notes?/.{0,1000}/note/(\d+)")
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


# A BlockNote block id as the editor keeps it, and the only shape `?block=` may carry. The
# frontend checks the same pattern before it reads the parameter (`noteTarget.ts`), because the id
# ends up in a selector and a document lookup; here it ends up in an edit.
BLOCK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _one_link_param(ref, name: str, malformed: str) -> str | None:
    """The single value of query parameter `name` in a note link, or None when it has none.

    Refused (`malformed`) rather than ignored when the value is not an id or the parameter appears
    twice with different values: the caller meant something, and falling back to "nothing" could let
    a stray argument act on another block or thread.
    """
    if not isinstance(ref, str):
        return None
    text = _checked_text(ref)
    query = text.partition("#")[0].partition("?")[2]
    if not query:
        return None
    values = parse_qs(query, keep_blank_values=True).get(name)
    if not values:
        return None
    if len(set(values)) > 1 or not BLOCK_ID.match(values[0]):
        raise ToolError(malformed)
    return values[0]


def comment_in_note_link(ref) -> str | None:
    """The comment thread a note link points at, or None when it points at none (MIP-1662).

    "Copy link to comment" makes the note's own URL plus ONE query parameter,
    `/workspace/notes/my/12?comment=<threadId>` (MIP-146); a thread id has the same shape as a block
    id (the frontend checks both with one pattern). A `?block=` link names a block, never a thread.
    """
    return _one_link_param(
        ref,
        "comment",
        "The `comment` in that link is not one thread id. Copy the link again with 'Copy link to "
        "comment', or pass `thread_id` from `comment_threads` in `get_note`.",
    )


def block_in_note_link(ref) -> str | None:
    """The block a note link points at, or None when it points at none (MIP-1655).

    "Copy link to block" makes the note's own URL plus ONE query parameter,
    `/workspace/notes/my/12?block=<blockId>` (MIP-146). `resolve_note_ref` reads
    the note from the path and ignores the query, so a caller who wants the block
    too asks here. A bare id, or a link with no `block` (a `?comment=` link names
    a thread, not a block), names none.

    A `block` that is present but not a block id, or that appears twice with
    different values, is refused rather than ignored: the caller meant a block, and
    falling back to "no block" could let a stray `block_id` argument edit another.
    """
    return _one_link_param(
        ref,
        "block",
        "The `block` in that link is not one block id. Copy the link again with "
        "'Copy link to block', or pass `block_id` from `get_note` (with "
        "`include_blocks: true`).",
    )


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

    text = _checked_text(ref)
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
