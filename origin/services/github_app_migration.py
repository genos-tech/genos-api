"""Moving a team from the legacy per-repo GitHub hooks to the GitHub App (MIP-2091, design MIP-2057
section 6 phases 2 to 4).

Pieces, each behind a management command that reports first and changes nothing unless it is
given `--apply` (the runbook is `operations/GITHUB_APP_MIGRATION_RUNBOOK.md` in genos-docs):

  * `plan_hook_migration` / `migrate_hook`: which legacy repo hooks the App now covers, and the
    deletion of exactly those (`migrate_github_hooks_to_app`).
  * `restore_hook`: the rollback of that deletion, for a hook GitHub no longer has
    (`migrate_github_hooks_to_app --restore`).
  * `plan_grant_revocation` / `revoke_legacy_grant`: which legacy OAuth grants (the never-expiring
    `repo` token) can be revoked (`revoke_legacy_github_grants`).

Rules these keep (genos-security, design section 8):

  * **"Covered" is the legacy drop's own rule.** The legacy hook endpoint stops writing for a team
    only when an ACTIVE installation that covers the repo is linked to that team
    (`github_app_webhook.app_team_ids_for_repo`), and answers 200 `covered_by_app` only when that
    leaves the hook no team to write for. A hook is deleted here only under the same condition: every
    team the hook writes for (the registrar's teams, as `_repo_team_ids` reads them) is served by the
    App. A hook with one team the App does not serve is kept (`partly_covered`): deleting it would
    silence that team. With `GITHUB_APP_ENABLED` off nothing is covered.
  * **Only the Genos hook is ever deleted.** The hook is read first; it is deleted only if its URL
    path is the legacy webhook path (`/api/v2/github/webhook/`). A hook that points elsewhere, or
    that GitHub answers a redirect for, is left alone and reported.
  * **The registration row stays.** Only the hook at GitHub goes, and the row stops accepting the
    shared secret (a hook that no longer exists signs nothing). The reconcile cron and the PR scans
    read the repos of the App's installations too and use App credentials for them (MIP-2194), so
    the row is no longer needed for a covered repo; deleting the rows is a later cleanup step.
  * **Nothing here prints or logs a token, a secret or GitHub's answer.** Logs carry counts, row ids
    and an outcome word; an exception is logged by class only.
  * **Idempotent.** A hook GitHub no longer has (404) counts as already gone; a second run finds
    nothing new to delete.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests
from django.db.models import Exists, OuterRef

from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.services import crypto
from origin.services.github_app_webhook import app_team_ids_for_repo
from origin.services.github_webhooks import (
    _HOOK_EVENTS,
    GITHUB_API_BASE,
    _new_secret,
    _webhook_payload_url,
    hook_admin_headers,
    is_valid_repo_ref,
)
from origin.services.oauth.github import GITHUB_LOGIN_SCOPES
from origin.services.oauth.revoke import Grant, revoke_grant
from origin.views.utils.scope_guards import team_ids_for_user

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10
LEGACY_HOOK_PATH = "/api/v2/github/webhook"

# ---- verdicts for a legacy hook ---------------------------------------------------------------
COVERED = "covered"  # every team the hook writes for is served by the App: deleted with --apply
PARTLY_COVERED = "partly_covered"  # some are, some are not: kept, deleting would silence the rest
NOT_COVERED = "not_covered"  # the App serves none of its teams: kept
NO_TEAMS = "no_teams"  # the registrar is gone or in no team: the hook writes nothing; kept

# ---- outcomes of acting on one hook -----------------------------------------------------------
DELETED = "deleted"
ALREADY_GONE = "already_gone"  # GitHub no longer has it (404): the goal is met
NO_TOKEN = "no_token"  # the registrar has no usable legacy connection
NO_ACCESS = "no_access"  # GitHub refused (401 / 403): the registrar lost admin on the repo
NOT_OURS = "not_ours"  # the hook points somewhere other than the legacy webhook
INVALID_REPO = "invalid_repo"
GITHUB_ERROR = "github_error"  # a redirect, a 5xx, a timeout, an unreadable answer
PRESENT = "present"  # (probe) the hook exists and is the Genos one


@dataclass(frozen=True)
class HookPlan:
    """What the App migration would do with one legacy hook."""

    registration: GithubWebhookRegistration
    verdict: str
    teams: tuple[str, ...]  # the teams the hook writes for (registrar's), as strings
    served_teams: tuple[str, ...]  # of those, the ones the App serves for this repo

    @property
    def name(self) -> str:
        return f"{self.registration.owner}/{self.registration.repo}"

    @property
    def uncovered_teams(self) -> tuple[str, ...]:
        served = set(self.served_teams)
        return tuple(t for t in self.teams if t not in served)


def classify(registration: GithubWebhookRegistration) -> HookPlan:
    """The verdict for one legacy hook, by the legacy drop's own rule (see the module docstring)."""
    teams = ()
    if registration.registered_by_id is not None:
        teams = tuple(sorted(str(t) for t in team_ids_for_user(registration.registered_by_id)))
    app_teams = {str(t) for t in app_team_ids_for_repo(registration.owner, registration.repo)}
    served = tuple(t for t in teams if t in app_teams)
    if not teams:
        verdict = NO_TEAMS
    elif len(served) == len(teams):
        verdict = COVERED
    elif served:
        verdict = PARTLY_COVERED
    else:
        verdict = NOT_COVERED
    return HookPlan(registration=registration, verdict=verdict, teams=teams, served_teams=served)


def plan_hook_migration(
    *, team: Optional[str] = None, repo: Optional[str] = None
) -> list[HookPlan]:
    """A verdict for every legacy hook (or the ones `team` / `repo` name), in a stable order.

    `team` keeps the hooks that write for that team (a hook it shares with an uncovered team is
    still `partly_covered` and kept). `repo` is `owner/name`, case-insensitive.
    """
    qs = GithubWebhookRegistration.objects.order_by("owner", "repo")
    if repo:
        owner, _, name = repo.partition("/")
        qs = qs.filter(owner__iexact=owner, repo__iexact=name)
    plans = [classify(r) for r in qs]
    if team:
        plans = [p for p in plans if str(team) in p.teams]
    return plans


def _is_legacy_hook(hook) -> bool:
    url = ((hook or {}).get("config") or {}).get("url") if isinstance(hook, dict) else None
    if not isinstance(url, str):
        return False
    # Host AND path: a Genos hook of another environment (a staging copy of this database) points
    # at its own host and is not ours to delete from here. With BACKEND_BASE_URL unset nothing
    # matches, so nothing is deleted.
    parsed, ours = urlparse(url), urlparse(_webhook_payload_url())
    return (
        parsed.path.rstrip("/") == LEGACY_HOOK_PATH
        and bool(ours.netloc)
        and parsed.netloc.lower() == ours.netloc.lower()
    )


def _registrar_is_admin(registration: GithubWebhookRegistration, headers: dict) -> bool:
    """Can the registrar's token still administer the repo? GitHub answers 404 for a hook it will
    not show a token (the registrar left the organization, the repo was transferred or made
    private, the registrar lost admin) exactly as for a hook that is gone, so a 404 counts as "gone"
    only when the repo itself is visible with `permissions.admin` true."""
    try:
        resp = requests.get(
            f"{GITHUB_API_BASE}/repos/{registration.owner}/{registration.repo}",
            headers=headers,
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
        return (
            resp.status_code == 200 and (resp.json().get("permissions") or {}).get("admin") is True
        )
    except (requests.RequestException, ValueError, AttributeError) as exc:
        log.warning("[github-hooks-migrate] admin check error=%s", type(exc).__name__)
        return False


def _hook_url(registration: GithubWebhookRegistration) -> Optional[str]:
    if not is_valid_repo_ref(registration.owner, registration.repo):
        return None
    return (
        f"{GITHUB_API_BASE}/repos/{registration.owner}/{registration.repo}"
        f"/hooks/{int(registration.hook_id)}"
    )


def probe_hook(registration: GithubWebhookRegistration, headers: dict) -> str:
    """Ask GitHub (read only) about the hook: `present`, `already_gone`, `not_ours`, `no_access`,
    `invalid_repo` or `github_error`. Never raises."""
    url = _hook_url(registration)
    if url is None:
        return INVALID_REPO
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS, allow_redirects=False)
    except requests.RequestException as exc:
        log.warning("[github-hooks-migrate] probe error=%s", type(exc).__name__)
        return GITHUB_ERROR
    if resp.status_code == 404:
        return ALREADY_GONE if _registrar_is_admin(registration, headers) else NO_ACCESS
    if resp.status_code in (401, 403):
        return NO_ACCESS
    if resp.status_code != 200:
        return GITHUB_ERROR  # includes a 301 for a renamed repo: never follow it with a token
    try:
        hook = resp.json()
    except ValueError:
        return GITHUB_ERROR
    return PRESENT if _is_legacy_hook(hook) else NOT_OURS


def migrate_hook(plan: HookPlan) -> str:
    """Delete one COVERED hook at GitHub (after checking it is the Genos hook) and stop its row
    accepting the shared secret. Returns an outcome word; never raises. A plan that is not
    `covered` is refused here as well as by the caller: this is the one place that deletes."""
    registration = plan.registration
    if plan.verdict != COVERED:
        return "not_covered"
    # Re-checked right before the write: the plan may be minutes old, and a link or an
    # installation can change meanwhile.
    if classify(registration).verdict != COVERED:
        return "not_covered"
    headers = hook_admin_headers(None, registration)
    if headers is None:
        return NO_TOKEN
    state = probe_hook(registration, headers)
    if state == PRESENT:
        outcome = _delete(registration, headers)
    else:
        outcome = state
    if outcome in (DELETED, ALREADY_GONE):
        # The hook no longer exists, so its secret signs nothing: stop accepting the shared one and
        # drop the own one. `no secret and not accepting the shared one` is also what marks a row
        # this tool migrated (`is_migrated`), so `--restore` recreates only those.
        GithubWebhookRegistration.objects.filter(pk=registration.pk).update(
            accepts_shared_secret=False, secret=None
        )
    return outcome


def is_migrated(registration: GithubWebhookRegistration) -> bool:
    """A row `migrate_hook` migrated: no own secret and not accepting the shared one. A healthy
    hook has one or the other (`rekey_hook` stores the own secret before clearing the flag)."""
    return not registration.accepts_shared_secret and not registration.secret


def _delete(registration: GithubWebhookRegistration, headers: dict) -> str:
    url = _hook_url(registration)
    if url is None:
        return INVALID_REPO
    try:
        resp = requests.delete(url, headers=headers, timeout=TIMEOUT_SECONDS, allow_redirects=False)
    except requests.RequestException as exc:
        log.warning("[github-hooks-migrate] delete error=%s", type(exc).__name__)
        return GITHUB_ERROR
    if resp.status_code == 204:
        return DELETED
    if resp.status_code == 404:
        return ALREADY_GONE
    if resp.status_code in (401, 403):
        return NO_ACCESS
    return GITHUB_ERROR


RESTORED = "restored"
STILL_PRESENT = "still_present"  # (restore) the hook exists: nothing to recreate
ALREADY_HOOKED = "already_hooked"  # (restore) GitHub refuses a second hook with our URL (422)


def restore_hook(registration: GithubWebhookRegistration, *, apply: bool) -> str:
    """The rollback of `migrate_hook`: recreate the hook for a registration whose hook GitHub no
    longer has, with a fresh secret of its own (the row keeps its place; only `hook_id`, `secret`
    and `accepts_shared_secret` change). Without `apply` it only asks GitHub (read only) and says
    what it would do: `restored` becomes `would_restore`. Returns an outcome word; never raises.

    Turn `GITHUB_APP_ENABLED` off first: while it is on, the legacy endpoint drops the deliveries of
    a repo the App covers, so a restored hook would deliver into nothing.
    """
    headers = hook_admin_headers(None, registration)
    if headers is None:
        return NO_TOKEN
    state = probe_hook(registration, headers)
    if state == PRESENT:
        return STILL_PRESENT
    if state != ALREADY_GONE:
        return state  # not_ours, no_access, invalid_repo, github_error: left as it is
    if not apply:
        return "would_restore"
    secret = _new_secret()
    body = {
        "name": "web",
        "active": True,
        "events": list(_HOOK_EVENTS),
        "config": {
            "url": _webhook_payload_url(),
            "content_type": "json",
            "secret": secret,
            "insecure_ssl": "0",
        },
    }
    try:
        resp = requests.post(
            f"{GITHUB_API_BASE}/repos/{registration.owner}/{registration.repo}/hooks",
            json=body,
            headers=headers,
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        log.warning("[github-hooks-migrate] restore error=%s", type(exc).__name__)
        return GITHUB_ERROR
    if resp.status_code == 422:
        return ALREADY_HOOKED
    if resp.status_code in (401, 403, 404):
        return NO_ACCESS
    if resp.status_code != 201:
        return GITHUB_ERROR
    try:
        hook_id = int(resp.json().get("id"))
    except (ValueError, TypeError, AttributeError):
        # GitHub made a hook whose id we cannot read: nothing here can verify or delete it.
        return GITHUB_ERROR
    GithubWebhookRegistration.objects.filter(pk=registration.pk).update(
        hook_id=hook_id, secret=secret, accepts_shared_secret=False
    )
    return RESTORED


# ---- legacy OAuth grants ----------------------------------------------------------------------

# Why a candidate grant is left alone, besides the outcomes of `revoke_grant`.
STILL_REGISTRAR = "still_registrar"
UNREADABLE = "unreadable"
STALE = "changed_meanwhile"


def registration_needs_registrar_token(registration: GithubWebhookRegistration) -> bool:
    """Does anything still use the registrar's legacy token for this registration?

    Not when the hook at GitHub is gone (`is_migrated`: the legacy hook was deleted, so there is
    nothing to re-key, adopt or delete) and the App serves EVERY team the registrar writes for
    (`classify` says `covered`: an active installation linked to each of those teams covers the
    repo), so the reconcile cron reads it with an installation token and the PR scans with the
    requester's own credential, with no registrar token (MIP-2194). Anything else (a live hook,
    a repo the App does not cover or no longer covers: suspended, unlinked, switch off, or covers
    only another team's installation) needs it.
    """
    if not is_migrated(registration):
        return True
    return classify(registration).verdict != COVERED


@dataclass
class GrantPlan:
    """The legacy `github` rows that can be revoked, and the ones held back."""

    eligible: list[ConnectedAccount] = field(default_factory=list)
    registrars: list[ConnectedAccount] = field(
        default_factory=list
    )  # held back: still a hook's admin
    unmoved: int = 0  # `repo` rows of people with no GitHub App connection: not touched by default


def plan_grant_revocation(
    *, include_unmoved: bool = False, include_registrars: bool = False
) -> GrantPlan:
    """The `github` rows whose stored token holds the `repo` scope, split into:

    * `eligible`: the person has moved (has a `github_app` connection), or `include_unmoved`;
    * `registrars`: eligible people whose token a legacy hook registration still needs
      (`registered_by`), kept unless `include_registrars` says the hooks are dealt with. A
      registration needs it (`registration_needs_registrar_token`) unless its hook is gone
      (`migrate_github_hooks_to_app` ran) AND the App covers the repo, because then the reconcile
      cron and the PR scans read it with the App's credentials (MIP-2194), and nothing else uses
      the registrar's token. So a registrar whose every repo is covered is eligible; one with a
      live hook, or a repo the App does not (or no longer) covers, is held back.
    """
    moved = Exists(ConnectedAccount.objects.filter(user=OuterRef("user"), provider="github_app"))
    rows = ConnectedAccount.objects.filter(provider="github", scopes__contains=["repo"])
    plan = GrantPlan()
    candidates = rows.order_by("ts_created_at")
    plan.unmoved = candidates.exclude(moved).count()
    if not include_unmoved:
        candidates = candidates.filter(moved)
    registrar_ids = set()
    for registration in GithubWebhookRegistration.objects.exclude(registered_by__isnull=True):
        if registration_needs_registrar_token(registration):
            registrar_ids.add(registration.registered_by_id)
    for account in candidates:
        if account.user_id in registrar_ids and not include_registrars:
            plan.registrars.append(account)
        else:
            plan.eligible.append(account)
    return plan


def revoke_legacy_grant(account: ConnectedAccount) -> str:
    """Revoke one legacy grant at GitHub and clear the stored token. Returns an outcome word
    (`revoked`, `already_invalid`, `failed`, `not_configured`, `unreadable`, `changed_meanwhile`);
    never raises.

    The row stays (it is the sign-in identity; deleting it would lock the person out and orphan
    their account), but loses the token and the `repo` scope: the stored token is dead anyway, and
    Genos keeps nothing it does not need. The row then reads like a sign-in-only connection (login
    scopes, no usable token), which GitHub already answers 401 or 404 for private repositories.
    The clear is a compare-and-swap on the ciphertext we read, so a token stored by a sign-in in
    the meantime is not erased from the database. (GitHub's grant delete kills every token of the
    grant, so a token issued during the run is dead at GitHub too: that person signs in again.)
    """
    try:
        token = crypto.decrypt(account.access_token_encrypted)
    except Exception as exc:  # noqa: BLE001 - a key problem or a corrupt value: leave the row
        log.warning("[github-grant-revoke] outcome=unreadable error=%s", type(exc).__name__)
        return UNREADABLE
    outcome = revoke_grant(Grant(provider="github", access_token=token))
    if outcome not in ("revoked", "already_invalid"):
        return outcome
    cleared = ConnectedAccount.objects.filter(
        pk=account.pk, access_token_encrypted=account.access_token_encrypted
    ).update(
        access_token_encrypted=crypto.encrypt(""),
        refresh_token_encrypted=None,
        access_token_expires_at=None,
        scopes=list(GITHUB_LOGIN_SCOPES),
    )
    if not cleared:
        return STALE
    return outcome


def summarize(outcomes: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts
