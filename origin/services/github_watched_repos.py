"""The repositories Genos watches for pull requests, as the GitHub App sees them (MIP-2194,
design MIP-2057 section 4).

Until now "the repos Genos watches" meant the rows of `GithubWebhookRegistration` (one per legacy
per-repo hook). The App needs no row: an installation linked to a team lists its repositories
(`GithubAppInstallationRepository`). The reconcile cron and the PR scans read the union of the two,
so a repo covered by the App is watched with no legacy hook, and the legacy rows can go once nothing
reads them.

Everything here is empty while `GITHUB_APP_ENABLED` is off, so the switch off reads exactly what
it read before. Nothing here holds or returns a credential.
"""

from __future__ import annotations

from origin.models.common.github_app_models import GithubAppInstallationRepository
from origin.models.common.user_models import GithubWebhookRegistration
from origin.services import github_app
from origin.services.github_app_webhook import app_team_ids_for_repo


def app_repos(team_ids=None) -> list[tuple[str, str]]:
    """`(owner, name)`, lower-case and sorted, of the repositories listed by an ACTIVE installation
    linked to a live team: to one of `team_ids`, or to any team when `team_ids` is None (the cron,
    which serves every team). Empty with the switch off, or for an empty `team_ids`.

    An installation on "all repositories" is read from its stored list like any other: the list is
    what the `installation` events kept in step, which is also what routes the App's deliveries.
    """
    if not github_app.is_enabled():
        return []
    links = {"installation__team_links__team__is_deleted": False}
    if team_ids is not None:
        team_ids = list(team_ids)
        if not team_ids:
            return []
        links["installation__team_links__team_id__in"] = team_ids
    names = (
        GithubAppInstallationRepository.objects.filter(
            installation__suspended_at__isnull=True, **links
        )
        .values_list("full_name", flat=True)
        .distinct()
    )
    out: set[tuple[str, str]] = set()
    for full_name in names:
        owner, _, repo = str(full_name).partition("/")
        if owner and repo and "/" not in repo:
            out.add((owner.lower(), repo.lower()))
    return sorted(out)


def registered_repos() -> list[tuple[str, str]]:
    """`(owner, repo)` of every legacy hook registration, in the spelling the row stores. The same
    query, in the same order, the scans ran before (MIP-2194): nothing here reorders their walk."""
    return list(GithubWebhookRegistration.objects.values_list("owner", "repo").distinct())


def merge_repos(*lists) -> list[tuple[str, str]]:
    """The lists joined in order, one entry per repo (case-insensitive): the first spelling wins,
    so a registered repo keeps the casing its row stores."""
    seen: set[tuple[str, str]] = set()
    merged: list[tuple[str, str]] = []
    for repos in lists:
        for owner, repo in repos:
            key = (owner.lower(), repo.lower())
            if key not in seen:
                seen.add(key)
                merged.append((owner, repo))
    return merged


def app_covers_repo(owner: str, repo: str) -> bool:
    """Does an active, team-linked installation of the App cover `owner/repo`? False with the
    switch off. The same predicate that routes the App's webhook (`app_team_ids_for_repo`)."""
    return bool(app_team_ids_for_repo(owner, repo))
