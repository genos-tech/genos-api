"""How often each rate limit has said no, per hour (MIP-724).

Every 429 a throttle serves bumps a per-scope, per-hour counter in the
cache (`origin.services.throttles.denied_count`); this prints them, so
"is a limit too tight" or "is someone hammering search" has an answer
without trawling logs:

    python manage.py throttle_denials              # last 24 hours
    python manage.py throttle_denials --hours 48   # as far back as kept

Counts live in Redis for two days and are best effort — a Redis restart
loses them. The `throttle.denied` log lines are the durable record.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from django.core.management.base import BaseCommand
from django.urls import get_resolver
from rest_framework.settings import api_settings

from origin.services import throttles


class Command(BaseCommand):
    help = "Print 429s served per throttle scope, per hour."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24)

    def handle(self, *args, hours, **options):
        # Load the URLconf so every view module — and with it every
        # throttle class and its scope — has been imported.
        get_resolver().url_patterns
        scopes = sorted(set(api_settings.DEFAULT_THROTTLE_RATES) | throttles.SCOPES)
        now = int(time.time() // 3600)
        window = range(now - hours + 1, now + 1)
        rows = []
        for scope in scopes:
            counts = [(h, throttles.denied_count(scope, h)) for h in window]
            total = sum(c for _, c in counts)
            if total:
                rows.append((scope, total, [(h, c) for h, c in counts if c]))
        if not rows:
            self.stdout.write(f"No 429s in the last {hours}h.")
            return
        for scope, total, hours_hit in sorted(rows, key=lambda r: -r[1]):
            self.stdout.write(f"{scope}: {total}")
            for h, c in hours_hit:
                stamp = datetime.fromtimestamp(h * 3600, tz=timezone.utc)
                self.stdout.write(f"  {stamp:%Y-%m-%d %H}:00Z  {c}")
