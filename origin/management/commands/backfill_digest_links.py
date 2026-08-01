"""Rewrite citation tokens in ALREADY-STORED digest Inbox items.

`agent_digest` resolves the agent's citation tokens into real
`/workspace/...` links at save time — but only for digests generated
after that shipped. Rows written before it kept the raw tokens
(`[KDS-439](task:1015)`), which the inbox bubble can only render as
plain text: the frontend can't resolve them itself, because a task link
needs the project id and that is a DB lookup.

This is the one-shot fix for those rows. Idempotent by construction: a
row whose links are already `/workspace/...` paths contains no tokens
left to match, so a second run is a no-op. Run it once after deploying
the digest link fix:

    python manage.py backfill_digest_links --dry-run   # report only
    python manage.py backfill_digest_links
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

from origin.models.common.inbox_models import InboxItems
from origin.search_engine.agent.tools.entity_links import rewrite_citation_md

log = logging.getLogger(__name__)

ITEM_TYPE_DIGEST = 6


class Command(BaseCommand):
    help = "Resolve citation tokens in stored digest inbox items into /workspace links."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change; write nothing.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=1000,
            help="Max digest rows to examine.",
        )
        parser.add_argument(
            "--user-id",
            default="",
            help="Restrict to one recipient (support / targeted repair).",
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        limit = max(1, int(options["limit"]))
        only_user = (options["user_id"] or "").strip()

        rows = InboxItems.objects.filter(item_type=ITEM_TYPE_DIGEST, is_deleted=False)
        if only_user:
            rows = rows.filter(receiver_id=only_user)
        rows = rows.order_by("-ts_created_at")[:limit]

        examined = changed = skipped = 0
        for item in rows:
            examined += 1
            body = item.item_body or {}
            text = body.get("text") if isinstance(body, dict) else None
            if not text or not item.team_id:
                skipped += 1
                continue
            rewritten = rewrite_citation_md(text, team_id=str(item.team_id))
            if rewritten == text:
                skipped += 1
                continue
            changed += 1
            if dry_run:
                self.stdout.write(
                    f"[dry-run] would rewrite item {item.item_id} (user={item.receiver_id})"
                )
                continue
            body["text"] = rewritten
            item.item_body = body
            item.save(update_fields=["item_body", "ts_updated_at"])

        self.stdout.write(
            f"digest link backfill — examined={examined} changed={changed} unchanged={skipped}"
        )
