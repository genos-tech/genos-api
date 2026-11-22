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
  * **Nothing here holds a credential.** The user token is used for one request and dropped; the
    stored token is `ConnectedAccount`'s (encrypted) and is written by the view.
"""

from __future__ import annotations

import logging
import uuid
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

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 10
# `/user/installations` answers 100 per page at most. A person with more than this many
# installations of one App is not a case worth a loop that never ends.
_PER_PAGE = 100
_MAX_PAGES = 5

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
    return Installation(
        installation_id=installation_id,
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


def confirmed_installations(
    user_token: str, *, hinted_installation_id: Optional[int], hint_present: bool
) -> list[Installation]:
    """What may be linked: installations GitHub itself lists for this person's token.

    * With an `installation_id` in the URL (just installed): only that installation, and only if it
      is in the person's list. A value that is not there (spoofed, someone else's, a typo) fails
      the whole connect: nothing is linked and nothing is stored.
    * Without one (the authorize path, a second press): every listed installation, because the
      person proved access to each with their own token.

    Raises `LinkError("installation_not_confirmed")` for a hint that does not answer and
    `LinkError("no_installation")` when the list is empty.
    """
    listed = list_user_installations(user_token)
    if hint_present:
        if hinted_installation_id is None:
            raise LinkError("installation_not_confirmed")
        chosen = [i for i in listed if i.installation_id == hinted_installation_id]
        if not chosen:
            raise LinkError("installation_not_confirmed")
        return chosen
    if not listed:
        raise LinkError("no_installation")
    return listed


# ---- the write ------------------------------------------------------------------------------


@transaction.atomic
def link_installations(
    *, team: TeamMaster, user, installations: list[Installation]
) -> list[GithubAppInstallation]:
    """Store each installation and link it to the team. Idempotent: pressing Connect again
    updates the stored facts and keeps the original link (and who made it)."""
    rows: list[GithubAppInstallation] = []
    for item in installations:
        row, _ = GithubAppInstallation.objects.update_or_create(
            installation_id=item.installation_id,
            defaults={
                "account_login": item.account_login,
                "account_type": item.account_type,
                "repository_selection": item.repository_selection,
                "suspended_at": item.suspended_at,
            },
        )
        GithubAppInstallationTeam.objects.get_or_create(
            installation=row, team=team, defaults={"linked_by": user}
        )
        rows.append(row)
    return rows
