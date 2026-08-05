"""`team_id` in a request names the caller's CURRENT team, not the owner.

Every v2 endpoint takes a `team_id` and most of them use it twice: once as
authorization ("is the caller in this team") and once as a filter
(`.filter(team=team_id, project=..., ...)`). That worked while an object
could only ever live in the team the caller was looking at.

Cross-team sharing broke the second use. A guest of team A reads A's
project from inside team B's workspace, so the request says `team_id=B`
while the project, its tasks, its milestones and its notes all say A. The
authorization half still answers correctly — it is per-object
(`is_project_member`, `can_access_task`, `get_effective_role`) and asks
nothing about teams — but the filter half silently matches zero rows. The
symptom is never a 403: it is an empty task list, a 404 on a note that
plainly exists, and a 400 from whatever the client tried next with the
half-loaded state. Guests could open a shared project and find it empty.

Fixing that filter at each of the ~110 call sites would be a hundred
chances to miss one, and the misses are invisible in code review because
the wrong version looks exactly like the right one. So it is fixed once,
here, in `AuthenticatedAPIView.initial`: when a request names an object
that belongs to a different team than the one it claims to be in, and the
caller genuinely reaches that object, `team_id` is rewritten to the team
that owns it before the view sees it.

This is a substitution, never a grant:

* The rewrite happens only if the caller passes that object's OWN access
  check. A stranger who guesses an id changes nothing about their request
  and gets the same nothing they got before.
* No view's authorization is bypassed or relaxed. Views that ask "is the
  caller a member of `team_id`" are now asked about the owning team, so a
  guest is refused the host's administrative endpoints exactly as they
  are refused them when switched into the host team's shell. That shell
  is the path this collapses onto: a request from team B's workspace now
  behaves the same as the same request made from A's, which is the
  behaviour that was already shipped and tested.
* Only object-scoped parameters are recognized. `object_id` on the
  cross-team endpoints themselves is deliberately NOT one of them: there
  `team_id` means the OTHER team (who to offer a share to), and
  rewriting it would invert the meaning of the call.

Cost is one indexed lookup per request that carries both a `team_id` and
an object id, cached briefly. An object's team is *nearly* immutable — the
one thing that rewrites it is a move into a project another team owns, so
whatever performs such a move has to call `forget_object_team`. See its
docstring for what a stale entry does.
"""

from __future__ import annotations

from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError

# Note types, as `note_type` uses them on the wire. Duplicated from
# `views/utils/note_role` rather than imported: this module is loaded on
# every request through the base view, and the note ACL layer pulls in
# five model modules.
_NOTE_PERSONAL = 1
_NOTE_TASK = 2
_NOTE_CHAT = 3

# What the object is, and what the wire calls it. Most specific first: a
# request that names several (a task note carries both `note_id` and
# `task_id`) resolves to the same team either way, so the order only
# decides which query runs.
#
# The bare `team` / `project` / `task` spellings are the older payload
# style, still used by the task create and update paths — the ones a guest
# needs for a shared project to be writable and not merely readable.
_TEAM_KEYS = ("team_id", "team")
_OBJECT_KEYS = (
    ("note", "note_id"),
    ("folder", "folder_id"),
    ("task", "task_id"),
    ("task", "task"),
    ("project", "project_id"),
    ("project", "project"),
)

# Paths whose `team_id` is somebody else's team on purpose. Prefix match
# against `request.path`.
_EXEMPT_PREFIXES = ("/api/v2/team/share", "/api/v2/team/connection")

_CACHE_TTL = 300


def _note_type(request) -> int:
    """Which note table `note_id` refers to.

    Sent explicitly by the endpoints that serve all three kinds; implied
    by the URL on the per-kind ones (`/note/personal/`, `/note/task/`,
    `/note/chat/`), which is where the single-note reads live.
    """
    raw = request.GET.get("note_type") or _body_get(request, "note_type")
    try:
        if raw is not None:
            return int(raw)
    except (TypeError, ValueError):
        pass
    path = request.path
    if "/note/task/" in path:
        return _NOTE_TASK
    if "/note/chat/" in path:
        return _NOTE_CHAT
    return _NOTE_PERSONAL


def _cache_key(kind: str, object_id, note_type: int) -> str:
    return f"objteam:{kind}:{note_type if kind == 'note' else ''}:{object_id}"


def forget_object_team(kind: str, object_id, note_type: int = _NOTE_PERSONAL) -> None:
    """Drop the cached owning team for one object, after it changed teams.

    A stale entry here is worse than no cache at all: the rewrite is
    skipped precisely when it is needed. A task moved into an externally
    shared project now belongs to the HOST team, but for the rest of the
    TTL this still answers with the team it came from — which equals the
    `team_id` the guest's client keeps sending, so `align_team_to_object`
    reads the request as already consistent and leaves it alone. Every
    view then filters on the old team and matches nothing: the task the
    user just moved 400s on its own reload ("Failed to fetch expected
    task data") while plainly sitting in the destination project.
    """
    cache.delete(_cache_key(kind, object_id, note_type))


def _owning_team(kind: str, object_id, note_type: int) -> str | None:
    """The team a given object belongs to, or None if there is no such object."""
    cache_key = _cache_key(kind, object_id, note_type)
    cached = cache.get(cache_key)
    if cached is not None:
        # Absence is cached as "" so a probe for a nonexistent id does not
        # re-query on every retry.
        return cached or None

    team = _load_owning_team(kind, object_id, note_type)
    cache.set(cache_key, team or "", timeout=_CACHE_TTL)
    return team


def _load_owning_team(kind: str, object_id, note_type: int) -> str | None:
    try:
        if kind == "project":
            from origin.models.project.prj_models import ProjectMaster

            row = (
                ProjectMaster.objects.filter(project_id=object_id)
                .values_list("team_id", flat=True)
                .first()
            )
        elif kind == "task":
            from origin.models.task.task_models import TaskMaster

            row = (
                TaskMaster.objects.filter(task_id=object_id)
                .values_list("team_id", flat=True)
                .first()
            )
        elif kind == "folder":
            from origin.models.note.personal_note_models import PersonalNoteFolder

            row = (
                PersonalNoteFolder.objects.filter(folder_id=object_id)
                .values_list("team_id", flat=True)
                .first()
            )
        else:
            row = _note_team(object_id, note_type)
    except (DjangoValidationError, ValueError, TypeError):
        # A malformed id names nothing. Same answer as a missing row.
        return None
    return str(row) if row else None


def _note_team(note_id, note_type: int):
    if note_type == _NOTE_TASK:
        from origin.models.note.task_note_models import TaskNoteMaster

        return (
            TaskNoteMaster.objects.filter(note_id=note_id).values_list("team_id", flat=True).first()
        )
    if note_type == _NOTE_CHAT:
        from origin.models.note.chat_note_models import ChatNoteMaster

        return (
            ChatNoteMaster.objects.filter(note_id=note_id).values_list("team_id", flat=True).first()
        )
    from origin.models.note.personal_note_models import PersonalNoteMaster

    return (
        PersonalNoteMaster.objects.filter(note_id=note_id).values_list("team_id", flat=True).first()
    )


def _may_reach(kind: str, object_id, note_type: int, user_id) -> bool:
    """Does this person reach the object on its own terms?

    The object's own access rule, not a team rule — that is the whole
    point. Deliberately the same predicates the views themselves use, so
    the rewrite can never widen what a view would then allow.
    """
    if kind == "project":
        from origin.views.utils.scope_guards import is_project_member

        return is_project_member(object_id, user_id)
    if kind == "task":
        from origin.views.utils.scope_guards import can_access_task

        return can_access_task(object_id, user_id)
    if kind == "folder":
        from origin.views.utils.note_folder_role import get_folder_role

        return get_folder_role(user_id, object_id) is not None
    from origin.views.utils.note_role import get_effective_role

    return get_effective_role(user_id, note_type, object_id) is not None


def _body_get(request, key):
    """`request.data[key]`, or None when there is no readable body.

    Multipart is skipped: forcing DRF to parse an upload here, before the
    view has decided anything, is a real cost on a path that runs for
    every request. The attachment endpoints do not scope by team anyway.
    """
    if request.method in ("GET", "HEAD", "OPTIONS", "DELETE"):
        return None
    content_type = getattr(request, "content_type", "") or ""
    if content_type.startswith("multipart/"):
        return None
    try:
        data = request.data
    except Exception:  # noqa: BLE001 - malformed body is the view's problem, not ours
        return None
    if not hasattr(data, "get"):
        return None
    return data.get(key)


def _param(request, key):
    """One request value, wherever this endpoint happens to take it."""
    return request.GET.get(key) or _body_get(request, key)


def _rewrite(request, team_id: str) -> None:
    """Put the owning team wherever the view is going to read it from.

    Only over keys that were already present: adding a `team_id` to a
    request that carried none would hand a value to a view that decided
    not to ask for one.
    """
    query = request._request.GET
    for key in _TEAM_KEYS:
        if key in query:
            was_mutable = query._mutable
            query._mutable = True
            query[key] = team_id
            query._mutable = was_mutable
    if _body_get(request, "team_id") is None and _body_get(request, "team") is None:
        return
    data = request.data
    for key in _TEAM_KEYS:
        if key not in data:
            continue
        if hasattr(data, "_mutable"):
            was_mutable = data._mutable
            data._mutable = True
            data[key] = team_id
            data._mutable = was_mutable
        else:
            try:
                data[key] = team_id
            except TypeError:
                return


def align_team_to_object(request) -> None:
    """Point `team_id` at the team that owns the object being named.

    A no-op for the overwhelmingly common request, where the caller is
    working inside their own team and the two already agree.
    """
    if any(request.path.startswith(prefix) for prefix in _EXEMPT_PREFIXES):
        return
    user_id = getattr(getattr(request, "user", None), "id", None)
    if user_id is None:
        return

    claimed = None
    for key in _TEAM_KEYS:
        claimed = _param(request, key)
        if claimed:
            break
    if not claimed:
        return

    note_type = None
    for kind, key in _OBJECT_KEYS:
        object_id = _param(request, key)
        if not object_id:
            continue
        if note_type is None:
            note_type = _note_type(request)
        owner = _owning_team(kind, object_id, note_type)
        if owner is None or owner == str(claimed):
            return
        if _may_reach(kind, object_id, note_type, user_id):
            _rewrite(request, owner)
        return
