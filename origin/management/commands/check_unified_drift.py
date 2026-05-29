"""Drift detection for the Track B dual-write window.

Compares row counts AND sampled body hashes between the legacy chat
tables (`DMMessages` / `GMMessages` / `PMMessages` / `MDMMessages`) and
the unified `Message` table for the trailing N hours. Pages on any
mismatch.

Designed to be run on a 5-minute cron during the Track B bake. After
Phase 6 (legacy writes off), this command becomes ceremonial — run it
once a week as a tripwire until Phase 7 drops the legacy tables.

Usage:

    python manage.py check_unified_drift                  # default window (1h)
    python manage.py check_unified_drift --hours 24       # last 24h
    python manage.py check_unified_drift --kinds dm,gm    # subset
    python manage.py check_unified_drift --sample 200     # body-hash sample size per channel
    python manage.py check_unified_drift --json           # machine-readable output
    python manage.py check_unified_drift --fail-on-drift  # exit non-zero on any mismatch

Exit codes:
  0 — no drift detected
  1 — drift detected AND `--fail-on-drift` set (so the cron / CI step fails loudly)
  2 — internal error (DB unreachable, etc.)
"""

from __future__ import annotations

import argparse
import hashlib
import json as _json
import sys
from datetime import timedelta
from typing import Optional

from django.core.management.base import BaseCommand
from django.utils import timezone

from origin.models.chat.dm_models import DMMessages
from origin.models.chat.gm_models import GMMessages
from origin.models.chat.mdm_models import MDMMessages
from origin.models.chat.pm_models import PMMessages
from origin.models.chat.unified_models import Channel, ChannelKind, Message

LEGACY_MODELS = {
    "dm": (DMMessages, ChannelKind.DM, "dm_id"),
    "gm": (GMMessages, ChannelKind.GM, "gm_id"),
    "pm": (PMMessages, ChannelKind.PM, "project_id"),
    "mdm": (MDMMessages, ChannelKind.MDM, "mdm_id"),
}


def _hash_body(body) -> str:
    """Deterministic short hash of a message body for drift comparison.
    Uses the first 64 chars of the rendered body — sufficient to detect
    truncation or replacement bugs without inflating the comparison
    window."""
    try:
        text = (body[0].get("content", [{}])[0].get("text", "") if body else "")[:64]
    except Exception:
        text = ""
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


class Command(BaseCommand):
    help = "Detect divergence between legacy chat tables and the unified Message table."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--hours",
            type=float,
            default=1.0,
            help="Trailing window in hours (default 1.0 — matches the 5-min cron cadence).",
        )
        parser.add_argument(
            "--kinds",
            default="dm,gm,pm,mdm",
            help="Comma-separated chat kinds to check.",
        )
        parser.add_argument(
            "--sample",
            type=int,
            default=100,
            help="Per-channel body-hash sample size. 0 = count-only check.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON instead of human-readable text.",
        )
        parser.add_argument(
            "--fail-on-drift",
            action="store_true",
            help="Exit non-zero if any drift is detected (for cron alerting).",
        )

    def handle(self, *args, **opts):
        hours: float = opts["hours"]
        kinds = {k.strip().lower() for k in opts["kinds"].split(",") if k.strip()}
        sample = max(0, int(opts["sample"]))
        emit_json: bool = opts["json"]
        fail_on_drift: bool = opts["fail_on_drift"]

        since = timezone.now() - timedelta(hours=hours)
        report: dict = {
            "since": since.isoformat(),
            "kinds": sorted(kinds),
            "drift": [],
            "summary": {"channels_checked": 0, "channels_with_drift": 0},
        }

        try:
            for kind in ("dm", "gm", "pm", "mdm"):
                if kind not in kinds:
                    continue
                self._check_kind(kind, since, sample, report)
        except Exception as e:  # noqa: BLE001 — operational tool, surface the error
            self.stderr.write(self.style.ERROR(f"check_unified_drift failed: {e!r}"))
            sys.exit(2)

        if emit_json:
            self.stdout.write(_json.dumps(report, default=str, indent=2))
        else:
            self._print_human(report)

        if fail_on_drift and report["drift"]:
            sys.exit(1)

    # ---- per-kind ------------------------------------------------------

    def _check_kind(self, kind: str, since, sample: int, report: dict) -> None:
        legacy_model, channel_kind, legacy_pk_attr = LEGACY_MODELS[kind]
        # Iterate channels that have ANY legacy row updated in the window.
        legacy_qs = legacy_model.objects.filter(ts_updated_at__gte=since)
        # Group by legacy chat_id. `values_list("dm_id"|"gm_id"|...)` gives
        # us the per-channel scope.
        if kind == "pm":
            scope_attr = "project_id"
        else:
            scope_attr = f"{kind}_id"
        chat_ids = set(legacy_qs.values_list(scope_attr, flat=True).distinct())
        for chat_id in chat_ids:
            report["summary"]["channels_checked"] += 1
            drift = self._check_channel(channel_kind, kind, chat_id, since, sample)
            if drift:
                report["drift"].append(drift)
                report["summary"]["channels_with_drift"] += 1

    def _check_channel(
        self,
        channel_kind: int,
        kind_label: str,
        legacy_chat_id: int,
        since,
        sample: int,
    ) -> Optional[dict]:
        legacy_model, _, _ = LEGACY_MODELS[kind_label]
        channel = Channel.objects.filter(kind=channel_kind, legacy_chat_id=legacy_chat_id).first()
        if channel is None:
            return {
                "kind": kind_label,
                "legacy_chat_id": legacy_chat_id,
                "reason": "no_unified_channel",
                "legacy_count": legacy_model.objects.filter(
                    **{
                        f"{kind_label if kind_label != 'pm' else 'project'}_id": legacy_chat_id,
                        "ts_updated_at__gte": since,
                    }
                ).count(),
                "unified_count": 0,
            }
        scope_field = "project_id" if kind_label == "pm" else f"{kind_label}_id"
        legacy_filter = {scope_field: legacy_chat_id, "ts_updated_at__gte": since}
        legacy_count = legacy_model.objects.filter(**legacy_filter).count()
        unified_count = Message.objects.filter(
            channel=channel,
            is_thread_reply=False,
            ts_updated_at__gte=since,
        ).count()
        result: dict = {
            "kind": kind_label,
            "legacy_chat_id": legacy_chat_id,
            "channel_id": str(channel.id),
            "legacy_count": legacy_count,
            "unified_count": unified_count,
        }
        if legacy_count != unified_count:
            result["reason"] = "count_mismatch"
            return result
        if sample <= 0:
            return None  # counts match, body-hash check disabled
        # Sample N most recent message_ids; compute body-hash on both
        # sides and diff.
        legacy_recent = list(
            legacy_model.objects.filter(**legacy_filter)
            .order_by("-message_id")[:sample]
            .values_list("message_id", "message_body")
        )
        if not legacy_recent:
            return None
        sampled_seqs = [seq for seq, _ in legacy_recent]
        unified_recent = {
            seq: body
            for seq, body in Message.objects.filter(
                channel=channel,
                is_thread_reply=False,
                seq__in=sampled_seqs,
            ).values_list("seq", "body")
        }
        hash_mismatches = []
        missing_in_unified = []
        for seq, body in legacy_recent:
            if seq not in unified_recent:
                missing_in_unified.append(seq)
                continue
            if _hash_body(body) != _hash_body(unified_recent[seq]):
                hash_mismatches.append(seq)
        if hash_mismatches or missing_in_unified:
            result["reason"] = "body_hash_mismatch" if hash_mismatches else "missing_in_unified"
            result["hash_mismatches"] = hash_mismatches[:10]
            result["missing_in_unified"] = missing_in_unified[:10]
            return result
        return None

    # ---- output --------------------------------------------------------

    def _print_human(self, report: dict) -> None:
        s = report["summary"]
        self.stdout.write(self.style.HTTP_INFO(f"Window since: {report['since']}"))
        self.stdout.write(f"Checked {s['channels_checked']} channels across {report['kinds']}")
        if not report["drift"]:
            self.stdout.write(self.style.SUCCESS("No drift detected."))
            return
        self.stdout.write(self.style.WARNING(f"{s['channels_with_drift']} channel(s) with drift:"))
        for d in report["drift"]:
            self.stdout.write(
                f"  - kind={d['kind']} legacy_chat_id={d['legacy_chat_id']} "
                f"reason={d.get('reason')} "
                f"legacy_count={d.get('legacy_count')} unified_count={d.get('unified_count')}"
            )
            if d.get("hash_mismatches"):
                self.stdout.write(f"    hash_mismatches (first 10): {d['hash_mismatches']}")
            if d.get("missing_in_unified"):
                self.stdout.write(f"    missing_in_unified (first 10): {d['missing_in_unified']}")
