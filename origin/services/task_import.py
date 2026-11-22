"""Import tasks from a CSV file: generic, Jira export, Notion database export (MIP-1835).

The importer is ONE importer with a column mapping. The Jira and Notion exports are two
auto-detected *profiles* of it: they only decide the proposed mapping and how the parent and the
external id are read. The person can change every column in the mapping step.

Layers, each testable alone:
  decode / parse / detect_profile / propose_mapping   the file and its headers (pure)
  map_status / map_priority / parse_date / clean_title  one cell (pure)
  resolve_rows                                         the whole file against a project (reads)
  commit                                               creates the tasks (writes)
  undo_plan / undo                                     removes what a run made

Rules the whole module keeps:
* The file is never stored, only its sha256. The report keeps row numbers and codes, never text.
* Messages are CODES (`{"code": "status_unknown", "value": "Weird"}`): the client words them.
* Each row is created in its own savepoint: a failing row is reported and the rest still import.
* Tasks are created one by one through `TaskMaster.objects.create`, never `bulk_create`, so the
  numbering, history, change feed and webhook signals run as for any task.
* A row that was imported before is skipped, by `row_key` (external id, else the values).
* Assignees are matched by e-mail among the project's members only; anything else is left
  unassigned with a warning (Notion exports names, not e-mails).
* No dependency is added: `Pipfile` is on the retrieval path list.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from collections import Counter
from datetime import date

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMembers, ProjectTags
from origin.models.task.task_import_models import TaskImportRow, TaskImportRun
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools.blocknote_md import task_body_blocks
from origin.search_engine.quota import TASK_CREATE_KEY, increment_usage
from origin.services.blind_index import USER_EMAIL
from origin.services.member_roles import is_guest_role
from origin.services.task_cache import invalidate_project_tasks_cache
from origin.signals.task_signals import update_tasks_recorded

logger = logging.getLogger(__name__)

MAX_ROWS = 1000
MAX_BYTES = 5 * 1024 * 1024
MAX_NEW_TAGS = 50
TAG_NAME_MAX = 20
TITLE_MAX = 255
DESCRIPTION_MAX = 20000
PREVIEW_ROWS = 200

FIELDS = (
    "title",
    "description",
    "status",
    "priority",
    "assignee",
    "due_date",
    "start_date",
    "tags",
    "parent",
    "external_id",
)
MULTI_FIELDS = ("tags", "parent", "external_id")

STATUSES = ("Open", "WIP", "Blocked", "Pending", "Closed")
PRIORITIES = ("Minimal", "Low", "Normal", "High", "Critical")


class ImportError_(Exception):
    """A file that cannot be imported at all: `code` is what the client words."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


# ── the file ────────────────────────────────────────────────────────────────


def decode(data: bytes) -> str:
    """UTF-8 (with or without a BOM), then Shift-JIS (cp932), the two a Japanese Excel writes."""
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ImportError_("bad_encoding")


def parse(text: str) -> tuple[list[str], list[list[str]]]:
    """(headers, rows) as lists, so a repeated header (Jira's `Labels`) keeps every column.
    Blank rows are dropped. The delimiter is sniffed from the header line."""
    first = text.split("\n", 1)[0]
    delimiter = (
        max(",;\t", key=first.count)
        if first.count(",") + first.count(";") + first.count("\t")
        else ","
    )
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    records: list[list[str]] = []
    seen = 0
    try:
        for record in reader:
            seen += 1
            # Blank lines count too, so a file of newlines cannot grow the list past the cap.
            if seen > MAX_ROWS + 1:
                raise ImportError_("too_many_rows")
            if any(c.strip() for c in record):
                records.append(record)
    except csv.Error as exc:
        raise ImportError_("bad_csv") from exc
    if not records:
        raise ImportError_("empty_file")
    headers = [h.strip() for h in records[0]]
    return headers, records[1:]


def detect_profile(headers: list[str]) -> str:
    lower = {h.lower() for h in headers}
    if {"issue key", "summary"} <= lower:
        return "jira"
    if "name" in lower and lower & {"created time", "parent item", "tags", "last edited time"}:
        return "notion"
    return "generic"


_SYNONYMS = {
    "title": (
        "title",
        "summary",
        "name",
        "task",
        "task name",
        "タイトル",
        "件名",
        "名前",
        "タスク名",
        "タスク",
    ),
    "description": ("description", "details", "notes", "body", "説明", "詳細", "内容"),
    "status": ("status", "state", "ステータス", "状態", "状況"),
    "priority": ("priority", "優先度", "優先順位"),
    "assignee": ("assignee", "assigned to", "owner", "担当", "担当者"),
    "due_date": ("due date", "due", "deadline", "due on", "期限", "締切", "期日"),
    "start_date": ("start date", "start", "開始日"),
    "tags": ("tags", "labels", "label", "tag", "ラベル", "タグ"),
    "parent": ("parent", "parent id", "parent item", "parent task", "parent key", "親タスク", "親"),
    "external_id": ("issue key", "issue id", "id", "key"),
}


def propose_mapping(headers: list[str], profile: str) -> dict[str, list[int]]:
    """{field: [column index, ...]}. A single-column field takes the first matching header."""
    lower = [h.strip().lower() for h in headers]
    mapping: dict[str, list[int]] = {}
    for field, names in _SYNONYMS.items():
        found = [i for i, h in enumerate(lower) if h in names]
        if field == "external_id" and profile == "jira":
            found = [i for i in found if lower[i] in ("issue key", "issue id")]
        elif field == "external_id" and profile == "notion":
            found = []
        if found:
            mapping[field] = found if field in MULTI_FIELDS else found[:1]
    return mapping


def clean_mapping(raw, n_columns: int) -> dict[str, list[int]]:
    """A mapping from the client: known fields and in-range column indexes only."""
    out: dict[str, list[int]] = {}
    if not isinstance(raw, dict):
        return out
    for field in FIELDS:
        cols = raw.get(field)
        if isinstance(cols, int) and not isinstance(cols, bool):
            cols = [cols]
        if not isinstance(cols, list):
            continue
        ok = [
            c for c in cols if isinstance(c, int) and not isinstance(c, bool) and 0 <= c < n_columns
        ]
        if ok:
            out[field] = ok if field in MULTI_FIELDS else ok[:1]
    return out


# ── one cell ────────────────────────────────────────────────────────────────

_STATUS_WORDS = {
    "Open": (
        "to do",
        "todo",
        "open",
        "backlog",
        "not started",
        "new",
        "selected for development",
        "未着手",
        "オープン",
    ),
    "WIP": (
        "in progress",
        "wip",
        "doing",
        "in review",
        "in development",
        "started",
        "進行中",
        "対応中",
    ),
    "Blocked": ("blocked", "impediment", "ブロック", "ブロック中"),
    "Pending": ("pending", "on hold", "waiting", "hold", "保留", "待機中"),
    "Closed": (
        "done",
        "closed",
        "resolved",
        "complete",
        "completed",
        "finished",
        "完了",
        "終了",
        "クローズ",
    ),
}
_STATUS_LOOKUP = {w: s for s, words in _STATUS_WORDS.items() for w in words}
_PRIORITY_WORDS = {
    "Critical": ("highest", "blocker", "critical", "urgent", "最高", "緊急"),
    "High": ("high", "major", "高"),
    "Normal": ("medium", "normal", "default", "中"),
    "Low": ("low", "minor", "低"),
    "Minimal": ("lowest", "trivial", "minimal", "最低"),
}
_PRIORITY_LOOKUP = {w: p for p, words in _PRIORITY_WORDS.items() for w in words}


def map_status(raw: str, overrides: dict | None = None) -> tuple[str | None, str | None]:
    """(status, warning code). Empty -> (None, None): the task takes the default."""
    key = (raw or "").strip().lower()
    if not key:
        return None, None
    if overrides and overrides.get(key) in STATUSES:
        return overrides[key], None
    if key in _STATUS_LOOKUP:
        return _STATUS_LOOKUP[key], None
    return "Open", "status_unknown"


def map_priority(raw: str, overrides: dict | None = None) -> tuple[str | None, str | None]:
    key = (raw or "").strip().lower()
    if not key:
        return None, None
    if overrides and overrides.get(key) == "none":  # the person chose "no priority" for it
        return None, None
    if overrides and overrides.get(key) in PRIORITIES:
        return overrides[key], None
    if key in _PRIORITY_LOOKUP:
        return _PRIORITY_LOOKUP[key], None
    return None, "priority_unknown"


_MONTHS = {
    m: i + 1
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ]
    )
    for m in names
}


def _make_date(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(raw: str) -> tuple[date | None, str | None]:
    """(date, warning code). Day-first vs month-first is never guessed: `03/12/2026` is a
    warning and no date, `25/12/2026` and `12/25/2026` are not ambiguous."""
    text = (raw or "").strip()
    if not text:
        return None, None
    patterns = (
        (
            r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)",
            lambda g: _make_date(int(g[0]), int(g[1]), int(g[2])),
        ),
        (
            r"^(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日",
            lambda g: _make_date(int(g[0]), int(g[1]), int(g[2])),
        ),
        (
            r"^(\d{1,2})[-/ ]([A-Za-z]{3,9})\.?[-/ ,]+(\d{2,4})(?!\d)",
            lambda g: _make_date(int(g[2]), _MONTHS.get(g[1].lower(), 0), int(g[0])),
        ),
        (
            r"^([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})",
            lambda g: _make_date(int(g[2]), _MONTHS.get(g[0].lower(), 0), int(g[1])),
        ),
    )
    for pattern, build in patterns:
        m = re.match(pattern, text)
        if m:
            found = build(m.groups())
            return (found, None) if found else (None, "date_unparsable")
    m = re.match(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})(?!\d)", text)
    if m:
        a, b, y = (int(x) for x in m.groups())
        if a > 12 and b <= 12:
            found = _make_date(y, b, a)
        elif b > 12 and a <= 12:
            found = _make_date(y, a, b)
        elif a == b:
            found = _make_date(y, a, b)
        else:
            return None, "date_ambiguous"
        return (found, None) if found else (None, "date_unparsable")
    return None, "date_unparsable"


def clean_title(raw: str) -> str:
    """The title as stored: trimmed, and no leading `= + @ tab CR`, so a title that came out
    of a spreadsheet as a formula is plain text if it ever goes back into one."""
    return (raw or "").strip().lstrip("=+@\t\r ").strip()


def _split_tags(values: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for value in values:
        for part in re.split(r"[,;、]", value or ""):
            name = part.strip()[:TAG_NAME_MAX]
            if name:
                seen.setdefault(name.lower(), name)
    return list(seen.values())


def _strip_url_suffix(text: str) -> str:
    """Notion writes a parent as `Title (https://www.notion.so/...)`."""
    return re.sub(r"\s*\(https?://[^)]*\)\s*$", "", text or "").strip()


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


# ── the whole file against a project ────────────────────────────────────────


def _member_ids(project) -> set[str]:
    ids = {
        str(m.attendee_id)
        for m in ProjectMembers.objects.filter(project_id=project.project_id)
        if m.attendee_id and not is_guest_role(m.member_role)
    }
    if project.owner_id:
        ids.add(str(project.owner_id))
    return ids


def _cell(row: list[str], cols: list[int] | None) -> str:
    for c in cols or []:
        if c < len(row) and row[c].strip():
            return row[c].strip()
    return ""


def _cells(row: list[str], cols: list[int] | None) -> list[str]:
    return [row[c] for c in cols or [] if c < len(row) and row[c].strip()]


def resolve_rows(
    project,
    profile: str,
    rows: list[list[str]],
    mapping: dict[str, list[int]],
    *,
    status_map: dict | None = None,
    priority_map: dict | None = None,
) -> dict:
    """Every row of the file resolved against `project`; reads, writes nothing.

    Returns {"rows": [...], "new_tags": [...], "status_values": [...], "priority_values": [...]}.
    A row is {"row": n (file record number, the header is 1), "title", "errors", "warnings",
    "already_imported", ...resolved fields, "_key", "_parent_row"}.
    """
    members = _member_ids(project)
    catalogue = {
        t.tag_name.lower(): t for t in ProjectTags.objects.filter(project_id=project.project_id)
    }
    email_cache: dict[str, tuple[CustomUser | None, str | None]] = {}

    def lookup_assignee(raw: str):
        if "@" not in raw:
            return None, "assignee_not_email"
        key = raw.lower()
        if key not in email_cache:
            user = CustomUser.objects.filter(USER_EMAIL.matches(raw)).first()
            if user is None:
                email_cache[key] = (None, "assignee_unknown")
            elif str(user.id) not in members:
                # The same answer as for no account at all: an editor must not learn which
                # e-mail addresses have an account elsewhere on Genos.
                email_cache[key] = (None, "assignee_unknown")
            else:
                email_cache[key] = (user, None)
        return email_cache[key]

    out: list[dict] = []
    ext_index: dict[str, int] = {}
    title_index: dict[str, list[int]] = {}
    status_values: Counter = Counter()
    priority_values: Counter = Counter()
    new_tags: dict[str, str] = {}
    seen_values: Counter = Counter()
    seen_ext: set[str] = set()

    for i, row in enumerate(rows):
        number = i + 2
        errors: list[dict] = []
        warnings: list[dict] = []

        raw_title = _cell(row, mapping.get("title"))
        title = clean_title(raw_title)
        if not title:
            errors.append({"code": "title_missing"})
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX]
            warnings.append({"code": "title_truncated"})

        description = _cell(row, mapping.get("description"))
        if len(description) > DESCRIPTION_MAX:
            description = description[:DESCRIPTION_MAX]
            warnings.append({"code": "description_truncated"})

        raw_status = _cell(row, mapping.get("status"))
        raw_priority = _cell(row, mapping.get("priority"))
        if raw_status:
            status_values[raw_status] += 1
        if raw_priority:
            priority_values[raw_priority] += 1
        task_status, w = map_status(raw_status, status_map)
        if w:
            warnings.append({"code": w, "value": raw_status})
        priority, w = map_priority(raw_priority, priority_map)
        if w:
            warnings.append({"code": w, "value": raw_priority})

        due, w = parse_date(_cell(row, mapping.get("due_date")))
        if w:
            warnings.append(
                {"code": w, "field": "due_date", "value": _cell(row, mapping.get("due_date"))}
            )
        start, w = parse_date(_cell(row, mapping.get("start_date")))
        if w:
            warnings.append(
                {"code": w, "field": "start_date", "value": _cell(row, mapping.get("start_date"))}
            )

        raw_assignee = _cell(row, mapping.get("assignee"))
        assignee, assignee_email = None, ""
        if raw_assignee:
            assignee, w = lookup_assignee(raw_assignee)
            if w:
                warnings.append({"code": w, "value": raw_assignee})
            else:
                assignee_email = raw_assignee
        tags = _split_tags(_cells(row, mapping.get("tags")))
        tag_names = []
        for name in tags:
            known = catalogue.get(name.lower())
            if known is None:
                new_tags.setdefault(name.lower(), name)
                tag_names.append(name)
            else:
                tag_names.append(known.tag_name)

        ext_ids = _cells(row, mapping.get("external_id"))
        ext = ext_ids[0].strip() if ext_ids else ""
        for value in ext_ids:
            ext_index.setdefault(value.strip(), number)
        title_index.setdefault(title.lower(), []).append(number)
        parent_ref = _strip_url_suffix(_cell(row, mapping.get("parent")))

        if ext:
            key = _sha(profile, "ext", ext)
            if key in seen_ext:
                errors.append({"code": "duplicate_in_file"})
            seen_ext.add(key)
        else:
            values = _sha(
                profile,
                title,
                description,
                raw_status,
                raw_priority,
                raw_assignee,
                str(due),
                parent_ref,
            )
            seen_values[values] += 1
            key = _sha(values, str(seen_values[values]))

        out.append(
            {
                "row": number,
                "title": title,
                "description": description,
                "status": task_status or "Open",
                "priority": priority,
                "due_date": due.isoformat() if due else None,
                "start_date": start.isoformat() if start else None,
                "assignee_email": assignee_email,
                "assignee_id": str(assignee.id) if assignee else None,
                "tags": tag_names,
                "external_id": ext,
                "external_ids": [e.strip() for e in ext_ids],
                "parent_ref": parent_ref,
                "parent_row": None,
                "errors": errors,
                "warnings": warnings,
                "already_imported": False,
                "_key": key,
            }
        )

    # parents: by external id in the file, else by a unique title (Notion), else an earlier import
    known_keys = set(
        TaskImportRow.objects.filter(
            project_id=project.project_id, row_key__in=[r["_key"] for r in out]
        ).values_list("row_key", flat=True)
    )
    for r in out:
        r["already_imported"] = r["_key"] in known_keys
        ref = r["parent_ref"]
        if not ref:
            continue
        target = ext_index.get(ref)
        if target is None:
            matches = title_index.get(clean_title(ref).lower(), [])
            target = matches[0] if len(matches) == 1 else None
        if target is None or target == r["row"]:
            if earlier_parent(project, ref) is None:
                r["warnings"].append({"code": "parent_missing", "value": ref})
            continue
        r["parent_row"] = target

    # break cycles: the first row found in one loses its parent
    by_row = {r["row"]: r for r in out}
    for r in out:
        seen, cur = {r["row"]}, r["parent_row"]
        while cur is not None:
            if cur == r["row"]:
                r["parent_row"] = None
                r["warnings"].append({"code": "parent_cycle", "value": r["parent_ref"]})
                break
            if cur in seen:
                break
            seen.add(cur)
            cur = by_row[cur]["parent_row"]

    return {
        "rows": out,
        "new_tags": list(new_tags.values()),
        "status_values": [
            {"value": v, "count": c, "mapped": map_status(v, status_map)[0]}
            for v, c in status_values.most_common()
        ],
        "priority_values": [
            {"value": v, "count": c, "mapped": map_priority(v, priority_map)[0]}
            for v, c in priority_values.most_common()
        ],
    }


def earlier_parent(project, ref: str):
    """A live task an EARLIER import made that `ref` names: by one of the ids its row carried
    (Jira's `Parent id` is the issue id, not the key), else by its title when only one task of
    the project's imports has it (Notion has no id). None when there is none or it is not one."""
    base = TaskImportRow.objects.filter(project_id=project.project_id).exclude(
        task__status="Deleted"
    )
    row = base.filter(external_ids__contains=[ref]).select_related("task").first()
    if row is None:
        title = clean_title(ref)
        if not title:
            return None
        rows = list(base.filter(task__title__iexact=title).select_related("task")[:2])
        row = rows[0] if len(rows) == 1 else None
    return row.task if row is not None else None


def public_row(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def counts(resolved: dict) -> dict:
    rows = resolved["rows"]
    errors = sum(1 for r in rows if r["errors"])
    skipped = sum(1 for r in rows if not r["errors"] and r["already_imported"])
    return {
        "rows": len(rows),
        "errors": errors,
        "warnings": sum(1 for r in rows if r["warnings"]),
        "already_imported": skipped,
        "to_create": len(rows) - errors - skipped,
    }


# ── commit ──────────────────────────────────────────────────────────────────


def _fingerprint(task: TaskMaster) -> str:
    return _sha(
        json.dumps(
            [
                task.title,
                task.content,
                task.status,
                task.priority,
                str(task.assignee_id or ""),
                str(task.due_date or ""),
            ],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    )


def _create_order(rows: list[dict]) -> list[dict]:
    """Parents before children, so a child is created with its parent's id (cycles are already
    broken by `resolve_rows`, so the walk ends)."""
    by_row = {r["row"]: r for r in rows}
    ordered: list[dict] = []
    done: set[int] = set()
    for r in rows:
        chain, cur = [], r
        while cur is not None and cur["row"] not in done:
            chain.append(cur)
            cur = by_row.get(cur["parent_row"])
        for item in reversed(chain):
            ordered.append(item)
            done.add(item["row"])
    return ordered


def commit(
    *,
    project,
    user,
    profile: str,
    filename: str,
    file_sha256: str,
    mapping: dict,
    resolved: dict,
    create_tags: bool,
) -> TaskImportRun:
    """Create the tasks. Rows with errors and rows imported before are not created."""
    rows = resolved["rows"]
    if create_tags and resolved["new_tags"]:
        _create_tags(project, resolved["new_tags"][:MAX_NEW_TAGS])
    catalogue = {
        t.tag_name.lower(): t for t in ProjectTags.objects.filter(project_id=project.project_id)
    }

    report: list[dict] = []
    created_tasks: dict[int, TaskMaster] = {}
    created = skipped = failed = 0
    todo = []
    for r in rows:
        if r["errors"]:
            failed += 1
            report.append({"row": r["row"], "outcome": "failed", "code": r["errors"][0]["code"]})
        elif r["already_imported"]:
            skipped += 1
            report.append({"row": r["row"], "outcome": "skipped", "code": "already_imported"})
        else:
            todo.append(r)

    run = TaskImportRun.objects.create(
        team_id=project.team_id,
        project=project,
        created_by=user,
        profile=profile,
        filename=filename[:255],
        file_sha256=file_sha256,
        mapping=mapping,
    )
    all_rows = {r["row"]: r for r in rows}
    try:
        for r in _create_order(todo):
            tags = [
                {
                    "tagName": catalogue[t.lower()].tag_name,
                    "tagColor": catalogue[t.lower()].tag_color,
                    "tagTextColor": catalogue[t.lower()].tag_text_color,
                }
                for t in r["tags"]
                if t.lower() in catalogue
            ]
            kwargs = {
                "team_id": project.team_id,
                "project": project,
                "reporter": user,
                "title": r["title"],
                "status": r["status"],
            }
            if r["description"]:
                kwargs["content"] = task_body_blocks(r["description"])
            if r["assignee_id"]:
                kwargs["assignee_id"] = r["assignee_id"]
            if r["priority"]:
                kwargs["priority"] = r["priority"]
            if r["due_date"]:
                kwargs["due_date"] = r["due_date"]
            if r["start_date"]:
                kwargs["start_date"] = r["start_date"]
            if tags:
                kwargs["tags"] = tags
            parent_task = created_tasks.get(r["parent_row"]) if r["parent_row"] else None
            if parent_task is None and r["parent_ref"]:
                parent_task = earlier_parent(project, r["parent_ref"])
            if parent_task is not None:
                kwargs["parent_task_id"] = parent_task.task_id
            try:
                with transaction.atomic():
                    task = TaskMaster.objects.create(**kwargs)
                    TaskImportRow.objects.create(
                        run=run,
                        task=task,
                        project=project,
                        row_key=r["_key"],
                        external_ids=r["external_ids"],
                        fingerprint=_fingerprint(task),
                    )
                    n_comments = _create_comments(task, user, project, r.get("_comments") or [])
            except IntegrityError:
                skipped += 1
                report.append({"row": r["row"], "outcome": "skipped", "code": "already_imported"})
                continue
            except Exception:  # noqa: BLE001 - one bad row must not stop the rest
                logger.exception("task import: row %s failed", r["row"])
                failed += 1
                report.append({"row": r["row"], "outcome": "failed", "code": "create_failed"})
                continue
            created_tasks[r["row"]] = task
            created += 1
            entry = {
                "row": r["row"],
                "outcome": "created",
                "task_id": task.task_id,
                "display_id": task.display_id,
            }
            if n_comments:
                entry["comments"] = n_comments  # a count, never the text
            report.append(entry)
            if r["parent_row"] and parent_task is None and all_rows.get(r["parent_row"]):
                report.append({"row": r["row"], "outcome": "warning", "code": "parent_failed"})
    finally:
        report.sort(key=lambda x: x["row"])
        run.created_count, run.skipped_count, run.failed_count = created, skipped, failed
        run.report = report
        run.save(update_fields=["created_count", "skipped_count", "failed_count", "report"])
        if created:
            # In `finally`: a request that dies partway (a worker timeout raises into this frame)
            # still records and charges the tasks it already made.
            increment_usage(str(user.id), TASK_CREATE_KEY, created)
            invalidate_project_tasks_cache(project.team_id, project.project_id)
    return run


def _comment_markdown(comment: dict) -> str:
    """The comment's text under a first line that keeps who wrote it and when, as text."""
    author = re.sub(r"[_*\[\]`<>]", "", comment.get("author") or "").strip()
    when = re.sub(r"[^0-9-]", "", comment.get("created") or "")
    who = ", ".join(x for x in (author, when) if x)
    head = f"_Imported from Jira ({who})_" if who else "_Imported from Jira_"
    return f"{head}\n\n{comment['markdown']}"


def _create_comments(task: TaskMaster, user, project, comments: list[dict]) -> int:
    """Comments of an imported issue, written the way the comment API writes them (the row, and
    its mirror under the task's chat card) but NOT `fan_out_task_comment`: an import must not
    notify anyone, call the webhook or push to genos-sockets. Ids 1..n: `undo_plan` relies on it
    to tell these from a comment a person added later."""
    from origin.services.unified_writer import write_task_comment_as_thread_reply  # noqa: PLC0415

    for i, comment in enumerate(comments, 1):
        row = TaskComments.objects.create(
            task=task,
            sender=user,
            comment_id=i,
            comment_body=task_body_blocks(_comment_markdown(comment)),
        )
        write_task_comment_as_thread_reply(
            task_id=task.task_id,
            comment_id=row.comment_id,
            sender_id=str(user.id),
            body=row.comment_body,
            project_id=project.project_id,
            bypass_flag=True,
        )
    return len(comments)


def _create_tags(project, names: list[str]) -> None:
    existing = {
        t.lower()
        for t in ProjectTags.objects.filter(project_id=project.project_id).values_list(
            "tag_name", flat=True
        )
    }
    next_id = (
        ProjectTags.objects.filter(project_id=project.project_id).aggregate(m=Max("tag_id"))["m"]
        or 0
    )
    for name in names:
        if name.lower() in existing:
            continue
        next_id += 1
        try:
            with transaction.atomic():
                ProjectTags.objects.create(
                    team_id=project.team_id,
                    project=project,
                    tag_id=next_id,
                    tag_name=name,
                    tag_color="#E5E7EB",
                    tag_text_color="#111827",
                )
        except IntegrityError:
            pass  # a concurrent import made the same tag
        existing.add(name.lower())


# ── undo ────────────────────────────────────────────────────────────────────


EDITED_COMMENT_SECONDS = 5


def _person_commented(task: TaskMaster, imported: int) -> bool:
    """A comment the import did not make (ids 1..imported are its own), or one it made that was
    edited since (an update is stamped well after the send)."""
    comments = TaskComments.objects.filter(task=task, is_deleted=False)
    if comments.filter(comment_id__gt=imported).exists():
        return True
    return any(
        (c.ts_updated_at - c.ts_sent_at).total_seconds() > EDITED_COMMENT_SECONDS
        for c in comments.filter(comment_id__lte=imported)
    )


def undo_plan(run: TaskImportRun) -> dict:
    """Which of the run's tasks an undo removes and which stay: a task someone edited since
    (title, body, status, priority, assignee, due date) or commented on is the person's now, and
    so is any task that still has a live child that stays (nothing is left without its parent)."""
    markers = list(run.rows.select_related("task"))
    imported = {e["task_id"]: e["comments"] for e in run.report if e.get("comments")}
    keep: set[int] = set()
    for marker in markers:
        task = marker.task
        if task.status == "Deleted":
            continue  # already gone: only its marker is cleared
        if _fingerprint(task) != marker.fingerprint or _person_commented(
            task, imported.get(task.task_id, 0)
        ):
            keep.add(task.task_id)
    remove_ids = {m.task_id for m in markers} - keep
    while True:  # a parent with a child that stays, or one nobody here made, stays
        blocked = set(
            TaskMaster.objects.filter(parent_task_id__in=remove_ids)
            .exclude(status="Deleted")
            .exclude(task_id__in=remove_ids)
            .values_list("parent_task_id", flat=True)
        )
        if not blocked:
            break
        keep |= blocked
        remove_ids -= blocked
    remove = [m for m in markers if m.task_id in remove_ids]
    kept = [m for m in markers if m.task_id in keep]
    return {"remove": remove, "kept": kept}


def undo(run: TaskImportRun) -> dict:
    plan = undo_plan(run)
    live = [m.task_id for m in plan["remove"] if m.task.status != "Deleted"]
    with transaction.atomic():
        if live:
            update_tasks_recorded(
                TaskMaster.objects.filter(pk__in=live), cascade="import_undo", status="Deleted"
            )
        TaskImportRow.objects.filter(pk__in=[m.pk for m in plan["remove"]]).delete()
        run.undone_at = timezone.now()
        run.save(update_fields=["undone_at"])
    if live:
        invalidate_project_tasks_cache(run.team_id, run.project_id)
    return {"removed": len(live), "kept": len(plan["kept"])}
