"""Which GitHub credential a person's request uses (MIP-2089, design MIP-2057 sections 5-7).

ONE seam for every GitHub read made on behalf of a person: the PR views, the agent tools and
the MCP `get_pr` / `list_pr_*` tools (which are the agent tools). `github_credential(user, repo)`
answers "which stored GitHub account, and so which token, does this call use?".

The order, per call:

  1. **The App user token** (`github_app` row, 8 hours, refreshed under the row lock by
     `oauth.tokens`), when `GITHUB_APP_ENABLED` is on, the person has one, and either no repo is
     named or the App covers that repo for them.
  2. **The legacy OAuth token** (`github` row, the `repo` scope): the fallback while the switch is
     off, while the person has no App connection yet, and for a repo the App does not cover (the
     30-day migration window, design section 6). Phase 4 deletes this branch.
  3. The App token anyway, when the person has no legacy token: there is nothing else to try, and
     GitHub's own 404 for a repo the App cannot reach is mapped to the usual "cannot reach this
     repo" answer by the callers.

"Covered" means: an ACTIVE installation of the App, linked to a team the person belongs to, whose
account is the repo's owner, and that either covers every repository of that account or lists this
one (`GET /user/installations/{id}/repositories`, with the person's own token, cached 5 minutes).
A failed or oversized listing reads as "not covered": falling back is the safe side.

`installation_token(owner, repo, team_ids)` is the other credential the design names: a 1 hour,
read-only token narrowed to ONE repository, for background work that writes into a linked team.
It is never used for what a person asks for. It is returned as a plain string that the caller
puts in one `Authorization` header and drops: never stored, logged, returned or sent to a model.

Nothing here logs or returns a token; `GithubCredential` exposes it only through `token()`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from django.core.cache import cache

from origin.models.common.github_app_models import GithubAppInstallation
from origin.models.common.user_models import ConnectedAccount
from origin.services import github_app
from origin.services.oauth.tokens import get_valid_access_token

log = logging.getLogger(__name__)

APP = "app"
LEGACY = "legacy"

_API_ROOT = "https://api.github.com"
_API_VERSION = "2022-11-28"
_TIMEOUT_SECONDS = 10

# A repo list is read a page at a time up to this many repositories; an installation with more
# reads as "cannot tell", i.e. not covered, so the legacy token (which sees all) still works.
_REPOS_PER_PAGE = 100
_REPOS_MAX_PAGES = 5
_COVERAGE_TTL_SECONDS = 5 * 60


@dataclass(frozen=True)
class GithubCredential:
    """One stored GitHub account chosen for a call. `kind` is `APP` or `LEGACY`.

    The account row is what the view caches are keyed on (its id and `ts_updated_at`), so an App
    answer and a legacy answer for the same repo never share an entry.
    """

    account: ConnectedAccount
    kind: str

    def token(self) -> str:
        """The usable plaintext token (refreshed under the row lock when it has expired)."""
        return get_valid_access_token(self.account)


def _repo_listing_key(app_account: ConnectedAccount, installation_id: int) -> str:
    stamp = app_account.ts_updated_at
    scope = f"{app_account.id}.{int(stamp.timestamp() * 1_000_000) if stamp else 0}"
    return f"ghapp:cover:{scope}:{int(installation_id)}"


def _installation_repo_names(
    app_account: ConnectedAccount, installation_id: int, get_token
) -> set[str] | None:
    """The lowercase names of the repositories the person can reach through this installation,
    or None when that cannot be told (any failure, or more than the pages read).
    `get_token()` is the App token or None (a dead grant: cannot tell)."""
    key = _repo_listing_key(app_account, installation_id)
    cached = cache.get(key)
    if cached is not None:
        return set(cached)
    token = get_token()
    if token is None:
        return None
    names: set[str] = set()
    for page in range(1, _REPOS_MAX_PAGES + 1):
        try:
            resp = requests.get(
                f"{_API_ROOT}/user/installations/{int(installation_id)}/repositories",
                params={"per_page": _REPOS_PER_PAGE, "page": page},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": _API_VERSION,
                },
                timeout=_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            log.warning("GitHub App repo listing failed: %s", type(exc).__name__)
            return None
        if resp.status_code != 200:
            log.warning("GitHub App repo listing returned HTTP %s", resp.status_code)
            return None
        try:
            rows = resp.json()["repositories"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(rows, list):
            return None
        names.update(str(r["name"]).lower() for r in rows if isinstance(r, dict) and r.get("name"))
        if len(rows) < _REPOS_PER_PAGE:
            cache.set(key, sorted(names), _COVERAGE_TTL_SECONDS)
            return names
    return None  # more repositories than the pages read: cannot tell


class CredentialResolver:
    """`github_credential` for one person, for a caller that asks about many repositories.

    It reads the person's two account rows, their team ids and each owner's installations once,
    so a walk over N repositories costs a handful of queries, not N times that. Build one per
    request; it keeps the App token in memory only for that request (never logged or returned).
    """

    def __init__(self, user):
        self.user = user
        self._loaded = False
        self._app: ConnectedAccount | None = None
        self._legacy: ConnectedAccount | None = None
        self._team_ids: list | None = None
        self._installations: dict[str, list[GithubAppInstallation]] = {}
        self._names: dict[int, set[str] | None] = {}
        self._token_tried = False
        self._token: str | None = None

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self.user is None:
            return
        rows = ConnectedAccount.objects.filter(
            user=self.user, provider__in=("github", "github_app")
        )
        by_provider = {row.provider: row for row in rows}
        self._legacy = by_provider.get("github")
        self._app = by_provider.get("github_app") if github_app.is_enabled() else None

    def _linked_installations(self, owner: str) -> list[GithubAppInstallation]:
        """Active installations of `owner`'s account that are linked to a team the person is in."""
        key = owner.lower()
        if key not in self._installations:
            if self._team_ids is None:
                from origin.views.utils.scope_guards import team_ids_for_user  # noqa: PLC0415

                self._team_ids = team_ids_for_user(self.user.id)
            self._installations[key] = (
                list(
                    GithubAppInstallation.objects.filter(
                        account_login__iexact=owner,
                        suspended_at__isnull=True,
                        team_links__team_id__in=self._team_ids,
                    ).distinct()
                )
                if self._team_ids
                else []
            )
        return self._installations[key]

    def _app_token(self) -> str | None:
        """The App token, or None when the grant is dead or GitHub's token endpoint fails. Asked
        once per resolver: a failing refresh is not retried (under the row lock) for every repo of
        a walk."""
        if not self._token_tried:
            self._token_tried = True
            try:
                self._token = get_valid_access_token(self._app)
            except Exception as exc:  # noqa: BLE001 - ReauthRequired, HTTP errors, a lost row
                log.warning("GitHub App token unusable: %s", type(exc).__name__)
        return self._token

    def _repo_names(self, installation: GithubAppInstallation) -> set[str] | None:
        key = installation.installation_id
        if key not in self._names:
            self._names[key] = _installation_repo_names(self._app, key, self._app_token)
        return self._names[key]

    def covers(self, owner: str, repo: str) -> bool:
        """Does the App, for this person, reach `owner/repo`? See the module doc for what counts."""
        if self._app is None:
            return False
        for installation in self._linked_installations(owner):
            if installation.repository_selection == "all":
                return True
            names = self._repo_names(installation)
            if names is not None and repo.lower() in names:
                return True
        return False

    def for_repo(self, repo: tuple[str, str] | None = None) -> GithubCredential | None:
        self._load()
        if self._app is None:
            return GithubCredential(self._legacy, LEGACY) if self._legacy is not None else None
        if self._legacy is None:
            # Nothing else to try. A dead grant shows where the token is used (the views answer
            # "could not look", the tools a reconnect message), never as a crash here.
            return GithubCredential(self._app, APP)
        if self._app_token() is None:
            return GithubCredential(
                self._legacy, LEGACY
            )  # the App grant is dead: legacy still reads
        if repo is None or self.covers(repo[0], repo[1]):
            return GithubCredential(self._app, APP)
        return GithubCredential(self._legacy, LEGACY)


def github_credential(user, repo: tuple[str, str] | None = None) -> GithubCredential | None:
    """The credential for `user`'s GitHub call, or None when they have no GitHub connection.

    `repo` is `(owner, name)` when the call is about one repository (it decides between the App
    and the legacy token); None for a call about the person, such as "my pull requests".
    """
    return CredentialResolver(user).for_repo(repo)


def installation_token(owner: str, repo: str, team_ids) -> str | None:
    """A read-only installation token for ONE repository, for background work in a linked team.

    None when the App is off, or no active installation of `owner`'s account is linked to one of
    `team_ids`, or a token cannot be minted (the installation is gone, GitHub is down). The caller
    then uses what it used before. Never for a person's own request: that is `github_credential`.
    """
    if not github_app.is_enabled() or not team_ids:
        return None
    installation = (
        GithubAppInstallation.objects.filter(
            account_login__iexact=owner,
            suspended_at__isnull=True,
            team_links__team_id__in=list(team_ids),
        )
        .distinct()
        .first()
    )
    if installation is None:
        return None
    try:
        return github_app.get_installation_token(installation.installation_id, repositories=[repo])
    except github_app.GithubAppError as exc:
        log.warning("GitHub App installation token unavailable: %s", exc)
        return None
