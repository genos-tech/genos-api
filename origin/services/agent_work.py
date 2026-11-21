"""What the AI apps did in a team, as the viewer may read it (MIP-1613, `GET /api/v2/agent/work/`).

Four sources, each with its own access rule, merged into one answer:

* **task activity** (`TaskActivity`): every change to a task, with `metadata.via` (how it came:
  an API key, a connected app, the in-app agent, the web app). Only the tasks the viewer may open
  (`TaskMaster` rows that `scope_guards.can_access_task` would admit, here as one query) and only
  the team's rows. Comments are the `comment_added` rows; their text is read from `TaskComments`.
* **note versions** (`NoteVersionMaster`, `via` on each): a note the viewer may open, by
  `note_scope.readable_note_rows`, the one batch rule the timeline uses.
* **decisions**: notes titled "Decision: ..." (personal and task notes) the viewer may open.
* **connections**: the viewer's own live connected apps (family and last use).

**Who an agent is.** A row's agent is read through `via_for_viewer`: an API key is `api_key` for
everyone; a connected Claude or ChatGPT is its family for everyone; any other connected app is
`other_app` and its name never leaves its owner. A row with no `via`, or one from the web app or the
in-app Genos AI, is `person` and carries no agent. A system write with no person and no agent (a
GitHub webhook, a timer) is left out. The SQL label (`_label_sql`) and the Python one
(`_label_of`) are the same rule; a test pins both on the same rows.

**Not included, on purpose.** AI-app notices (`ActivityType.AI_APP`): every MCP task write already
leaves a `via`-stamped `TaskActivity` row, and a notice counts the same call again, so listing both
would count each change twice. Note comments (their source is not readable over REST yet, MIP-1739),
projects, sprints, members, calendar.

**Bounded.** Every query is limited by the window and the team; the page and the scans are capped
(`MAX_PAGE_ITEMS`, `SCAN_LIMIT`); the number of queries does not grow with the team's size.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Case, CharField, Count, F, Q, Value, When
from django.db.models.fields.json import KeyTextTransform, KeyTransform
from django.utils import timezone

from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.services.mcp_client_family import APP_FAMILIES, app_family
from origin.services.note_scope import readable_note_rows
from origin.services.plain_text import body_text
from origin.views.public.mcp.via_privacy import via_for_viewer
from origin.views.utils.scope_guards import reachable_project_ids

A = TaskActivityActionType

AGENTS = ("claude", "chatgpt", "api_key", "other_app", "person")
KINDS = ("task_created", "task_status", "task_updated", "task_comment", "note", "decision")
DEFAULT_DAYS = 7
MAX_DAYS = 31
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_OFFSET = 2000
# The newest rows read per source before the Python-side read check. A note the viewer cannot open
# is dropped after the read, so the scan is what bounds the work, not the page.
SCAN_LIMIT = 1000
DECISION_LIMIT = 50
BY_USER_LIMIT = 50
TEXT_CHARS = 280
SCALAR_CHARS = 80

# The task-activity types each kind of work is made of. A type not listed here (comment edits and
# deletes, the GitHub links, a close that nothing writes) is not shown: the list fails closed.
ACTIVITY_KINDS: dict[str, tuple[str, ...]] = {
    "task_created": (A.CREATED,),
    "task_status": (A.STATUS, A.REOPENED, A.DELETED),
    "task_updated": (
        A.TITLE,
        A.PRIORITY,
        A.EFFORT,
        A.ASSIGNEE,
        A.REPORTER,
        A.DUE_DATE,
        A.START_DATE,
        A.DESCRIPTION,
        A.TAGS,
        A.PARENT,
        A.MILESTONE_LINK,
        A.SPRINT_LINK,
        A.PROJECT,
        A.CUSTOM_FIELD,
        A.ATTACHMENT_ADDED,
        A.ATTACHMENT_REMOVED,
        A.MILESTONE_ASSIGNEE_ADDED,
        A.MILESTONE_ASSIGNEE_REMOVED,
    ),
    "task_comment": (A.COMMENT_ADDED,),
}
NOTE_TYPES = (1, 2, 3)  # personal, task, chat: `note_scope.NOTE_MODELS`
DECISION_NOTE_TYPES = (1, 2)
DECISION_PREFIX = "Decision:"


class BadFilter(ValueError):
    """A query value the endpoint refuses (answered 400 by the view)."""


@dataclass(frozen=True)
class Filters:
    days: int
    project_id: int | None
    agent: str | None
    user_id: str | None
    kind: str | None

    @property
    def activity_types(self) -> tuple[str, ...]:
        if self.kind is None:
            return tuple(t for group in ACTIVITY_KINDS.values() for t in group)
        return ACTIVITY_KINDS.get(self.kind, ())

    @property
    def wants_notes(self) -> bool:
        return self.kind in (None, "note")

    @property
    def wants_decisions(self) -> bool:
        return self.kind in (None, "decision")


# ── the agent label ──────────────────────────────────────────────────────────


def _label_of(via: Any, actor_id: Any) -> str | None:
    """The agent of a row, from its `via` as the viewer may read it (`via_for_viewer`)."""
    kind = via.get("kind") if isinstance(via, dict) else None
    if not isinstance(kind, str) or not kind:
        return "person" if actor_id is not None else None
    if kind == "api_key":
        return "api_key"
    if kind == "oauth":
        app = via.get("app")
        return app if app in APP_FAMILIES else "other_app"
    if kind in ("agent", "app"):
        return "person" if actor_id is not None else None
    return None


def _via_annotations(via_expr: Any) -> dict[str, Any]:
    """The `via` kind and family as annotations (`k_kind`, `k_app`), for `_label_case`."""
    return {
        "k_kind": KeyTextTransform("kind", via_expr),
        "k_app": KeyTextTransform("app", via_expr),
    }


def _label_case(actor_field: str) -> Case:
    """`_label_of` as SQL, over the `k_kind` / `k_app` annotations and `actor_id`. NULL when the
    row is left out. Keep in step with `_label_of`: `test_agent_work` checks both on one matrix."""
    return Case(
        When(k_kind="api_key", then=Value("api_key")),
        When(Q(k_kind="oauth") & Q(k_app="claude"), then=Value("claude")),
        When(Q(k_kind="oauth") & Q(k_app="chatgpt"), then=Value("chatgpt")),
        When(k_kind="oauth", then=Value("other_app")),
        When(
            Q(**{f"{actor_field}__isnull": False})
            & (Q(k_kind__in=["agent", "app"]) | Q(k_kind__isnull=True) | Q(k_kind="")),
            then=Value("person"),
        ),
        default=Value(None),
        output_field=CharField(),
    )


def _agent_obj(label: str | None) -> dict | None:
    if label is None or label == "person":
        return None
    if label in APP_FAMILIES:
        return {"kind": label, "app": label}
    return {"kind": label}


def _user_obj(user_id: Any, names: dict[str, str]) -> dict | None:
    if user_id is None:
        return None
    key = str(user_id)
    return {"id": key, "name": names.get(key, "")}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ── access ───────────────────────────────────────────────────────────────────


def _visible_tasks(viewer_id: Any, team_id: str):
    """The tasks `viewer_id` may open in `team_id`, as one query. The rule of
    `scope_guards.can_access_task`: a live task, on a live project, that the viewer reaches through a
    project (member, owner, a shared grant) or as its assignee or reporter on this team."""
    projects = reachable_project_ids(viewer_id, team_id)
    holder = Q(assignee_id=viewer_id) | Q(reporter_id=viewer_id)
    on_team = Q(project__team_id=team_id) | Q(project__isnull=True, team_id=team_id)
    return TaskMaster.objects.filter(
        Q(is_deleted=False) & ~Q(project__is_deleted=True),
    ).filter(Q(project_id__in=projects) | (holder & on_team))


def _activity_qs(viewer_id: Any, team_id: str, since: datetime, f: Filters, *, agent=None):
    qs = TaskActivity.objects.filter(
        Q(team_id=team_id) | Q(team__isnull=True),
        ts_created_at__gte=since,
        action_type__in=f.activity_types,
        task_id__in=_visible_tasks(viewer_id, team_id).values("task_id"),
    )
    if f.project_id is not None:
        qs = qs.filter(task__project_id=f.project_id)
    if f.user_id is not None:
        qs = qs.filter(actor_id=f.user_id)
    via = KeyTransform("via", "metadata")
    qs = (
        qs.annotate(via_json=via, comment_id=KeyTransform("commentId", "metadata"))
        .annotate(**_via_annotations(via))
        .annotate(agent=_label_case("actor_id"))
        .filter(agent__isnull=False)
    )
    if agent is not None:
        qs = qs.filter(agent=agent)
    return qs


def _note_versions_qs(team_id: str, since: datetime, f: Filters):
    qs = NoteVersionMaster.objects.filter(
        team_id=team_id, note_type__in=NOTE_TYPES, ts_created_at__gte=since
    )
    if f.user_id is not None:
        qs = qs.filter(editor_id=f.user_id)
    return (
        qs.annotate(**_via_annotations(F("via")))
        .annotate(agent=_label_case("editor_id"))
        .filter(agent__isnull=False)
    )


def _readable(viewer_id: Any, team_id: str, keys: set[tuple[int, int]]) -> dict:
    if not keys:
        return {}
    return readable_note_rows(ToolContext(team_id=str(team_id), user_id=str(viewer_id)), keys)


def _note_projects(viewer_id, team_id: str, keys: set[tuple[int, int]]) -> dict:
    """A task note's project, when the viewer may see that project; else None. A note the viewer
    can read may sit in a project they are not in, and that project's name must not show here.
    Personal and chat notes have no project."""
    task_keys = {nid for nt, nid in keys if nt == 2}
    if not task_keys:
        return {}
    allowed = set(reachable_project_ids(viewer_id, team_id))
    rows = TaskNoteMaster.objects.filter(team_id=team_id, note_id__in=task_keys).values_list(
        "note_id", "project_id"
    )
    return {(2, nid): (pid if pid in allowed else None) for nid, pid in rows}


# ── text ─────────────────────────────────────────────────────────────────────


def _one_line(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= TEXT_CHARS else flat[: TEXT_CHARS - 1] + "…"


def _scalar(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    return text if len(text) <= SCALAR_CHARS else None


def _activity_text(row: dict, comments: dict[tuple[int, int], Any]) -> str | None:
    if row["action_type"] == A.COMMENT_ADDED:
        body = comments.get((row["task_id"], row["comment_id"]))
        return _one_line(body_text(body)) if body else None
    old, new = _scalar(row["old_value"]), _scalar(row["new_value"])
    if old is not None and new is not None:
        return _one_line(f"{old} → {new}")
    return None


# ── the report ───────────────────────────────────────────────────────────────


def build(*, viewer_id: Any, team_id: str, f: Filters, offset: int, limit: int, now=None) -> dict:
    now = now or timezone.now()
    since = now - timedelta(days=f.days)
    activity_all = _activity_qs(viewer_id, team_id, since, f)

    # Totals: every filter but the agent (the summary row shows every agent).
    counts_by_agent = {a: 0 for a in AGENTS}
    by_project: dict[int | None, Counter] = defaultdict(Counter)
    by_user: dict[tuple[str, str], dict] = {}
    for row in activity_all.values("agent", "actor_id", "task__project_id").annotate(n=Count("pk")):
        _add_count(counts_by_agent, by_project, by_user, row, row["task__project_id"])

    note_keys: set[tuple[int, int]] = set()
    note_rows: list[dict] = []
    if f.wants_notes:
        versions = (
            _note_versions_qs(team_id, since, f)
            .values("note_type", "note_id", "editor_id", "agent")
            .annotate(n=Count("pk"))
        )
        note_rows = list(versions)
        note_keys = {(r["note_type"], r["note_id"]) for r in note_rows}
    readable = _readable(viewer_id, team_id, note_keys)
    note_project = _note_projects(viewer_id, team_id, note_keys)
    for row in note_rows:
        key = (row["note_type"], row["note_id"])
        if key not in readable:
            continue
        _add_count(counts_by_agent, by_project, by_user, row, note_project.get(key))

    total_changes = sum(counts_by_agent.values())

    # Decisions (not agent-filtered in the totals; filtered in the list).
    decisions, decision_total = (
        _decisions(viewer_id, team_id, since, f) if f.wants_decisions else ([], 0)
    )

    # Items: the newest rows of both sources, merged.
    want = offset + limit + 1
    items = _items(viewer_id, team_id, since, f, want)
    merged = items
    page = merged[offset : offset + limit]
    has_more = len(merged) > offset + limit

    names = _names(
        {str(uid) for uid, *_ in by_user if uid}
        | {i["user"]["id"] for i in page if i["user"]}
        | {d["user"]["id"] for d in decisions if d["user"]}
    )
    for item in page:
        if item["user"]:
            item["user"]["name"] = names.get(item["user"]["id"], "")
    for d in decisions:
        if d["user"]:
            d["user"]["name"] = names.get(d["user"]["id"], "")

    project_ids = {pid for pid in by_project if pid is not None}
    projects = {
        p.project_id: p
        for p in ProjectMaster.objects.filter(project_id__in=project_ids).only(
            "project_id", "project_name", "code"
        )
    }
    groups = _groups(by_project, projects, page, f)

    return {
        "window": {
            "days": f.days,
            "since": since.isoformat(),
            "until": now.isoformat(),
        },
        "filters": {
            "project_id": f.project_id,
            "agent": f.agent,
            "user_id": f.user_id,
            "kind": f.kind,
        },
        "totals": {
            "by_agent": counts_by_agent,
            "decisions": decision_total,
            "changes": total_changes,
        },
        "by_user_agent": _by_user_rows(by_user, names, projects, f),
        "groups": groups,
        "decisions": decisions,
        "connections": _connections(viewer_id, team_id),
        "next_cursor": str(offset + limit) if has_more else None,
    }


def _add_count(counts_by_agent, by_project, by_user, row, project_id) -> None:
    label, n = row["agent"], row["n"]
    counts_by_agent[label] += n
    by_project[project_id][label] += n
    user = row.get("actor_id") if "actor_id" in row else row.get("editor_id")
    key = (str(user) if user is not None else "", label)
    slot = by_user.setdefault(key, {"count": 0, "projects": Counter()})
    slot["count"] += n
    if project_id is not None:
        slot["projects"][project_id] += n


def _names(user_ids: set[str]) -> dict[str, str]:
    if not user_ids:
        return {}
    return {
        str(u.id): u.username
        for u in CustomUser.objects.filter(id__in=user_ids).only("id", "username")
    }


def _by_user_rows(by_user, names, projects, f: Filters) -> list[dict]:
    rows = []
    for (uid, label), slot in by_user.items():
        if f.agent is not None and label != f.agent:
            continue
        rows.append(
            {
                "user": _user_obj(uid, names) if uid else None,
                "agent": label,
                "count": slot["count"],
                "projects": [
                    {"id": pid, "name": projects[pid].project_name, "code": projects[pid].code}
                    for pid, _ in slot["projects"].most_common(3)
                    if pid in projects
                ],
            }
        )
    rows.sort(key=lambda r: (-r["count"], r["agent"], r["user"]["name"] if r["user"] else ""))
    return rows[:BY_USER_LIMIT]


def _groups(by_project, projects, page, f: Filters) -> list[dict]:
    items_by_project: dict[int | None, list] = defaultdict(list)
    for item in page:
        items_by_project[item["_project_id"]].append(item)
    out = []
    for pid, counter in by_project.items():
        counts = {a: int(counter.get(a, 0)) for a in AGENTS}
        # The agent filter leaves out a group with none of that agent's work (an honest empty state).
        if sum(counts.values()) == 0 or (f.agent is not None and counts[f.agent] == 0):
            continue
        project = projects.get(pid) if pid is not None else None
        group_items = [_public_item(i) for i in items_by_project.get(pid, [])]
        out.append(
            {
                "project": (
                    {"id": project.project_id, "name": project.project_name, "code": project.code}
                    if project
                    else None
                ),
                "counts": {**counts, "total": sum(counts.values())},
                "items": group_items,
                "_sort": sum(counts.values()),
            }
        )
    out.sort(key=lambda g: (g["project"] is None, -g.pop("_sort")))
    return out


def _public_item(item: dict) -> dict:
    return {k: v for k, v in item.items() if not k.startswith("_")}


def _items(viewer_id, team_id, since, f: Filters, want: int) -> list[dict]:
    out: list[dict] = []
    if f.activity_types:
        rows = list(
            _activity_qs(viewer_id, team_id, since, f, agent=f.agent)
            .order_by("-ts_created_at", "-activity_id")
            .values(
                "activity_id",
                "ts_created_at",
                "actor_id",
                "action_type",
                "old_value",
                "new_value",
                "task_id",
                "via_json",
                "comment_id",
                "task__title",
                "task__project_id",
            )[:want]
        )
        comments = _comment_bodies(rows)
        for r in rows:
            shown = via_for_viewer(r["via_json"], r["actor_id"], viewer_id)
            out.append(
                {
                    "id": f"activity:{r['activity_id']}",
                    "at": _iso(r["ts_created_at"]),
                    "user": {"id": str(r["actor_id"]), "name": ""} if r["actor_id"] else None,
                    "agent": _agent_obj(_label_of(shown, r["actor_id"])),
                    "action": _kind_of_action(r["action_type"]),
                    "target": {"type": "task", "id": r["task_id"], "title": r["task__title"]},
                    "text": _activity_text(r, comments),
                    "_project_id": r["task__project_id"],
                    "_sort": r["ts_created_at"],
                }
            )
    if f.wants_notes:
        out.extend(_note_items(viewer_id, team_id, since, f, want))
    out.sort(key=lambda i: (i["_sort"], i["id"]), reverse=True)
    return out[:want]


def _kind_of_action(action: str) -> str:
    for kind, types in ACTIVITY_KINDS.items():
        if action in types:
            return kind
    return "task_updated"


def _comment_bodies(rows) -> dict[tuple[int, int], Any]:
    keys = {
        (r["task_id"], r["comment_id"])
        for r in rows
        if r["action_type"] == A.COMMENT_ADDED and isinstance(r["comment_id"], int)
    }
    if not keys:
        return {}
    # A deleted comment keeps its row (its id is never reissued), so its text must not be read.
    found = TaskComments.objects.filter(
        task_id__in={t for t, _ in keys}, comment_id__in={c for _, c in keys}, is_deleted=False
    ).values_list("task_id", "comment_id", "comment_body")
    return {(t, c): body for t, c, body in found if (t, c) in keys}


def _note_items(viewer_id, team_id, since, f: Filters, want: int) -> list[dict]:
    rows = list(
        _note_versions_qs(team_id, since, f)
        .order_by("-ts_created_at", "-id")
        .values(
            "id",
            "note_type",
            "note_id",
            "editor_id",
            "title",
            "via",
            "agent",
            "ts_created_at",
        )[:SCAN_LIMIT]
    )
    keys = {(r["note_type"], r["note_id"]) for r in rows}
    readable = _readable(viewer_id, team_id, keys)
    projects = _note_projects(viewer_id, team_id, keys)
    out = []
    for r in rows:
        key = (r["note_type"], r["note_id"])
        if key not in readable:
            continue
        project_id = projects.get(key)
        if f.project_id is not None and project_id != f.project_id:
            continue
        shown = via_for_viewer(r["via"], r["editor_id"], viewer_id)
        label = _label_of(shown, r["editor_id"])
        if label is None or (f.agent is not None and label != f.agent):
            continue
        out.append(
            {
                "id": f"note:{r['note_type']}:{r['note_id']}:{r['id']}",
                "at": _iso(r["ts_created_at"]),
                "user": {"id": str(r["editor_id"]), "name": ""} if r["editor_id"] else None,
                "agent": _agent_obj(label),
                "action": "note",
                "target": {
                    "type": "note",
                    "id": r["note_id"],
                    "title": r["title"],
                    "note_type": r["note_type"],
                },
                "text": None,
                "_project_id": project_id,
                "_sort": r["ts_created_at"],
            }
        )
        if len(out) >= want:
            break
    return out


def _decisions(viewer_id, team_id, since, f: Filters) -> tuple[list[dict], int]:
    masters = []
    for model, note_type in ((PersonalNoteMaster, 1), (TaskNoteMaster, 2)):
        if note_type not in DECISION_NOTE_TYPES:
            continue
        rows = (
            model.objects.filter(
                team_id=team_id, ts_created_at__gte=since, title__istartswith=DECISION_PREFIX
            )
            .order_by("-ts_created_at", "-note_id")
            .values(
                "note_id",
                "owner_id",
                "title",
                "ts_created_at",
                *(["project_id"] if note_type == 2 else []),
            )[:SCAN_LIMIT]
        )
        for r in rows:
            masters.append({**r, "note_type": note_type, "project_id": r.get("project_id")})
    keys = {(m["note_type"], m["note_id"]) for m in masters}
    readable = _readable(viewer_id, team_id, keys)
    if not readable:
        return [], 0
    first = {
        (r["note_type"], r["note_id"]): r
        for r in NoteVersionMaster.objects.filter(
            team_id=team_id,
            note_type__in=DECISION_NOTE_TYPES,
            version_no=1,
            note_id__in={nid for _, nid in readable},
        ).values("note_type", "note_id", "editor_id", "via")
    }
    out = []
    for m in masters:
        key = (m["note_type"], m["note_id"])
        if key not in readable:
            continue
        version = first.get(key, {})
        actor = version.get("editor_id") or m["owner_id"]
        label = _label_of(via_for_viewer(version.get("via"), actor, viewer_id), actor)
        if f.project_id is not None and m.get("project_id") != f.project_id:
            continue
        if f.user_id is not None and str(actor) != f.user_id:
            continue
        if f.agent is not None and label != f.agent:
            continue
        out.append(
            {
                "note_id": m["note_id"],
                "note_type": m["note_type"],
                "title": m["title"][:TEXT_CHARS],
                "at": _iso(m["ts_created_at"]),
                "user": {"id": str(actor), "name": ""} if actor else None,
                "agent": _agent_obj(label),
            }
        )
    total = len(out)
    return out[:DECISION_LIMIT], total


def _connections(viewer_id, team_id) -> list[dict]:
    rows = McpConnection.objects.filter(user_id=viewer_id, revoked_at__isnull=True).select_related(
        "application"
    )
    out = []
    for conn in rows:
        if str(team_id) not in [str(t) for t in (conn.team_ids or [])]:
            continue
        family = app_family(conn.application) or "other"
        out.append({"family": family, "last_used_at": _iso(conn.last_used_at)})
    return out
