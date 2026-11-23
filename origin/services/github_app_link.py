"""Linking a GitHub App installation to a Genos team (MIP-2087, design MIP-2057 section 3).

The install / authorize flow runs through the existing "Connect GitHub" entry point
(`oauth_views`); this module holds the parts that are not HTTP plumbing:

  * which Genos team a connect is for, and whether the person may link one (`resolve_link_team`);
  * the two URLs the initiate step can return (`install_url`; the authorize URL is the provider's);
  * the check that decides what gets linked (`confirmed_installations`);
  * the write (`link_installations`).

Security rules (genos-security, design section 8):

  * **`installation_id` in the callback URL is a hint, never a fact.** GitHub's own docs warn that
    anyone can open the callback with a spoofed `installation_id`. What is linked is decided only
    by `GET /user/installations` answered for the person's own, freshly exchanged user token: an
    installation that does not appear there is never linked, whatever the URL says.
  * **The tenant is the signed state**, not the URL: the team id travels in the signed `state`
    (user id, team id, 10 minutes), and the person's right to link into that team (owner or
    editor) is checked again at the callback, because a role can change in between.
  * **An Organization installation links only for an active member** (MIP-2169): the person's own
    user token asks `GET /user/memberships/orgs/{org}` and `state` must be `active`. Any other
    answer or failure refuses the link (`github_app_org_membership_unverified`).
  * **The FIRST link of an Organization installation needs an organization admin** (MIP-2178): while
    no team holds it, `role` must be `admin` too (`github_app_org_admin_required` for an active
    member who is not). Later links by active members of a team that already holds it are as
    before. The membership calls of one callback run concurrently, bounded in number, in workers
    and in total time (`_check_memberships`), so a person with very many organizations cannot
    outlast the worker timeout.
  * **Nothing here holds a credential.** The user token is used for one request and dropped; the
    stored token is `ConnectedAccount`'s (encrypted) and is written by the view.
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import quote, urlencode

import requests
from django.conf import settings
from django.db import transaction

from origin.models.common.github_app_models import (
    INSTALLATION_ACCOUNT_TYPE_CHOICES,
    REPOSITORY_SELECTION_CHOICES,
    GithubAppInstallation,
    GithubAppInstallationTeam,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.services.member_roles import can_manage, resolve_team_role
from origin.views.utils.scope_guards import is_team_member

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 10
# `/user/installations` answers 100 per page at most. A person with more than this many
# installations of one App is not a case worth a loop that never ends.
_PER_PAGE = 100
_MAX_PAGES = 5

# Failure-page reason: the person's active membership of an installation's organization could not
# be verified (not a member, invitation pending, the App's "Members: read" not accepted yet, or
# GitHub unreachable). MIP-2169.
MEMBERSHIP_UNVERIFIED = "github_app_org_membership_unverified"
# Failure-page reason: the person is an active member of the organization but not an admin, and no
# team holds the organization's installation yet, so only an admin may make the first link. MIP-2178.
ADMIN_REQUIRED = "github_app_org_admin_required"

# The membership calls of one callback: at most this many organizations are asked (the rest are
# left out, as unverified), this many at a time, and the callback waits this long in total.
MAX_MEMBERSHIP_CHECKS = 20
MEMBERSHIP_WORKERS = 5
MEMBERSHIP_BUDGET_SECONDS = 15

_ACCOUNT_TYPES = {value for value, _ in INSTALLATION_ACCOUNT_TYPE_CHOICES}
_SELECTIONS = {value for value, _ in REPOSITORY_SELECTION_CHOICES}


class LinkError(Exception):
    """A connect cannot continue. `reason` is the failure-page reason (`?error=<reason>`) or, at
    the initiate step, the machine-readable `detail`; `status` is the HTTP status for the latter."""

    def __init__(self, reason: str, status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Installation:
    """One installation as GitHub's `/user/installations` answered it."""

    installation_id: int
    account_login: str
    account_type: str
    repository_selection: str
    suspended_at: Optional[datetime]
    # GitHub's id of the account that owns the installation (a user id for a User account).
    account_id: Optional[int] = None


# ---- which team ----------------------------------------------------------------------------


def _own_team_ids(user) -> set[uuid.UUID]:
    owned = TeamMaster.objects.filter(owner=user, is_deleted=False, is_demo=False).values_list(
        "team_id", flat=True
    )
    member = TeamMembers.objects.filter(
        attendee=user, is_deleted=False, team__is_deleted=False, team__is_demo=False
    ).values_list("team_id", flat=True)
    return set(owned) | set(member)


def resolve_link_team(user, team_id) -> TeamMaster:
    """The team this connect links an installation into, checked for the person.

    `team_id` comes from the request body. Omitted, it is the person's only team; a person in
    several teams must say which (`team_required`: the client sends the active team). A team the
    person is not in answers exactly like one that does not exist. Linking widens what may write
    into the team's tasks (PR events, a later PR), so it needs the owner or an editor, the same
    people who manage a team's integrations and API keys.
    """
    if team_id in (None, ""):
        ids = _own_team_ids(user)
        if not ids:
            raise LinkError("team_not_found", 404)
        if len(ids) > 1:
            raise LinkError("team_required", 400)
        team_id = next(iter(ids))
    try:
        team_id = uuid.UUID(str(team_id))
    except (ValueError, AttributeError, TypeError):
        raise LinkError("team_not_found", 404) from None
    team = TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()
    if team is None or team_id not in _own_team_ids(user):
        raise LinkError("team_not_found", 404)
    if not can_manage(resolve_team_role(team, user.id)):
        raise LinkError("team_not_allowed", 403)
    return team


def installation_manage_url(installation: GithubAppInstallation) -> str:
    """The installation's own settings page on GitHub, where repositories are added or removed.
    An organization's lives under the organization; a personal account's under the person's
    settings. Built from the stored login and id only (login quoted), never from a request."""
    iid = int(installation.installation_id)
    if installation.account_type == "Organization":
        login = quote(installation.account_login, safe="")
        return f"https://github.com/organizations/{login}/settings/installations/{iid}"
    return f"https://github.com/settings/installations/{iid}"


def _readable_team(user, team_id) -> Optional[TeamMaster]:
    """The team the Connections card describes, for a person who may only LOOK at it: the one
    named, or the person's only team, and only a team the person belongs to. None otherwise (no
    team named in a several-team account, an unknown team, a stranger's team: all alike).
    Unlike `resolve_link_team` this never needs the owner or editor role and never raises."""
    ids = _own_team_ids(user)
    if team_id in (None, ""):
        if len(ids) != 1:
            return None
        team_id = next(iter(ids))
    try:
        team_id = uuid.UUID(str(team_id))
    except (ValueError, AttributeError, TypeError):
        return None
    if team_id not in ids:
        return None
    return TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()


def connections_block(user, team_id) -> dict:
    """What `/integrations/me/` adds while `GITHUB_APP_ENABLED` is on (MIP-2090): whether this
    team has the App installed, on which accounts, and whether this person may link one.

    Public facts only: the installed accounts' logins and settings links, which every member of
    the team may see on GitHub's side anyway. No token, no secret. The caller leaves the key out
    when the switch is off, so an older client sees nothing new."""
    team = _readable_team(user, team_id)
    if team is None:
        return {"enabled": True, "team_id": None, "can_link": False, "installations": []}
    links = (
        GithubAppInstallationTeam.objects.filter(team=team)
        .select_related("installation")
        .order_by("installation__account_login", "installation__installation_id")
    )
    return {
        "enabled": True,
        "team_id": str(team.team_id),
        "can_link": can_manage(resolve_team_role(team, user.id)),
        "installations": [
            {
                "installation_id": link.installation.installation_id,
                "account_login": link.installation.account_login,
                "account_type": link.installation.account_type,
                "repository_selection": link.installation.repository_selection,
                "suspended": link.installation.suspended_at is not None,
                "manage_url": installation_manage_url(link.installation),
            }
            for link in links
        ],
    }


def team_has_active_installation(team: TeamMaster) -> bool:
    return GithubAppInstallationTeam.objects.filter(
        team=team, installation__suspended_at__isnull=True
    ).exists()


# ---- the install URL -----------------------------------------------------------------------


def install_url(*, state: str) -> str:
    """GitHub's "install the app" page for this App. The person picks the account or organization
    and its repositories there (a non-owner can send the owner an install request)."""
    slug = quote(settings.GITHUB_APP_SLUG, safe="")
    return f"https://github.com/apps/{slug}/installations/new?{urlencode({'state': state})}"


# ---- the check ------------------------------------------------------------------------------


def _parse_time(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_installation(raw) -> Optional[Installation]:
    if not isinstance(raw, dict):
        return None
    # One App only: a row naming another App id is not ours, whatever else it says.
    app_id = raw.get("app_id")
    if app_id is not None and str(app_id) != str(settings.GITHUB_APP_ID):
        return None
    installation_id = raw.get("id")
    account = raw.get("account")
    if isinstance(installation_id, bool) or not isinstance(installation_id, int):
        return None
    if not isinstance(account, dict):
        return None
    login = account.get("login")
    account_type = account.get("type")
    if not isinstance(login, str) or not login or account_type not in _ACCOUNT_TYPES:
        return None
    selection = raw.get("repository_selection")
    account_id = account.get("id")
    return Installation(
        installation_id=installation_id,
        account_id=account_id
        if isinstance(account_id, int) and not isinstance(account_id, bool)
        else None,
        account_login=login[:255],
        account_type=account_type,
        repository_selection=selection if selection in _SELECTIONS else "selected",
        suspended_at=_parse_time(raw.get("suspended_at")),
    )


def list_user_installations(user_token: str) -> list[Installation]:
    """`GET /user/installations` with the person's own user token: the installations of this App
    the person can access. Raises `LinkError("provider_error")` on any failure; GitHub's answer is
    never put in the message."""
    found: list[Installation] = []
    for page in range(1, _MAX_PAGES + 1):
        try:
            resp = requests.get(
                f"{API_ROOT}/user/installations",
                params={"per_page": _PER_PAGE, "page": page},
                headers={
                    "Authorization": f"Bearer {user_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                },
                timeout=TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            log.warning("GitHub App /user/installations failed: %s", type(exc).__name__)
            raise LinkError("provider_error") from None
        if resp.status_code != 200:
            log.warning("GitHub App /user/installations returned HTTP %s", resp.status_code)
            raise LinkError("provider_error")
        try:
            body = resp.json()
            rows = body["installations"]
        except (ValueError, KeyError, TypeError):
            raise LinkError("provider_error") from None
        if not isinstance(rows, list):
            raise LinkError("provider_error")
        for raw in rows:
            parsed = _parse_installation(raw)
            if parsed is not None:
                found.append(parsed)
        if len(rows) < _PER_PAGE:
            break
    return found


def parse_installation_hint(raw) -> Optional[int]:
    """The callback's `installation_id` as an int, or None. A hint only: see the module doc."""
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit() or len(raw) > 18:
        return None
    return int(raw)


@dataclass(frozen=True)
class OrgMembership:
    """GitHub's answer to "is this person a member of this organization".

    `verdict`: `active` (state active for the installation's own organization; `role` is GitHub's
    `admin` or `member`), `not_member` (GitHub answered: 404, or a state that is not active, or an
    answer about another organization) or `unverified` (anything else: 403 because the App's
    "Members: read" is not accepted yet, 5xx, a timeout, an unreadable body). Only `not_member` is a
    definitive no; `recheck_github_app_org_links --unlink` removes a link on nothing else.
    """

    verdict: str
    role: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.verdict == "active"


_UNVERIFIED = OrgMembership("unverified")
_NOT_MEMBER = OrgMembership("not_member")


def org_membership(user_token: str, item: Installation) -> OrgMembership:
    """The person's membership of the installation's organization (MIP-2169, role MIP-2178).

    `GET /user/memberships/orgs/{org}` with the person's own user token. It needs the App's
    organization permission "Members: read": until the organization has accepted it GitHub answers
    403, which is refused like a 404 (not a member) or a `pending` invitation. Every failure,
    including a timeout, is "not verified"; only `state == "active"` for the very organization of
    the installation is active. The log carries the organization login and the HTTP status only,
    never a token or GitHub's body.
    """
    org = item.account_login
    try:
        resp = requests.get(
            f"{API_ROOT}/user/memberships/orgs/{quote(org, safe='')}",
            headers={
                "Authorization": f"Bearer {user_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        log.warning("GitHub App org membership check for %s failed: %s", org, type(exc).__name__)
        return _UNVERIFIED
    if resp.status_code != 200:
        log.warning(
            "GitHub App org membership check for %s returned HTTP %s", org, resp.status_code
        )
        return _NOT_MEMBER if resp.status_code == 404 else _UNVERIFIED
    try:
        body = resp.json()
        state = body["state"]
        organization = body.get("organization")
        role = body.get("role")
    except (ValueError, KeyError, TypeError, AttributeError):
        log.warning("GitHub App org membership check for %s returned an unreadable body", org)
        return _UNVERIFIED
    if state != "active":
        # `%r` of a short slice: GitHub's text can never add a line to the log.
        log.warning(
            "GitHub App org membership of %s is not active (state=%r)", org, str(state)[:20]
        )
        return _NOT_MEMBER
    # The answer must be about the installation's own organization: its id, when both are known.
    org_id = organization.get("id") if isinstance(organization, dict) else None
    if item.account_id is not None and org_id is not None and str(org_id) != str(item.account_id):
        log.warning("GitHub App org membership answer for %s names another organization", org)
        return _NOT_MEMBER
    return OrgMembership("active", role if isinstance(role, str) else None)


def _check_memberships(user_token: str, items: list[Installation]) -> dict[int, OrgMembership]:
    """`org_membership` for each installation, concurrently and bounded (MIP-2178).

    At most `MEMBERSHIP_WORKERS` calls run at once, at most `MAX_MEMBERSHIP_CHECKS` organizations
    are asked, and the whole step waits at most `MEMBERSHIP_BUDGET_SECONDS`: an organization not
    answered by then (or beyond the cap) is `unverified`, so the callback always finishes inside
    the worker timeout and nothing is stored on a slow GitHub. No database access in the workers.
    """
    verdicts: dict[int, OrgMembership] = {}
    asked = items[:MAX_MEMBERSHIP_CHECKS]
    for item in items[MAX_MEMBERSHIP_CHECKS:]:
        verdicts[item.installation_id] = _UNVERIFIED
    if len(items) > len(asked):
        log.warning(
            "GitHub App org membership: %s organizations, only the first %s are checked",
            len(items),
            len(asked),
        )
    if len(asked) <= 1:
        for item in asked:
            verdicts[item.installation_id] = org_membership(user_token, item)
        return verdicts
    pool = ThreadPoolExecutor(
        max_workers=min(MEMBERSHIP_WORKERS, len(asked)), thread_name_prefix="gh-membership"
    )
    futures = {pool.submit(org_membership, user_token, item): item for item in asked}
    done, pending = wait(futures, timeout=MEMBERSHIP_BUDGET_SECONDS)
    # Do not wait for a call still in flight: it ends by its own request timeout.
    pool.shutdown(wait=False, cancel_futures=True)
    for future, item in futures.items():
        verdict = _UNVERIFIED
        if future in done:
            try:
                verdict = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad call must not fail the others
                log.warning(
                    "GitHub App org membership check for %s crashed: %s",
                    item.account_login,
                    type(exc).__name__,
                )
        verdicts[item.installation_id] = verdict
    if pending:
        log.warning(
            "GitHub App org membership: %s of %s checks did not answer in %s s",
            len(pending),
            len(asked),
            MEMBERSHIP_BUDGET_SECONDS,
        )
    return verdicts


def _may_link(
    item: Installation, *, user, team: TeamMaster, github_user_id: str, holders: set
) -> bool:
    """Whether this person may bring this (listed) installation into this team.

    `/user/installations` also lists installations the person only reaches through ONE repository
    (an outside collaborator with read access), so being listed is not ownership. What can be
    checked with the App's own permissions:

      * A **User** account's installation: its `account.id` must be the person's own GitHub id.
        A collaborator on someone else's personal repository can never link it.
      * An **Organization** installation: the installation is linked only if no other team holds
        it yet, the team already holds it, or the person already belongs to a team that holds it.
        A person who is not in the team that adopted an organization's installation cannot pull it
        into their own team. This is the local rule; the person's membership of the organization
        itself is checked separately, over the network (`org_membership`, MIP-2169; the first
        claim also needs an admin, MIP-2178).

    `holders` is the set of team ids holding this installation, read ONCE by the caller so this
    rule and the "first claim needs an admin" rule see the same snapshot (MIP-2178).
    """
    if item.account_type == "User":
        return item.account_id is not None and str(item.account_id) == str(github_user_id)
    if not holders or team.team_id in holders:
        return True
    return any(is_team_member(team_id, user.id) for team_id in holders)


def confirmed_installations(
    user_token: str,
    *,
    hinted_installation_id: Optional[int],
    hint_present: bool,
    user,
    team: TeamMaster,
    github_user_id: str,
) -> list[Installation]:
    """What may be linked: installations GitHub itself lists for this person's token AND that the
    person may bring into this team (`_may_link`).

    * With an `installation_id` in the URL (just installed): only that installation, and only if it
      is in the person's list and passes `_may_link`. Anything else (spoofed, someone else's, a
      typo, not theirs to link) fails the whole connect: nothing is linked and nothing is stored.
    * Without one (the authorize path, a second press): every listed installation that passes
      `_may_link`; the others are left out.

    An Organization installation also needs the person to be an active member of the organization
    (`org_membership`, MIP-2169), and, when no team holds the installation yet, an admin of it
    (MIP-2178: the first claim decides which team receives the organization's PR events). The
    calls run only for installations that already passed the local rules, concurrently and bounded
    (`_check_memberships`); any failure leaves that installation out (with a hint: refuses it).

    Raises `LinkError("installation_not_confirmed")` for a hint that does not answer,
    `LinkError(ADMIN_REQUIRED)` when an active member would make a first claim, `LinkError(
    MEMBERSHIP_UNVERIFIED)` when the only reason nothing links is a membership that could not be
    verified, and `LinkError("no_installation")` when nothing passes.
    """
    listed = list_user_installations(user_token)
    # One snapshot of who holds what, for both the local rule and the first-claim rule.
    holders: dict[int, set] = {}
    for installation_id, team_id in GithubAppInstallationTeam.objects.filter(
        installation__installation_id__in=[i.installation_id for i in listed]
    ).values_list("installation__installation_id", "team_id"):
        holders.setdefault(installation_id, set()).add(team_id)
    allowed = [
        i
        for i in listed
        if _may_link(
            i,
            user=user,
            team=team,
            github_user_id=github_user_id,
            holders=holders.get(i.installation_id, set()),
        )
    ]

    if hint_present:
        if hinted_installation_id is None:
            raise LinkError("installation_not_confirmed")
        allowed = [i for i in allowed if i.installation_id == hinted_installation_id]
        if not allowed:
            raise LinkError("installation_not_confirmed")

    orgs = [i for i in allowed if i.account_type == "Organization"]
    held = {installation_id for installation_id, teams in holders.items() if teams}
    verdicts = _check_memberships(user_token, orgs)

    linkable: list[Installation] = []
    refusal: Optional[str] = None
    for item in allowed:
        reason = None
        if item.account_type == "Organization":
            membership = verdicts[item.installation_id]
            if not membership.active:
                reason = MEMBERSHIP_UNVERIFIED
            elif item.installation_id not in held and membership.role != "admin":
                reason = ADMIN_REQUIRED
        if reason is None:
            linkable.append(item)
        elif refusal != ADMIN_REQUIRED:
            refusal = reason
    if hint_present and refusal:
        raise LinkError(refusal)
    if not linkable:
        raise LinkError(refusal or "no_installation")
    return linkable


# ---- the write ------------------------------------------------------------------------------


@transaction.atomic
def link_installations(
    *, team: TeamMaster, user, installations: list[Installation]
) -> list[GithubAppInstallation]:
    """Store each installation and link it to the team. Idempotent: pressing Connect again
    updates the stored facts and keeps the original link (and who made it)."""
    rows: list[GithubAppInstallation] = []
    for item in installations:
        # `suspended_at` is set only when the row is created: after that the installation's
        # webhook events own it, so a slow listing cannot undo a fresh suspension.
        facts = {
            "account_login": item.account_login,
            "account_type": item.account_type,
            "repository_selection": item.repository_selection,
        }
        row, _ = GithubAppInstallation.objects.update_or_create(
            installation_id=item.installation_id,
            defaults=facts,
            create_defaults={**facts, "suspended_at": item.suspended_at},
        )
        GithubAppInstallationTeam.objects.get_or_create(
            installation=row, team=team, defaults={"linked_by": user}
        )
        rows.append(row)
    return rows
