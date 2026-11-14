"""`python manage.py cleanup_stale_chat_note_roles` — end chat-note roles that outlived a departure (MIP-1491).

Since MIP-1482 (api #753) leaving a chat, a project or a team revokes the explicit chat-note roles
the membership wrote. People who had ALREADY left still hold them, so they keep Editor/Owner on
notes of chats they are no longer in. This command finds those rows and, only when asked, deletes
them, by the SAME rule as the live revoke (`origin/services/chat_note_access.py`:
`row_is_membership_derived`; classification in `origin/services/chat_note_stale_roles.py`):

  * a holder who is a member of the note's chat now: never touched;
  * left the team (their `TeamMembers` row is soft-deleted): every role goes, shares included;
  * left the chat/project: roles written AT/AFTER they joined go (they came from membership);
    roles written BEFORE they joined stay (a deliberate share by the note's owner);
  * "ambiguous": kept like a share, but written within 60 s of the note's creation, which is what
    the creation fan-out looks like, so the recorded join date may be wrong. Counted apart, never
    revoked: read that number before trusting the heuristic;
  * notes of a deleted chat, or filed under another chat kind than their channel, are skipped.
  For a project chat the join date is the soft-deleted channel mirror's, a proxy for the
  `ProjectMembers` row that was hard-deleted (usually a bit later than the real join, so it errs
  toward keeping).

DRY RUN IS THE DEFAULT. Without `--apply` the command only SELECTs, inside a transaction the
database itself holds READ ONLY (`SET LOCAL transaction_read_only`), so a bug cannot write.
`--apply` additionally needs `--yes`. `--limit N` caps the rows that would be revoked (the scan
stops there, so the other counts cover the rows examined up to it). `--team <uuid>` scopes to one
team. `--json` prints one JSON document and nothing else. `--batch-size` (default 200) is the rows
deleted per transaction; each batch re-reads its rows and deletes only those still stale, so a
second run finds nothing.

No SecurityEvent is written: the live revoke records none (only the view's MEMBER_REMOVED for a
removal by someone else), and a new action would need a migration.

Runbook (owner), production, read-only first:

    railway ssh --service <api service>        # a shell inside the running api container
    python manage.py cleanup_stale_chat_note_roles --json > /tmp/stale.json
    python manage.py cleanup_stale_chat_note_roles --team <team uuid>     # one team, readable

  1. Read `would_revoke` against `kept_deliberate_shares` and, above all, `ambiguous_kept`
     (`ambiguous_sample` lists the first 20 rows, ids only). A large ambiguous count means the
     recorded join dates cannot be trusted: stop and re-judge the heuristic.
  2. Try a small write first: `--apply --yes --limit 5`, then re-run the dry run: the count must
     drop by exactly 5 and `current_members_untouched` must not change.
  3. Then `--apply --yes`. Rerunning is safe and reports 0.
  A person who loses a row only falls back to what the chat itself grants (a current member reads
  the note through the chat); the owner can re-share anything removed wrongly.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from origin.services import chat_note_stale_roles as stale

LINES = (
    ("would revoke (membership ended)", "would_revoke_membership"),
    ("would revoke (left the team)", "would_revoke_team_left"),
    ("kept: deliberate shares", "kept_deliberate_shares"),
    ("kept: AMBIGUOUS (looks like a membership row)", "ambiguous_kept"),
    ("untouched: current members", "current_members_untouched"),
    ("skipped (deleted chat / kind mismatch)", "skipped"),
)


@contextmanager
def read_only():
    """A transaction Postgres itself refuses to write in."""
    postgres = connection.vendor == "postgresql"
    with transaction.atomic():
        if postgres:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL transaction_read_only = on")
        yield
        if postgres:
            # The savepoint inside an outer transaction (tests) does not undo SET LOCAL.
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL transaction_read_only = off")


class Command(BaseCommand):
    help = (
        "Find (dry run, the default) or delete (--apply --yes) chat-note roles held by people "
        "who already left the chat, project or team. See the module docstring."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Delete (needs --yes).")
        parser.add_argument("--yes", action="store_true", help="Confirm --apply.")
        parser.add_argument(
            "--limit", type=int, default=None, help="Cap on ROWS REVOKED (not rows examined)."
        )
        parser.add_argument("--team", default=None, help="Only the chat notes of this team id.")
        parser.add_argument("--json", action="store_true", help="Print one JSON document.")
        parser.add_argument(
            "--batch-size", type=int, default=200, help="Rows deleted per transaction."
        )

    def handle(self, *args, **opts):
        if opts["apply"] and not opts["yes"]:
            raise CommandError("--apply deletes rows: add --yes to confirm (or drop --apply).")
        if opts["limit"] is not None and opts["limit"] < 0:
            raise CommandError("--limit must be >= 0.")
        if opts["batch_size"] < 1:
            raise CommandError("--batch-size must be >= 1.")
        with read_only():
            report = stale.scan(team_id=opts["team"], limit=opts["limit"])
        data = report.as_dict()
        data["mode"] = "apply" if opts["apply"] else "dry-run"
        if opts["apply"]:
            data["revoked"] = stale.apply(report.revoke_pks, batch_size=opts["batch_size"])
        if opts["json"]:
            self.stdout.write(json.dumps(data, indent=2, sort_keys=True))
            return
        self.stdout.write(self._text(data))

    @staticmethod
    def _text(d):
        out = [
            f"[{d['mode']}] chat-note role rows examined: {d['scanned_rows']}"
            + ("  (stopped at --limit)" if d["limited"] else ""),
            f"  WOULD REVOKE TOTAL: {d['would_revoke']}  "
            f"({d['people_affected']} people, {d['chats_affected']} chats)",
        ]
        out += [f"  {label}: {d[key]}" for label, key in LINES]
        out.append("  by chat kind (revoke / share / ambiguous / current / skipped):")
        for kind, c in d["by_chat_kind"].items():
            revoke = c[stale.REVOKE_MEMBERSHIP] + c[stale.REVOKE_TEAM_LEFT]
            out.append(
                f"    {kind:<4} {revoke} / {c[stale.KEPT_SHARE]} / {c[stale.KEPT_AMBIGUOUS]} / "
                f"{c[stale.CURRENT_MEMBER]} / {c[stale.SKIPPED]}"
            )
        if d["mode"] == "dry-run":
            out.append("  Nothing was written. Re-run with --apply --yes to delete.")
        else:
            out.append(f"  DELETED: {d['revoked']} rows.")
        return "\n".join(out)
