"""The links of a dashboard page (MIP-2417): the ONE place their paths are made.

A page names things by a reference, `{"kind": "task"|"milestone"|"project"|"note", "id": int}`
(a note also by `"note_type"`), never by an address. `paths_for` turns references into app paths
and gives one ONLY when every reader of the page may open the thing now; a reference that fails is
left out, so its words stay on the page as plain text. Nobody's text, a model's included, is ever
part of a path: every segment comes from the database row the reference found.

`is_app_path` is the second check the renderer makes before it writes an `href`
(`services/dashboard_page`), the same shapes the viewer accepts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, NamedTuple

from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.search_engine.agent.acl import may_read_note
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools.base import ToolContext
from origin.views.utils.note_role import NOTE_TYPE_CHAT, NOTE_TYPE_PERSONAL, NOTE_TYPE_TASK

KIND_TASK = "task"
KIND_MILESTONE = "milestone"
KIND_PROJECT = "project"
KIND_NOTE = "note"
KINDS = (KIND_TASK, KIND_MILESTONE, KIND_PROJECT, KIND_NOTE)

# A note's family as a reference names it. "team" and "shared" are the same table as "personal".
NOTE_FAMILIES = {
    "personal": "personal",
    "team": "personal",
    "shared": "personal",
    "task": "task",
    "chat": "chat",
    # the numbers a search hit carries
    "1": "personal",
    "2": "task",
    "3": "chat",
}
_MAX_ID = 2**62
# One page names a bounded number of things; past this the rest keep their words only.
MAX_REFS = 200
_CHAT_SLUGS = {1: "dm", 2: "gm", 3: "pm", 4: "mdm"}

_APP_PATH = re.compile(
    r"/workspace/(?:"
    r"tasks/project/[0-9]+(?:/(?:task|milestone)/[0-9]+)?"
    r"|notes/(?:my|team|shared)/[0-9]+"
    r"|notes/task/project/[0-9]+/task/[0-9]+/note/[0-9]+"
    r"|notes/chat/(?:dm|gm|pm|mdm)/[0-9a-f-]{36}/thread/(?:0|[0-9a-f-]{36})/note/[0-9]+"
    r")"
)


class Ref(NamedTuple):
    kind: str
    id: int
    # "personal", "task" or "chat" for a note; "" for everything else.
    family: str = ""


def is_app_path(value: Any) -> bool:
    """True only for one of the app's own routes this module writes, in full."""
    return isinstance(value, str) and _APP_PATH.fullmatch(value) is not None


def clean_ref(value: Any) -> Ref | None:
    """The reference `value` describes (a dict from a job or a model), or None. Nothing but the
    kind, a positive whole number and a note's family is read."""
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    raw = value.get("id")
    if isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 18:
        raw = int(raw)
    if kind not in KINDS or isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if not 0 < raw < _MAX_ID:
        return None
    if kind != KIND_NOTE:
        return Ref(kind, raw)
    family = NOTE_FAMILIES.get(str(value.get("note_type") or "").lower())
    return Ref(kind, raw, family) if family else None


def as_dict(ref: Ref) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": ref.kind, "id": ref.id}
    if ref.family:
        out["note_type"] = ref.family
    return out


def _task_paths(ctx: ToolContext, ids: set[int], readers: set[str]) -> dict[Ref, str]:
    from origin.services.schedule_jobs.ai_dashboard import visible_tasks  # noqa: PLC0415 (cycle)

    return {
        Ref(KIND_TASK, task_id): f"/workspace/tasks/project/{task.project_id}/task/{task_id}"
        for task_id, task in visible_tasks(ctx, ids, readers).items()
        if task.project_id
    }


def _everyone(ctx: ToolContext, readers: set[str]) -> list[LabelAccess]:
    return [LabelAccess(ToolContext(team_id=ctx.team_id, user_id=r)) for r in sorted(readers)]


def _milestone_paths(ctx: ToolContext, ids: set[int], readers: set[str]) -> dict[Ref, str]:
    access = _everyone(ctx, readers)
    out: dict[Ref, str] = {}
    for milestone_id in ids:
        found = [a.milestone(milestone_id) for a in access]
        if found and all(m is not None and m.project_id for m in found):
            out[Ref(KIND_MILESTONE, milestone_id)] = (
                f"/workspace/tasks/project/{found[0].project_id}/milestone/{milestone_id}"
            )
    return out


def _project_paths(ctx: ToolContext, ids: set[int], readers: set[str]) -> dict[Ref, str]:
    access = _everyone(ctx, readers)
    projects = ProjectMaster.objects.filter(
        project_id__in=ids, team_id=ctx.team_id, is_deleted=False
    )
    return {
        Ref(KIND_PROJECT, p.project_id): f"/workspace/tasks/project/{p.project_id}"
        for p in projects
        if access and all(a.project_readable(p) for a in access)
    }


def _in_team_space(note: PersonalNoteMaster) -> bool:
    """Filed in a team folder, itself or through the note at the top of its parent chain."""
    seen: set[int] = set()
    while note.parent_note_id and note.note_id not in seen:
        seen.add(note.note_id)
        parent = PersonalNoteMaster.objects.filter(note_id=note.parent_note_id).first()
        if parent is None:
            break
        note = parent
    return (
        note.folder_id is not None
        and PersonalNoteFolder.objects.filter(
            folder_id=note.folder_id, scope=PersonalNoteFolder.SCOPE_TEAM
        ).exists()
    )


def _note_path(ctx: ToolContext, ref: Ref, readers: set[str]) -> str | None:
    model, code = {
        "personal": (PersonalNoteMaster, NOTE_TYPE_PERSONAL),
        "task": (TaskNoteMaster, NOTE_TYPE_TASK),
        "chat": (ChatNoteMaster, NOTE_TYPE_CHAT),
    }[ref.family]
    note = model.objects.filter(note_id=ref.id, team_id=ctx.team_id).first()
    if note is None or not readers:
        return None
    if ref.family == "personal" and PersonalNoteMaster.skill_body_ids([ref.id]):
        return None  # a skill's text opens only in the Skills editor
    for reader in readers:
        if not may_read_note(
            user_id=reader, note_type_code=code, note_id=ref.id, owner_id=note.owner_id
        ):
            return None
    if ref.family == "task":
        if not (note.project_id and note.task_id):
            return None
        return f"/workspace/notes/task/project/{note.project_id}/task/{note.task_id}/note/{ref.id}"
    if ref.family == "chat":
        slug = _CHAT_SLUGS.get(note.chat_type)
        if slug is None or not note.channel_id:
            return None
        thread = note.thread_root_id or 0
        return f"/workspace/notes/chat/{slug}/{note.channel_id}/thread/{thread}/note/{ref.id}"
    if _in_team_space(note):
        return f"/workspace/notes/team/{ref.id}"
    # A note outside a team folder opens under a different route for its owner than for a person
    # it is shared with, and one page has one address.
    if len(readers) != 1:
        return None
    space = "my" if str(note.owner_id or "") in readers else "shared"
    return f"/workspace/notes/{space}/{ref.id}"


def paths_for(ctx: ToolContext, refs: Iterable[Ref], readers: set[str]) -> dict[Ref, str]:
    """`{ref: app path}` for the references EVERY one of `readers` may open now, as the member
    `ctx` runs for. A reference that is not in the answer gets no link. No reader, no link."""
    wanted = list(dict.fromkeys(refs))[:MAX_REFS]
    if not wanted or not readers:
        return {}
    ids: dict[str, set[int]] = {}
    for ref in wanted:
        ids.setdefault(ref.kind, set()).add(ref.id)
    out: dict[Ref, str] = {}
    if task_ids := ids.get(KIND_TASK):
        out.update(_task_paths(ctx, task_ids, readers))
    if milestone_ids := ids.get(KIND_MILESTONE):
        out.update(_milestone_paths(ctx, milestone_ids, readers))
    if project_ids := ids.get(KIND_PROJECT):
        out.update(_project_paths(ctx, project_ids, readers))
    for ref in wanted:
        if ref.kind == KIND_NOTE and (path := _note_path(ctx, ref, readers)) is not None:
            out[ref] = path
    return {ref: path for ref, path in out.items() if ref in wanted and is_app_path(path)}


def _holders(value: Any) -> Iterable[dict[str, Any]]:
    """Every dict of a page's data that may name a thing (it has a `ref` or an `href`). The rows
    the page holds back (`more_rows`, MIP-2452) come last, so on a page with more things than it
    links (`MAX_REFS`) it is never a row in sight that goes without."""
    held: list[Any] = []
    stack = [value]
    while stack or held:
        if not stack:
            stack, held = held[::-1], []
        item = stack.pop()
        if isinstance(item, dict):
            if "ref" in item or "href" in item:
                yield item
            children = []
            for key, child in item.items():
                (held if key == "more_rows" else children).append(child)
            stack.extend(children[::-1])
        elif isinstance(item, list):
            stack.extend(item[::-1])


def link_page(page: Any, ctx: ToolContext, readers: set[str]) -> None:
    """Give every referenced thing of `page` its `href`, in place, for the ones every reader may
    open. An `href` the data already had is removed first: this is the only thing that writes one."""
    holders = list(_holders(page))
    found: list[tuple[dict[str, Any], Ref]] = []
    for holder in holders:
        holder.pop("href", None)
        if (ref := clean_ref(holder.get("ref"))) is not None:
            found.append((holder, ref))
    paths = paths_for(ctx, [ref for _, ref in found], readers)
    for holder, ref in found:
        if ref in paths:
            holder["href"] = paths[ref]


def keep_seen(page: Any, seen: set[Ref]) -> None:
    """Drop every `ref` (and `href`) of a model's page that its run did not read, in place."""
    for holder in list(_holders(page)):
        holder.pop("href", None)
        if clean_ref(holder.get("ref")) not in seen:
            holder.pop("ref", None)


# Keys of a tool result that hold what a PERSON typed (their own field names), not Genos's ids.
_NOT_ROWS = ("custom_fields",)
MAX_SEEN = 5000


def _kind_of_key(key: str) -> str:
    if key == "task_id" or key.endswith("_task_id"):
        return KIND_TASK
    if key in ("milestone_id", "backs_milestone_id"):
        return KIND_MILESTONE
    return KIND_PROJECT if key == "project_id" else ""


def refs_in_result(tool_name: str, result: Any, seen: set[Ref]) -> None:
    """Add to `seen` the things the RESULT of one Genos tool names by id. Only results count (a
    model chooses its arguments), and only Genos's own tools: a number from GitHub or a calendar
    is not the id of anything here."""
    from origin.search_engine.agent.tool_tiers import INTEGRATION_TOOLS  # noqa: PLC0415 (heavy)

    if any(tool_name in tools for tools in INTEGRATION_TOOLS.values()):
        return
    stack = [result]
    while stack and len(seen) < MAX_SEEN:
        value = stack.pop()
        if isinstance(value, list):
            stack.extend(value)
        if not isinstance(value, dict):
            continue
        for key, child in value.items():
            if key in _NOT_ROWS or not isinstance(key, str):
                continue
            if isinstance(child, (dict, list)):
                stack.append(child)
                continue
            if key == "note_id":
                ref = clean_ref(
                    {"kind": KIND_NOTE, "id": child, "note_type": value.get("note_type")}
                )
            else:
                ref = clean_ref({"kind": _kind_of_key(key), "id": child})
            if ref is not None:
                seen.add(ref)
