"""`python manage.py ai_credit_grant` — manual credit operations.

The V2 §6.5 gate-8 tool: a genuine early user who unexpectedly runs out
gets credits by command, a mis-posted entry gets REVERSED (never
edited), and every intervention leaves reason + actor on the row — the
ledger is its own audit trail.

    # grant 20 credits
    ai_credit_grant --user 42 --credits 20 --reason "early adopter ran out mid-eval" --actor kentaro

    # promotional grant
    ai_credit_grant --user 42 --credits 10 --kind promotional --reason "launch promo" --actor kentaro

    # take credits away (negative), e.g. correcting a fat-fingered grant
    ai_credit_grant --user 42 --credits -10 --reason "typo in promo grant" --actor kentaro

    # reverse a specific posted entry by id (restores its negation)
    ai_credit_grant --reverse 118 --reason "overcharge from policy bug #x" --actor kentaro

    # inspect a user's period
    ai_credit_grant --user 42 --list
    ai_credit_grant --user 42 --list --period 2026-06

Plain BaseCommand, not CronCommand: this is an operator tool — its
failures should print, not page.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from origin.search_engine import credit_ledger
from origin.search_engine.models import AiCreditEntry


class Command(BaseCommand):
    help = "Manually grant, adjust, reverse or list AI credit ledger entries."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="User id the entry belongs to.")
        parser.add_argument(
            "--credits",
            type=float,
            help="Whole credits to grant (negative to remove). Stored as milli-credits.",
        )
        parser.add_argument(
            "--kind",
            choices=[
                AiCreditEntry.KIND_MANUAL,
                AiCreditEntry.KIND_PROMOTIONAL,
                AiCreditEntry.KIND_PURCHASED,
            ],
            default=AiCreditEntry.KIND_MANUAL,
            help="Grant kind (default manual). `monthly` is system-only.",
        )
        parser.add_argument("--reason", help="Why. Required for any write; lands on the row.")
        parser.add_argument("--actor", help="Who. Required for any write; lands on the row.")
        parser.add_argument(
            "--reverse",
            type=int,
            metavar="ENTRY_ID",
            help="Reverse this posted entry (posts its negation, linked via ref_id).",
        )
        parser.add_argument("--list", action="store_true", help="List a user's entries instead.")
        parser.add_argument(
            "--period",
            help="Period for --list, as YYYY-MM (default: the current UTC month).",
        )

    def handle(self, *args, **options):
        if options.get("list"):
            self._list(options)
            return

        reason = (options.get("reason") or "").strip()
        actor = (options.get("actor") or "").strip()
        if not reason or not actor:
            raise CommandError(
                "--reason and --actor are required for any write. The ledger is "
                "the audit trail; an unattributed intervention defeats it."
            )

        if options.get("reverse") is not None:
            try:
                new_id = credit_ledger.reverse_entry(
                    options["reverse"], reason=reason, actor=actor
                )
            except AiCreditEntry.DoesNotExist:
                raise CommandError(f"entry {options['reverse']} does not exist")
            except ValueError as e:
                raise CommandError(str(e))
            self.stdout.write(
                self.style.SUCCESS(f"Reversed entry {options['reverse']} with entry {new_id}.")
            )
            return

        user_id = (options.get("user") or "").strip()
        credits_whole = options.get("credits")
        if not user_id or credits_whole is None:
            raise CommandError("--user and --credits are required (or use --reverse / --list).")
        if credits_whole == 0:
            raise CommandError("--credits must be non-zero.")

        new_id = credit_ledger.post_manual(
            user_id=user_id,
            credits_milli=int(round(credits_whole * 1000)),
            reason=reason,
            actor=actor,
            kind=options["kind"],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Posted entry {new_id}: {credits_whole:+g} credit(s) "
                f"({options['kind']}) to user {user_id}."
            )
        )

    def _list(self, options) -> None:
        user_id = (options.get("user") or "").strip()
        if not user_id:
            raise CommandError("--list requires --user.")
        period = options.get("period") or credit_ledger.period_for()
        rows = AiCreditEntry.objects.filter(user_id=user_id, period=period).order_by(
            "created_at"
        )
        if not rows.exists():
            self.stdout.write(f"No entries for user {user_id} in {period}.")
            return
        self.stdout.write(f"=== user {user_id} — {period} ===")
        total = 0
        for r in rows:
            total += r.credits_milli
            ref = f" ref={r.ref_id}" if r.ref_id else ""
            req = f" req={r.request_id}" if r.request_id else ""
            why = f"  [{r.reason}]" if r.reason else ""
            self.stdout.write(
                f"  #{r.id:<6} {r.created_at:%m-%d %H:%M} {r.entry_type:<8} "
                f"{r.kind:<12} {r.credits_milli / 1000:>+9.3f}cr  by {r.actor or '-'}"
                f"{req}{ref}{why}"
            )
        self.stdout.write(self.style.NOTICE(f"  balance: {total / 1000:+.3f} credit(s)"))
