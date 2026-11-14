"""`python manage.py create_reviewer_account` — a sign-in for app-directory reviewers (MIP-1113).

OpenAI's reviewers (the ChatGPT app listing, MIP-1108) need an account of
their own: email + password, no code step, on a plan that includes MCP, in a
workspace that already holds real-looking work. This command builds it.

Usage:
    python manage.py create_reviewer_account --email <addr>                 # dry-run (default)
    python manage.py create_reviewer_account --email <addr> --apply --yes
    python manage.py create_reviewer_account --email <addr> --reset-password --apply --yes

**Dry-run is the default.** Without `--apply` nothing is written: not the
user, not the team, not an audit row. `--apply` also needs `--yes`.

**The data is the demo workspace.** The workspace is seeded by the same
`create_demo_environment` the landing-page demo uses (version v2, English):
two projects with sprints, a milestone, tasks in every status with comment
threads and history, task dependencies, DMs, a group chat, project channels,
notes and to-dos, four sample teammates (no sign-in: unusable passwords).
There is no second seeder.

**The marker, and why the account is not a demo account.** A demo visitor
(`CustomUser.is_demo=True`) is deleted on sign-out (`LogoutView`) and by the
daily `cleanup_demo_users` sweep. The reviewer is therefore a normal user
(`is_demo=False`) who owns the seeded team, and the seeder leaves that team
`is_demo=True`. "A non-demo user who owns a demo team" is the reviewer
marker: only the seeder creates a demo team (the team-create view never
passes `is_demo` through) and a demo team's owner is, in practice, a demo
visitor; the cleanup sweep needs BOTH flags, so it never reaches the
reviewer. Not a hard guarantee: the code does not stop a demo team's owner
being changed (`TeamMasterView` ownership transfer to a member, or a real
user being added to a demo team), so the marker is "shaped like the seeder's
output", not a cryptographic one. The failure direction that matters is
safe: a reviewer whose team was handed to a sample teammate is refused, and
no ordinary user can be made to look like a reviewer by creating a team. No
migration needed. An address that belongs to any other account (a real
user, a demo visitor) is refused, and nothing is changed.

**The plan** is written through `stripe_billing._set_team_plan` with
`source=operator`, the one sanctioned team-plan writer (the same write
`feature_access set-team-plan` makes), so Stripe can never take it away.
Only tiers that include MCP are accepted.

**The password** is generated here, stored only as the normal password hash,
and printed once, on the last line of an `--apply` run that created the
account or ran `--reset-password`, after the transaction has committed. It is
never logged and never written to the audit row. Run it from an interactive
shell (`railway ssh`), never as a one-off job or cron, whose stdout lands in the
platform's logs. `--reset-password` replaces it; every app session minted
under the old one stops working (JWTs are bound to the password,
`SIMPLE_JWT["CHECK_REVOKE_TOKEN"]`). MCP OAuth grants (the ChatGPT connection)
and API keys are NOT tied to the password and keep working: to end all access
after the review, use `operator_cutoff user <user_id>`.

**Idempotent.** A second run finds the reviewer by its address and reports
it; with `--apply` it re-asserts the plan (a no-op when it is right) and, only
when asked, resets the password. It never seeds twice. The seeded story is
dated around the day it is seeded (due dates, sprints, "recent" activity), so
create the account shortly before the review is submitted.

**Audit.** Creating the account and resetting its password each write a
`password.reset` SecurityEvent with source `operator`, the operator's OS
user and which of the two it was (no password, ids only). No audit row, no
change: the write and the event commit together.

The search index is not refreshed here (a daemon thread would die with the
command): the regular `opensearch_reindex` cron picks the rows up, or run
`opensearch_reindex --since-minutes 30` by hand.
"""

from __future__ import annotations

import getpass
import secrets
from collections import Counter

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

from origin.models.chat.unified_models import Channel, Message
from origin.models.common.security_event_models import SecurityAction, SecuritySource
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import TIER_SET_BY_OPERATOR
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import demo_v2, stripe_billing
from origin.services.demo_seeder import _content, create_demo_environment
from origin.services.pii_mask import mask_email
from origin.services.security_events import record_security_event

User = get_user_model()

# Tiers whose `TIER_QUOTAS` entry has `mcp_enabled: True` (Core and up since
# MIP-1138). Free is not offered: the account would be refused at the MCP gate.
MCP_PLANS = ("core", "pro", "max")
LANG = "en"
PASSWORD_BYTES = 24  # token_urlsafe(24) = 32 characters, ~192 bits


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def reviewer_team(user) -> TeamMaster | None:
    """The reviewer's seeded team, or None when `user` is not a reviewer.

    A reviewer is a NON-demo user who owns a demo team; see the module
    docstring for what that shape does and does not guarantee, and why it survives the demo sweep.
    A soft-deleted team still marks its owner (so the password can still be
    reset); the summary says it was deleted.
    """
    if user.is_demo:
        return None
    return (
        TeamMaster.objects.filter(owner=user, is_demo=True)
        .order_by("is_deleted", "ts_created_at")
        .first()
    )


def _rename_team(team: TeamMaster, name: str) -> None:
    """Give the seeded team the reviewer team's name, and the one seeded text that
    quotes the team's name with it: the group chat's title (`_create_group_chat`,
    `CHANNEL_TITLES["gm"]`). Done here rather than through a seeder argument
    because `demo_seeder.py` is on the billed `agent-evals` path filter.
    """
    gm_template = _content(LANG).CHANNEL_TITLES["gm"]
    old_title = gm_template.format(team_name=team.team_name)
    team.team_name = name
    team.save(update_fields=["team_name"])
    # `.save()` per row, not `.update()`: signals keep clients and the index in step.
    for channel in Channel.objects.filter(team=team, title=old_title):
        channel.title = gm_template.format(team_name=name)
        channel.save(update_fields=["title"])


def _new_password(user=None) -> str:
    for _ in range(10):
        candidate = secrets.token_urlsafe(PASSWORD_BYTES)
        try:
            validate_password(candidate, user)
        except ValidationError:
            continue
        return candidate
    raise CommandError("Could not generate a password that passes the password validators.")


class Command(BaseCommand):
    help = "Create (or report) the sign-in account app-directory reviewers use. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="The reviewer account's address.")
        parser.add_argument(
            "--team-name",
            default="Genos Review",
            help='Name of the reviewer team (default "Genos Review"). Used only on creation.',
        )
        parser.add_argument(
            "--plan",
            choices=MCP_PLANS,
            default="pro",
            help="Team plan, operator-set so Stripe cannot remove it (default pro).",
        )
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would happen and write nothing (the default).",
        )
        mode.add_argument("--apply", action="store_true", help="Write. Needs --yes.")
        parser.add_argument("--yes", action="store_true", help="Confirm --apply.")
        parser.add_argument(
            "--reset-password",
            action="store_true",
            help="Generate a new password for an existing reviewer account.",
        )

    # -- entry -------------------------------------------------------------

    def handle(self, *args, **opts):
        self.apply = bool(opts["apply"])
        if self.apply and not opts["yes"]:
            raise CommandError("--apply writes to the database; add --yes to confirm.")
        self.operator = _operator()
        email = User.objects.normalize_email((opts["email"] or "").strip())
        try:
            validate_email(email)
        except ValidationError:
            raise CommandError("--email is not a valid address.") from None
        team_name = (opts["team_name"] or "").strip()
        if not team_name:
            raise CommandError("--team-name must not be blank.")
        plan = opts["plan"]
        reset = bool(opts["reset_password"])

        existing = User.objects.with_email(email).first()
        if existing is not None:
            team = reviewer_team(existing)
            if team is None:
                raise CommandError(
                    f"{mask_email(email)} belongs to an existing account that is not a "
                    "reviewer account. Refusing: nothing was changed. Use another address."
                )
            self._existing(existing, team, plan, reset)
            return
        if reset:
            raise CommandError("--reset-password needs an existing reviewer account; none found.")
        if TeamMaster.objects.filter(team_name=team_name).exists():
            raise CommandError(
                f'A team named "{team_name}" already exists. Refusing: nothing was changed. '
                "Pick another --team-name."
            )
        self._create(email, team_name, plan)

    # -- output ------------------------------------------------------------

    def _say(self, line: str) -> None:
        self.stdout.write(("" if self.apply else "[dry-run] ") + line)

    def _summary(self, user, team) -> None:
        projects = list(
            ProjectMaster.objects.filter(team=team)
            .order_by("project_id")
            .values_list("project_id", "project_name")
        )
        tasks = TaskMaster.objects.filter(team=team, is_deleted=False)
        statuses = Counter(tasks.filter(is_milestone=False).values_list("status", flat=True))
        if team.is_deleted:
            self.stdout.write(
                "  WARNING: the reviewer team was deleted; the account has no workspace."
            )
        channels = Channel.objects.filter(team=team)
        notes = (
            PersonalNoteMaster.objects.filter(team=team).count()
            + TaskNoteMaster.objects.filter(team=team).count()
            + ChatNoteMaster.objects.filter(team=team).count()
        )
        self.stdout.write(f"  user id:     {user.id}")
        self.stdout.write(f"  team id:     {team.team_id}")
        self.stdout.write(f'  team name:   "{team.team_name}"')
        self.stdout.write(f"  plan:        {team.plan} (set by {team.plan_set_by})")
        self.stdout.write(
            f"  sign-in:     email verified={user.is_email_verified}, active={user.is_active}, "
            "MFA: none (Genos has no MFA or sign-in code step)"
        )
        for project_id, name in projects:
            self.stdout.write(f'  project:     {project_id} "{name}"')
        self.stdout.write(
            f"  counts:      {len(projects)} projects, "
            f"{sum(statuses.values())} tasks, "
            f"{MilestoneMaster.objects.filter(team=team).count()} milestones, "
            f"{TaskComments.objects.filter(task__team=team).count()} task comments, "
            f"{notes} notes, {channels.count()} chats, "
            f"{Message.objects.filter(channel__team=team).count()} messages"
        )
        self.stdout.write(
            "  task status: " + ", ".join(f"{s} {n}" for s, n in sorted(statuses.items()))
        )

    def _print_password(self, password: str) -> None:
        # The ONE place the password leaves this process. stdout only: never
        # a logger, never the audit row.
        self.stdout.write("")
        self.stdout.write(
            "Password (shown once; hand it to the owner, then into the reviewer form): " + password
        )

    def _record(self, user, event: str) -> None:
        recorded = record_security_event(
            SecurityAction.PASSWORD_RESET,
            actor_label=f"operator:{self.operator}",
            force_source=SecuritySource.OPERATOR,
            target_type="user",
            target_id=user.id,
            metadata={
                "command": "create_reviewer_account",
                "event": event,
                "operator": self.operator,
            },
        )
        if recorded is None:
            # Inside the caller's atomic: raising rolls the write back.
            raise CommandError(
                "The audit row could not be written, so nothing was changed. "
                "Check the logs for 'security.event write failed' and run it again."
            )

    # -- existing reviewer -------------------------------------------------

    def _existing(self, user, team, plan: str, reset: bool) -> None:
        self._say(f"Reviewer account {mask_email(user.email)} already exists.")
        plan_changes = team.plan != plan or team.plan_set_by != TIER_SET_BY_OPERATOR
        if plan_changes:
            self._say(f"plan: {team.plan} ({team.plan_set_by}) -> {plan} (operator)")
        if reset:
            self._say("password: generate a new one (sessions under the old one end)")
        if not user.is_active:
            # Not undone here: an operator cut-off is an incident decision.
            self.stdout.write(
                "  WARNING: this account is deactivated (operator_cutoff?); it cannot sign in."
            )
        password = None
        if self.apply and (plan_changes or reset):
            with transaction.atomic():
                stripe_billing._set_team_plan(
                    team,
                    plan,
                    reason="create_reviewer_account",
                    source=TIER_SET_BY_OPERATOR,
                )
                if reset:
                    password = _new_password(user)
                    user.set_password(password)
                    user.save(update_fields=["password"])
                    self._record(user, "reviewer_password_reset")
            team.refresh_from_db()
        elif not plan_changes and not reset:
            self._say("Nothing to change.")
        self._summary(user, team)
        if password is not None:
            self._print_password(password)

    # -- new reviewer ------------------------------------------------------

    def _create(self, email: str, team_name: str, plan: str) -> None:
        if not self.apply:
            content = demo_v2.adapt_content(_content(LANG))
            self._say(f"Would create reviewer account {mask_email(email)}:")
            self._say(
                f'  team "{team_name}", plan {plan} (operator-set), email verified, no MFA, '
                f'user "{demo_v2.VISITOR_NAMES[LANG]}"'
            )
            self._say("  4 sample teammates (cannot sign in)")
            for blueprint in content.PROJECT_BLUEPRINTS:
                self._say(
                    f'  project "{blueprint["name"]}": {len(blueprint["tasks"])} top-level tasks '
                    "with comments, statuses, a milestone and a sprint"
                )
            self._say("  plus DMs, a group chat, project channels, notes and to-dos")
            self._say("Nothing written. Re-run with --apply --yes to create it.")
            return

        password = _new_password()
        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    email=email,
                    username=demo_v2.VISITOR_NAMES[LANG],
                    password=password,
                    is_demo=False,
                    is_email_verified=True,
                )
                info = create_demo_environment(user, lang=LANG, version=demo_v2.VISITOR_VERSION)
                team = TeamMaster.objects.get(team_id=info["team_id"])
                _rename_team(team, team_name)
                stripe_billing._set_team_plan(
                    team,
                    plan,
                    reason="create_reviewer_account",
                    source=TIER_SET_BY_OPERATOR,
                )
                self._record(user, "reviewer_account_created")
        except IntegrityError as exc:
            # The database's own words (constraint and key; the email column is
            # encrypted, and the password is never in a query): a race on the
            # address or the team name, or a seeder bug, must be told apart.
            detail = str(exc).strip().splitlines()[0][:300] if str(exc).strip() else ""
            raise CommandError(
                f"Nothing was changed: the database refused a write ({detail}). If another "
                "run just took the address or the team name, pick another; otherwise this "
                "is a seeder bug (re-run with --traceback)."
            ) from exc
        team.refresh_from_db()
        self.stdout.write(f"Created reviewer account {mask_email(email)}.")
        self._summary(user, team)
        self.stdout.write(
            "  search:      indexed by the next opensearch_reindex cron tick "
            "(or run opensearch_reindex --since-minutes 30)"
        )
        self._print_password(password)
