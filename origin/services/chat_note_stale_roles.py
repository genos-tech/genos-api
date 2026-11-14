"""Finding (and ending) chat-note roles that outlived a membership which ended before MIP-1482.

MIP-1482 made leaving a chat, a project or a team revoke the explicit `NotePermissionMaster` rows
the membership wrote (`chat_note_access`). People who had ALREADY left still hold those rows. This
module walks the table once, read-only, and says for every chat-note role row what the live revoke
would have done about it, using the very same rule (`chat_note_access.row_is_membership_derived`):

  * `CURRENT_MEMBER`      the holder is a member of the note's chat now (`chat_member_q`'s rule:
                          a project chat is the project's people, any other chat its live member
                          rows). Never touched, whatever the team or the dates say. This is also
                          what keeps an external guest of a shared chat, who has no `TeamMembers`
                          row in the host team, safe.
  * `REVOKE_TEAM_LEFT`    not a member, and the holder's `TeamMembers` row in the note's team is
                          soft-deleted: they left the team, so every role goes, shares included
                          (`revoke_team_roles`). Someone who NEVER had a team row is a guest and
                          takes the membership rule below.
  * `REVOKE_MEMBERSHIP`   not a member, written at/after they joined: it came from membership.
  * `KEPT_SHARE`          not a member, written before they joined (or they never were one): a
                          deliberate share, kept.
  * `KEPT_AMBIGUOUS`      kept exactly like a share, but it carries the creation fan-out's
                          signature (written within `FANOUT_WINDOW` of the note's creation), so
                          it is more likely a membership row whose recorded join date is wrong
                          or late. Counted apart so the owner can judge the heuristic. Never
                          revoked here.
  * `SKIPPED`             no live chat to judge by (the chat is deleted) or the note is filed
                          under another chat kind than its channel's: left for a separate
                          decision.

For a project chat the join date is the soft-deleted mirror `ChannelMember.ts_joined_at`: the
`ProjectMembers` row the live revoke reads was hard-deleted when the person left, so it is a proxy
(usually a little later than the real join, which makes the rule err toward KEEPING).

`scan` only reads. `apply` deletes the rows `scan` found, by primary key, in batches that each
re-check the verdict inside their own transaction, so a person re-added in between is not hit and a
second run finds nothing.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.services.chat_note_access import (
    NOTE_TYPE_CHAT,
    _mark_search,
    row_is_membership_derived,
)

log = logging.getLogger(__name__)

# `chat_note_roles` writes the creation fan-out in the request that creates the note.
FANOUT_WINDOW = timedelta(seconds=60)
CHUNK = 1000
SAMPLE_MAX = 20

CURRENT_MEMBER = "current_member"
REVOKE_TEAM_LEFT = "revoke_team_left"
REVOKE_MEMBERSHIP = "revoke_membership"
KEPT_SHARE = "kept_share"
KEPT_AMBIGUOUS = "kept_ambiguous"
SKIPPED = "skipped"
REVOKE = (REVOKE_TEAM_LEFT, REVOKE_MEMBERSHIP)
VERDICTS = (CURRENT_MEMBER, *REVOKE, KEPT_SHARE, KEPT_AMBIGUOUS, SKIPPED)

KIND_NAMES = {int(k): k.name for k in ChannelKind}  # 1 DM, 2 GM, 3 PM, 4 MDM


@dataclass
class Report:
    scanned: int = 0
    limited: bool = False
    # {kind name: {verdict: rows}}
    by_kind: dict = field(default_factory=lambda: defaultdict(Counter))
    people_to_revoke: set = field(default_factory=set)
    chats_to_revoke: set = field(default_factory=set)
    ambiguous_sample: list = field(default_factory=list)
    revoke_pks: list = field(default_factory=list)

    def total(self, verdict) -> int:
        return sum(c[verdict] for c in self.by_kind.values())

    @property
    def revoke_total(self) -> int:
        return sum(self.total(v) for v in REVOKE)

    def as_dict(self) -> dict:
        kinds = sorted(self.by_kind, key=lambda n: ChannelKind[n].value if n in ChannelKind.__members__ else 99)
        return {
            "scanned_rows": self.scanned,
            "limited": self.limited,
            "would_revoke": self.revoke_total,
            "would_revoke_membership": self.total(REVOKE_MEMBERSHIP),
            "would_revoke_team_left": self.total(REVOKE_TEAM_LEFT),
            "kept_deliberate_shares": self.total(KEPT_SHARE),
            "ambiguous_kept": self.total(KEPT_AMBIGUOUS),
            "current_members_untouched": self.total(CURRENT_MEMBER),
            "skipped": self.total(SKIPPED),
            "people_affected": len(self.people_to_revoke),
            "chats_affected": len(self.chats_to_revoke),
            "by_chat_kind": {n: {v: self.by_kind[n][v] for v in VERDICTS} for n in kinds},
            "ambiguous_sample": self.ambiguous_sample,
        }


def _scope(team_id=None):
    notes = ChatNoteMaster.objects.filter(channel_id__isnull=False)
    if team_id:
        notes = notes.filter(team_id=team_id)
    return NotePermissionMaster.objects.filter(
        note_type=NOTE_TYPE_CHAT,
        user_id__isnull=False,
        note_id__in=notes.values("note_id"),
    )


def _classify(rows):
    """`rows`: `[(pk, user_id, note_id, ts_created_at)]` -> `[(row, verdict, kind_name,
    channel_id, joined_at)]`, from a handful of set-based reads (no per-row query)."""
    notes = {
        n["note_id"]: n
        for n in ChatNoteMaster.objects.filter(note_id__in={r[2] for r in rows}).values(
            "note_id", "channel_id", "team_id", "chat_type", "ts_created_at"
        )
    }
    channels = {
        str(c["id"]): c
        for c in Channel.objects.filter(
            id__in={n["channel_id"] for n in notes.values() if n["channel_id"]}
        ).values("id", "kind", "project_id", "is_deleted")
    }
    users = {r[1] for r in rows}
    members = {
        (str(cid), str(uid)): (deleted, joined)
        for cid, uid, deleted, joined in ChannelMember.objects.filter(
            channel_id__in=list(channels), user_id__in=users
        ).values_list("channel_id", "user_id", "is_deleted", "ts_joined_at")
    }
    on_project = {
        (str(pid), str(uid))
        for pid, uid in ProjectMembers.objects.filter(
            project_id__in={c["project_id"] for c in channels.values() if c["project_id"]},
            attendee_id__in=users,
        ).values_list("project_id", "attendee_id")
    }
    team_rows = {
        (str(tid), str(uid)): deleted
        for tid, uid, deleted in TeamMembers.objects.filter(
            team_id__in={n["team_id"] for n in notes.values() if n["team_id"]},
            attendee_id__in=users,
        ).values_list("team_id", "attendee_id", "is_deleted")
    }

    out = []
    for row in rows:
        pk, user_id, note_id, created = row
        note = notes.get(note_id)
        channel = channels.get(str(note["channel_id"])) if note else None
        if channel is None:
            out.append((row, SKIPPED, "UNKNOWN", None, None))
            continue
        kind = KIND_NAMES.get(channel["kind"], str(channel["kind"]))
        cid, uid = str(channel["id"]), str(user_id)
        if channel["is_deleted"] or note["chat_type"] != channel["kind"]:
            out.append((row, SKIPPED, kind, cid, None))
            continue
        member = members.get((cid, uid))
        if channel["kind"] == ChannelKind.PM:
            current = bool(channel["project_id"]) and (str(channel["project_id"]), uid) in on_project
        else:
            current = member is not None and not member[0]
        joined = member[1] if member else None
        if current:
            verdict = CURRENT_MEMBER
        elif note["team_id"] and team_rows.get((str(note["team_id"]), uid)) is True:
            verdict = REVOKE_TEAM_LEFT
        elif row_is_membership_derived(created, joined):
            verdict = REVOKE_MEMBERSHIP
        elif abs(created - note["ts_created_at"]) <= FANOUT_WINDOW:
            verdict = KEPT_AMBIGUOUS
        else:
            verdict = KEPT_SHARE
        out.append((row, verdict, kind, cid, joined))
    return out


def scan(*, team_id=None, limit=None) -> Report:
    """Classify every chat-note role row (of `team_id`, or all). Reads only.

    `limit` caps the rows that would be revoked; the scan stops once it is reached, so the other
    counts then cover the rows examined up to there."""
    report = Report()
    last = 0
    while True:
        rows = list(
            _scope(team_id)
            .filter(pk__gt=last)
            .order_by("pk")
            .values_list("pk", "user_id", "note_id", "ts_created_at")[:CHUNK]
        )
        if not rows:
            break
        last = rows[-1][0]
        for row, verdict, kind, cid, joined in _classify(rows):
            if verdict in REVOKE and limit is not None and len(report.revoke_pks) >= limit:
                report.limited = True
                return report
            report.scanned += 1
            report.by_kind[kind][verdict] += 1
            if verdict in REVOKE:
                report.revoke_pks.append(row[0])
                report.people_to_revoke.add(str(row[1]))
                report.chats_to_revoke.add(cid)
            elif verdict == KEPT_AMBIGUOUS and len(report.ambiguous_sample) < SAMPLE_MAX:
                report.ambiguous_sample.append(
                    {
                        "role_row_id": row[0],
                        "note_id": row[2],
                        "chat_id": cid,
                        "chat_kind": kind,
                        "row_created": row[3].isoformat(),
                        "recorded_join": joined.isoformat() if joined else None,
                    }
                )
    return report


def apply(pks, *, batch_size=200) -> int:
    """Delete the rows `scan` chose, `batch_size` per transaction. Returns rows deleted.

    Each batch reads its rows again and deletes only those whose verdict is still a revoke, so a
    person re-added since the scan, or a row already gone, is left alone (idempotent)."""
    deleted_total = 0
    pks = list(pks)
    for start in range(0, len(pks), batch_size):
        batch = pks[start : start + batch_size]
        with transaction.atomic():
            rows = list(
                NotePermissionMaster.objects.filter(pk__in=batch).values_list(
                    "pk", "user_id", "note_id", "ts_created_at"
                )
            )
            still = [r for r, verdict, _k, _c, _j in _classify(rows) if verdict in REVOKE] if rows else []
            if not still:
                continue
            channels = {
                str(cid)
                for cid in ChatNoteMaster.objects.filter(
                    note_id__in={r[2] for r in still}
                ).values_list("channel_id", flat=True)
                if cid
            }
            deleted, _ = NotePermissionMaster.objects.filter(pk__in=[r[0] for r in still]).delete()
            if deleted:
                _mark_search(channels)
            deleted_total += deleted
            log.info(
                "cleanup_stale_chat_note_roles: batch deleted=%d channels=%d", deleted, len(channels)
            )
    return deleted_total
