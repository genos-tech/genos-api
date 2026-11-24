"""Team data export for a departing customer (MIP-2257). Operator command only: no endpoint, no UI.

`python manage.py export_team <team_id> --out <dir> --yes` writes one team's data to a directory:

  manifest.json            what was written (counts only), format version, options
  members.jsonl            names and roles; emails only with `--include-emails`
  projects.jsonl, tasks.jsonl, task_comments.jsonl, notes.jsonl, channels.jsonl, messages.jsonl
  files/<kind>/<id>/<name> the stored attachment files

JSON Lines, one object per line, every set read with `queryset.iterator()` and written as it goes,
so memory stays bounded however big the team is. Notes carry the BlockNote JSON and a Markdown body.
Secrets are never exported: no tokens, key hashes, OAuth or webhook secrets, password data.
The security event for the run is written by the command, not here.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from pathlib import Path

from django.core.files.storage import default_storage

log = logging.getLogger(__name__)

EXPORT_FORMAT_VERSION = 1
_CHUNK = 500


def _iso(value):
    return value.isoformat() if value else None


def _safe_name(name: str) -> str:
    """A file name safe to create on disk: no path parts, no odd characters."""
    base = os.path.basename(str(name or "")).strip().replace("\x00", "")
    base = re.sub(r"[^\w.\- ]", "_", base)[:120].lstrip(".")
    return base or "file"


class _Writer:
    def __init__(self, out: Path):
        self.out = out
        self.counts: Counter = Counter()

    def jsonl(self, name: str, rows) -> None:
        with open(self.out / f"{name}.jsonl", "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, default=str))
                fh.write("\n")
                self.counts[name] += 1

    def copy_file(self, kind: str, ident, stored_name, display_name) -> str | None:
        """Copy one stored file in chunks. Returns its path inside the archive, or None if missing."""
        if not stored_name:
            return None
        rel = Path("files") / kind / str(ident) / _safe_name(display_name or stored_name)
        dest = self.out / rel
        try:
            with default_storage.open(str(stored_name), "rb") as src:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as dst:
                    for chunk in iter(lambda: src.read(1024 * 1024), b""):
                        dst.write(chunk)
        except Exception:  # noqa: BLE001 - one missing file must not stop the export
            self.counts["files missing"] += 1
            log.warning("team export: a stored file could not be copied")
            return None
        self.counts["files"] += 1
        return rel.as_posix()


def _markdown(body) -> str:
    from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown

    try:
        return blocks_to_markdown(body) if body else ""
    except Exception:  # noqa: BLE001 - the JSON body is exported either way
        return ""


def _user(u, include_emails: bool) -> dict:
    row = {"user_id": str(u.id), "username": u.username}
    if include_emails:
        row["email"] = u.email
    return row


def export_team(team_id, out: Path, *, include_emails: bool = False) -> dict:
    """Write the archive; returns the manifest (counts only)."""
    from origin.models.chat.unified_models import Channel, Message, MessageAttachment
    from origin.models.common.team_models import TeamMaster, TeamMembers
    from origin.models.note.chat_note_models import ChatNoteMaster
    from origin.models.note.personal_note_models import PersonalNoteMaster
    from origin.models.note.task_note_models import TaskNoteMaster
    from origin.models.project.prj_models import ProjectMaster, ProjectMembers
    from origin.models.task.task_models import TaskAttachments, TaskComments, TaskMaster

    team = TeamMaster._base_manager.get(team_id=team_id)
    out.mkdir(parents=True, exist_ok=True)
    w = _Writer(out)

    def members():
        qs = TeamMembers.objects.filter(team_id=team_id, attendee__is_system_user=False)
        for m in qs.select_related("attendee").iterator(chunk_size=_CHUNK):
            yield {
                **_user(m.attendee, include_emails),
                "role": m.member_role,
                "removed": bool(m.is_deleted),
            }

    def projects():
        for p in ProjectMaster._base_manager.filter(team=team_id).iterator(chunk_size=_CHUNK):
            yield {
                "project_id": p.project_id,
                "name": p.project_name,
                "is_private": p.is_private,
                "is_deleted": p.is_deleted,
                "archived_at": _iso(p.archived_at),
                "members": [
                    {"user_id": str(pm.attendee_id), "role": pm.member_role}
                    for pm in ProjectMembers.objects.filter(project=p)
                ],
            }

    def tasks():
        qs = TaskMaster.objects.filter(team=team_id).prefetch_related("collaborators")
        for t in qs.iterator(chunk_size=_CHUNK):
            files = [
                {
                    "name": a.original_filename or os.path.basename(a.attached_file.name),
                    "type": a.attached_type,
                    "size_bytes": a.size_bytes,
                    "file": w.copy_file(
                        "task", t.task_id, a.attached_file.name, a.original_filename
                    ),
                }
                for a in TaskAttachments.objects.filter(task=t)
            ]
            yield {
                "task_id": t.task_id,
                "project_id": t.project_id,
                "parent_task_id": t.parent_task_id,
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "effort_level": t.effort_level,
                "assignee_id": str(t.assignee_id) if t.assignee_id else None,
                "reporter_id": str(t.reporter_id) if t.reporter_id else None,
                "collaborator_ids": [str(c.id) for c in t.collaborators.all()],
                "start_date": _iso(t.start_date),
                "due_date": _iso(t.due_date),
                "tags": t.tags,
                "links": t.links,
                "content": t.content,
                "content_markdown": _markdown(t.content),
                "attachments": files,
            }

    def comments():
        qs = TaskComments.objects.filter(task__team=team_id).order_by("task_id", "comment_id")
        for c in qs.iterator(chunk_size=_CHUNK):
            yield {
                "task_id": c.task_id,
                "comment_id": c.comment_id,
                "sender_id": str(c.sender_id) if c.sender_id else None,
                "body": c.comment_body,
                "body_markdown": _markdown(c.comment_body),
                "is_deleted": c.is_deleted,
                "sent_at": _iso(c.ts_sent_at),
            }

    def notes():
        for kind, model, extra in (
            ("personal", PersonalNoteMaster, lambda n: {"folder_id": n.folder_id}),
            ("task", TaskNoteMaster, lambda n: {"task_id": n.task_id}),
            ("chat", ChatNoteMaster, lambda n: {"channel_id": str(n.channel_id)}),
        ):
            for n in model.objects.filter(team=team_id).iterator(chunk_size=_CHUNK):
                yield {
                    "kind": kind,
                    "note_id": n.note_id,
                    "title": n.title,
                    "owner_id": str(n.owner_id) if n.owner_id else None,
                    "body_format": n.body_format,
                    "body": n.body,
                    "body_markdown": _markdown(n.body) if n.body_format == "blocknote" else "",
                    "created_at": _iso(n.ts_created_at),
                    "updated_at": _iso(n.ts_updated_at),
                    **extra(n),
                }

    def channels():
        for ch in Channel._base_manager.filter(team=team_id).iterator(chunk_size=_CHUNK):
            yield {
                "channel_id": str(ch.id),
                "kind": ch.kind,
                "title": ch.title,
                "description": ch.description,
                "project_id": ch.project_id,
                "is_private": ch.is_private,
                "members": [
                    {"user_id": str(m.user_id), "role": m.role}
                    for m in ch.members.filter(is_deleted=False)
                ],
            }

    def messages():
        qs = Message.objects.filter(channel__team=team_id).order_by("channel_id", "seq")
        for m in qs.iterator(chunk_size=_CHUNK):
            files = [
                {
                    "name": a.original_name,
                    "mime": a.mime,
                    "size_bytes": a.size_bytes,
                    "file": w.copy_file("message", m.id, a.file.name, a.original_name),
                }
                for a in MessageAttachment.objects.filter(message=m)
            ]
            yield {
                "message_id": str(m.id),
                "channel_id": str(m.channel_id),
                "seq": m.seq,
                "sender_id": str(m.sender_id) if m.sender_id else None,
                "parent_id": str(m.parent_id) if m.parent_id else None,
                "task_id": m.task_id,
                "body": m.body,
                "text": m.body_text,
                "attachments": files,
            }

    for name, rows in (
        ("members", members()),
        ("projects", projects()),
        ("tasks", tasks()),
        ("task_comments", comments()),
        ("notes", notes()),
        ("channels", channels()),
        ("messages", messages()),
    ):
        w.jsonl(name, rows)

    manifest = {
        "format_version": EXPORT_FORMAT_VERSION,
        "team_id": str(team.team_id),
        "include_emails": include_emails,
        "counts": dict(w.counts),
    }
    with open(out / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest
