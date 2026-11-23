"""Delete the legacy per-repo GitHub hooks that the GitHub App now covers (MIP-2091, phase 2 of the
migration in the design MIP-2057, section 6; the runbook is
`operations/GITHUB_APP_MIGRATION_RUNBOOK.md` in genos-docs).

    python manage.py migrate_github_hooks_to_app                       # report only (the default)
    python manage.py migrate_github_hooks_to_app --counts-only         # the same, no repo names
    python manage.py migrate_github_hooks_to_app --check-github        # also ask GitHub (read only)
    python manage.py migrate_github_hooks_to_app --team <team uuid>    # one team's hooks
    python manage.py migrate_github_hooks_to_app --repo owner/name     # one repo
    python manage.py migrate_github_hooks_to_app --apply               # delete the covered hooks
    python manage.py migrate_github_hooks_to_app --restore [--apply]   # rollback: recreate them

The report lists, per team, the legacy hooks the App covers (`would delete`), the ones it does not
(`kept`) and why, then a total. A hook is COVERED when every team it writes for is served by an
active installation that covers its repo (the rule the legacy webhook endpoint already uses to drop
a delivery). A hook with even one uncovered team is kept, because deleting it would silence that
team. Nothing is covered while `GITHUB_APP_ENABLED` is off: `--apply` then refuses.

The default run reads the database only (no GitHub call, no token decrypted). `--check-github` asks
GitHub, with the registrar's own stored connection, whether each covered hook still exists.
`--apply` reads each covered hook, deletes it only if it is the Genos hook (its URL path is the
legacy webhook path), and treats a hook GitHub no longer has as already done, so it is safe to
repeat. The registration row stays (the reconcile cron still reads it) and stops accepting the
shared secret. A failure (the registrar lost admin or disconnected, GitHub unreachable) leaves that
hook in place, is listed, and makes the run exit non-zero; the legacy endpoint keeps dropping its
deliveries meanwhile, so nothing is processed twice.

`--restore` is the rollback. Turn `GITHUB_APP_ENABLED` off first (while it is on, the legacy endpoint
drops the deliveries of a repo the App covers). For every registration whose hook GitHub no longer
has, it recreates the hook with a fresh secret of its own (report first, `--apply` to create; a
hook that still exists is left alone). It asks GitHub (read only) even without `--apply`.

Output names team ids and `owner/repo`, never a token, a secret, a name or an email. Production:
run it only with the maintainer's go.
"""

from __future__ import annotations

import logging
import uuid

from django.core.management.base import BaseCommand, CommandError

from origin.services import crypto, github_app
from origin.services.github_app_migration import (
    ALREADY_HOOKED,
    COVERED,
    NO_TEAMS,
    NOT_COVERED,
    PARTLY_COVERED,
    RESTORED,
    STILL_PRESENT,
    HookPlan,
    migrate_hook,
    plan_hook_migration,
    probe_hook,
    restore_hook,
    summarize,
)
from origin.services.github_webhooks import hook_admin_headers

log = logging.getLogger(__name__)

_WHY_KEPT = {
    PARTLY_COVERED: "kept: the App serves only some of its teams",
    NOT_COVERED: "kept: no active installation links this team to the repo",
    NO_TEAMS: "kept: the registrar is in no team, so the hook writes nothing",
}


class Command(BaseCommand):
    help = "Report (default) or delete the legacy GitHub repo hooks the GitHub App now covers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the covered hooks. Without it nothing is changed.",
        )
        parser.add_argument(
            "--counts-only",
            action="store_true",
            help="Print counts only: no team ids, no repository names.",
        )
        parser.add_argument(
            "--check-github",
            action="store_true",
            help="Report only: also ask GitHub (read only) whether each covered hook exists.",
        )
        parser.add_argument(
            "--restore",
            action="store_true",
            help="Rollback: recreate the hooks GitHub no longer has (with --apply; else report).",
        )
        parser.add_argument("--team", help="Only the hooks that write for this team id.")
        parser.add_argument("--repo", help="Only this repository, as owner/name.")

    def handle(self, *args, **options):
        apply: bool = options["apply"]
        counts_only: bool = options["counts_only"]
        check_github: bool = options["check_github"]
        team = options["team"]
        repo = options["repo"]
        if team:
            try:
                team = str(uuid.UUID(str(team)))
            except ValueError:
                raise CommandError("--team must be a team id (a UUID).") from None
        if repo and repo.count("/") != 1:
            raise CommandError("--repo must be owner/name.")
        if apply and check_github:
            raise CommandError("--check-github is for the report; --apply reads GitHub itself.")
        if options["restore"]:
            if check_github:
                raise CommandError("--restore always asks GitHub; --check-github adds nothing.")
            if apply and github_app.is_enabled():
                raise CommandError(
                    "GITHUB_APP_ENABLED is on: the legacy endpoint would drop a restored hook's "
                    "deliveries for any repo the App covers. Turn it off first."
                )
            crypto.require_keyring()
            return self._restore(
                plan_hook_migration(team=team, repo=repo), apply=apply, counts_only=counts_only
            )

        if not github_app.is_enabled():
            if apply:
                raise CommandError(
                    "GITHUB_APP_ENABLED is off: the App serves no repository, so no legacy hook "
                    "is covered and none is deleted."
                )
            self.stdout.write(
                "NOTE: GITHUB_APP_ENABLED is off, so the App covers nothing yet; every hook "
                "below reads as not covered."
            )
        if apply or check_github:
            # Reading the registrar's stored token needs the keyring.
            crypto.require_keyring()

        plans = plan_hook_migration(team=team, repo=repo)
        probes = self._probe(plans) if check_github else {}
        self._report(plans, probes, counts_only=counts_only, apply=apply)

        if not apply:
            self.stdout.write("Dry run: nothing was changed. Run with --apply to delete.")
            return
        self._apply(plans, counts_only=counts_only)

    # ---- rollback ----------------------------------------------------------------------------

    def _restore(self, plans: list[HookPlan], *, apply: bool, counts_only: bool) -> None:
        results = []
        for plan in plans:
            outcome = restore_hook(plan.registration, apply=apply)
            results.append((plan, outcome))
            log.info(
                "[github-hooks-migrate] restore registration=%s outcome=%s",
                plan.registration.pk,
                outcome,
            )
            if not counts_only:
                self.stdout.write(f"{outcome:14} {plan.name}")
        counts = summarize(outcome for _, outcome in results)
        self.stdout.write(
            f"{len(results)} registration(s): "
            + ", ".join(f"{n} {outcome}" for outcome, n in sorted(counts.items()))
        )
        if not apply:
            self.stdout.write("Dry run: nothing was changed. Run with --restore --apply to create.")
            return
        failed = [(p, o) for p, o in results if o not in (RESTORED, STILL_PRESENT, ALREADY_HOOKED)]
        if failed:
            for plan, outcome in failed:
                self.stderr.write(
                    f"not restored: {'a hook' if counts_only else plan.name} ({outcome})"
                )
            raise CommandError(
                f"{len(failed)} hook(s) were not restored. Fix the cause and run again."
            )

    # ---- report ------------------------------------------------------------------------------

    @staticmethod
    def _probe(plans: list[HookPlan]) -> dict:
        states = {}
        for plan in plans:
            if plan.verdict != COVERED:
                continue
            headers = hook_admin_headers(None, plan.registration)
            states[plan.registration.pk] = (
                "no_token" if headers is None else probe_hook(plan.registration, headers)
            )
        return states

    def _report(self, plans, probes, *, counts_only: bool, apply: bool) -> None:
        by_team: dict[str, list[HookPlan]] = {}
        for plan in plans:
            for team_id in plan.teams or ("(no team)",):
                by_team.setdefault(team_id, []).append(plan)
        verb = "will delete" if apply else "would delete"
        for team_id in sorted(by_team):
            hooks = by_team[team_id]
            covered = sum(1 for p in hooks if p.verdict == COVERED)
            if counts_only:
                self.stdout.write(
                    f"team {team_id}: {len(hooks)} legacy hook(s), {covered} covered, "
                    f"{len(hooks) - covered} kept"
                )
                continue
            self.stdout.write(f"team {team_id}: {len(hooks)} legacy hook(s)")
            for plan in hooks:
                if plan.verdict == COVERED:
                    state = probes.get(plan.registration.pk)
                    suffix = f" [GitHub: {state}]" if state else ""
                    self.stdout.write(f"  covered      {plan.name}  {verb} hook{suffix}")
                else:
                    self.stdout.write(f"  not covered  {plan.name}  {_WHY_KEPT[plan.verdict]}")

        counts = summarize(p.verdict for p in plans)
        self.stdout.write(
            f"{len(plans)} legacy hook(s): {counts.get(COVERED, 0)} covered, "
            f"{counts.get(PARTLY_COVERED, 0)} partly covered, "
            f"{counts.get(NOT_COVERED, 0)} not covered, {counts.get(NO_TEAMS, 0)} with no team"
        )
        if probes:
            self.stdout.write(
                "GitHub says: "
                + ", ".join(
                    f"{n} {state}" for state, n in sorted(summarize(probes.values()).items())
                )
            )
        log.info(
            "[github-hooks-migrate] report hooks=%d covered=%d partly=%d not_covered=%d no_team=%d",
            len(plans),
            counts.get(COVERED, 0),
            counts.get(PARTLY_COVERED, 0),
            counts.get(NOT_COVERED, 0),
            counts.get(NO_TEAMS, 0),
        )

    # ---- apply -------------------------------------------------------------------------------

    def _apply(self, plans: list[HookPlan], *, counts_only: bool) -> None:
        results = []
        for plan in plans:
            if plan.verdict != COVERED:
                continue
            outcome = migrate_hook(plan)
            results.append((plan, outcome))
            log.info(
                "[github-hooks-migrate] registration=%s outcome=%s", plan.registration.pk, outcome
            )
            if not counts_only:
                self.stdout.write(f"{outcome:14} {plan.name}")
        counts = summarize(outcome for _, outcome in results)
        done = counts.get("deleted", 0) + counts.get("already_gone", 0)
        self.stdout.write(
            f"deleted {counts.get('deleted', 0)}, already gone {counts.get('already_gone', 0)}, "
            f"left {len(results) - done} of {len(results)} covered hook(s)"
        )
        log.info(
            "[github-hooks-migrate] apply covered=%d deleted=%d already_gone=%d left=%d",
            len(results),
            counts.get("deleted", 0),
            counts.get("already_gone", 0),
            len(results) - done,
        )
        failed = [(p, o) for p, o in results if o not in ("deleted", "already_gone")]
        if failed:
            for plan, outcome in failed:
                self.stderr.write(
                    f"still in place: {'a hook' if counts_only else plan.name} ({outcome})"
                )
            raise CommandError(
                f"{len(failed)} covered hook(s) were not deleted. They stay on GitHub; the legacy "
                "endpoint keeps dropping their deliveries. Fix the cause and run again."
            )
