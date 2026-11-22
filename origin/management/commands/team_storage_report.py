"""Bytes of uploads per team (MIP-1962), read-only, for the operator.

    python manage.py team_storage_report [--top N] [--team <uuid>]

Prints team ids, counts and byte totals only: no team, user or file names. Old rows count as 0 bytes
until `backfill_storage_sizes` has run.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from origin.services.storage_usage import storage_usage_by_team


class Command(BaseCommand):
    help = "Per-team storage bytes (counts and totals only)."

    def add_arguments(self, parser):
        parser.add_argument("--top", type=int, default=0, help="Only the N largest.")
        parser.add_argument("--team", default="", help="Only this team id.")

    def handle(self, *args, top, team, **options):
        usage = storage_usage_by_team()
        rows = sorted(usage.items(), key=lambda kv: kv[1]["total_bytes"], reverse=True)
        if team:
            rows = [r for r in rows if r[0] == team]
        if top:
            rows = rows[:top]
        self.stdout.write("bucket  files  bytes")
        for key, value in rows:
            files = sum(k["count"] for k in value["kinds"].values())
            self.stdout.write(f"{key}  {files}  {value['total_bytes']}")
        self.stdout.write(f"TOTAL  {sum(v['total_bytes'] for v in usage.values())}")
