"""How many bytes of uploads each team holds (MIP-1962). Measurement only: nothing here refuses an upload.

ONE registry, `KINDS`, says which models hold an upload, which column holds its size in bytes and how
the row maps to a team. The team owner's figure (`TeamStorageUsageView`), the operator report
(`team_storage_report`) and the backfill (`backfill_storage_sizes`) all read it, so a new upload
kind is added in one place; `test_storage_usage.RegistryTests` fails if a FileField is missing.

What counts: every row with a stored file, soft-deleted ones too (their bytes are still on disk).
What does not: inline chat images (`chats/<id>/inline/`, no row), and the file of a PM channel
(`Channel.profile_image_url` points at the project's file, which the project already counts).

Buckets besides a team: a user's avatar belongs to a person, not a team (`unattributed`), as does a
note attachment whose note has no team; a default emoji has no team (`system`).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.apps import apps
from django.db.models import Count, F, Sum
from django.db.models.functions import Coalesce

UNATTRIBUTED = "unattributed"
SYSTEM = "system"


@dataclass(frozen=True)
class Kind:
    label: str
    model_label: str
    file_attname: str
    size_attname: str
    # The `values()` paths that name the team; the first non-null wins. Empty = never a team's.
    team_paths: tuple[str, ...]
    # Where a row with no team goes.
    no_team_bucket: str = UNATTRIBUTED

    @property
    def model(self):
        return apps.get_model(self.model_label)


KINDS: tuple[Kind, ...] = (
    Kind(
        "task_attachment",
        "origin.TaskAttachments",
        "attached_file",
        "size_bytes",
        ("task__team_id", "task__project__team_id"),
    ),
    Kind(
        "task_body_attachment",
        "origin.TaskBodyAttachmentFact",
        "body_attachment_url",
        "size_bytes",
        ("task__team_id", "task__project__team_id"),
    ),
    Kind(
        "task_note_attachment",
        "origin.TaskNoteAttachmentFact",
        "note_attachment_url",
        "size_bytes",
        ("note__team_id", "task__team_id"),
    ),
    Kind(
        "chat_note_attachment",
        "origin.ChatNoteAttachmentFact",
        "note_attachment_url",
        "size_bytes",
        ("note__team_id", "channel__team_id"),
    ),
    Kind(
        "personal_note_attachment",
        "origin.PersonalNoteAttachmentFact",
        "note_attachment_url",
        "size_bytes",
        ("note__team_id",),
    ),
    Kind(
        "message_attachment",
        "origin.MessageAttachment",
        "file",
        "size_bytes",
        ("message__channel__team_id",),
    ),
    Kind(
        "team_emoji",
        "origin.TeamEmojiMaster",
        "image",
        "image_size_bytes",
        ("team_id",),
        no_team_bucket=SYSTEM,
    ),
    Kind(
        "team_avatar",
        "origin.TeamMaster",
        "profile_image_file",
        "profile_image_size_bytes",
        ("team_id",),
    ),
    Kind(
        "project_avatar",
        "origin.ProjectMaster",
        "profile_image_url",
        "profile_image_size_bytes",
        ("team_id",),
    ),
    Kind(
        "channel_avatar",
        "origin.Channel",
        "profile_image_file",
        "profile_image_size_bytes",
        ("team_id",),
    ),
    Kind(
        "user_avatar",
        "origin.CustomUser",
        "profile_image_url",
        "profile_image_size_bytes",
        (),
    ),
)

# FileFields that are deliberately not counted, as (model label, attname).
EXEMPT: frozenset[tuple[str, str]] = frozenset()


def _with_file(kind: Kind):
    """The kind's rows that hold a file."""
    return kind.model._default_manager.exclude(**{kind.file_attname: ""}).exclude(
        **{f"{kind.file_attname}__isnull": True}
    )


def _team_expr(kind: Kind):
    paths = [F(p) for p in kind.team_paths]
    if not paths:
        return None
    return paths[0] if len(paths) == 1 else Coalesce(*paths)


def _empty() -> dict:
    return {"total_bytes": 0, "kinds": {}}


def _add(bucket: dict, label: str, count: int, size: int) -> None:
    entry = bucket["kinds"].setdefault(label, {"count": 0, "bytes": 0})
    entry["count"] += count
    entry["bytes"] += size
    bucket["total_bytes"] += size


def team_storage_usage(team_id) -> dict:
    """`{"total_bytes", "kinds": {label: {"count", "bytes"}}}` for one team; kinds with no file left out."""
    usage = _empty()
    for kind in KINDS:
        expr = _team_expr(kind)
        if expr is None:
            continue
        agg = (
            _with_file(kind)
            .annotate(_team=expr)
            .filter(_team=team_id)
            .aggregate(n=Count("pk"), b=Sum(kind.size_attname))
        )
        if agg["n"]:
            _add(usage, kind.label, agg["n"], agg["b"] or 0)
    return usage


def storage_usage_by_team() -> dict:
    """`{team_id (str) | "unattributed" | "system": usage}` for every bucket that holds a file."""
    out: dict[str, dict] = {}

    def bucket(key):
        return out.setdefault(key, _empty())

    for kind in KINDS:
        expr = _team_expr(kind)
        rows = _with_file(kind)
        if expr is None:
            agg = rows.aggregate(n=Count("pk"), b=Sum(kind.size_attname))
            if agg["n"]:
                _add(bucket(kind.no_team_bucket), kind.label, agg["n"], agg["b"] or 0)
            continue
        grouped = (
            rows.annotate(_team=expr)
            .values("_team")
            .annotate(n=Count("pk"), b=Sum(kind.size_attname))
            .order_by()
        )
        for row in grouped:
            key = str(row["_team"]) if row["_team"] else kind.no_team_bucket
            _add(bucket(key), kind.label, row["n"], row["b"] or 0)
    return out
