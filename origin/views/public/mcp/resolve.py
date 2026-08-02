"""Turn however a human named a task into a task id.

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
