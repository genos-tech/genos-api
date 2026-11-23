"""The GitHub App webhook: what a verified delivery may do (MIP-2088, design MIP-2057 section 4).

The endpoint (`origin.views.common.github_app_webhook_views`) verifies, de-duplicates and
dispatches; this module holds the rules that decide who a delivery may write for.

Security rules (genos-security, design section 8):

  * **The tenant is the stored link, never the payload.** A pull-request style delivery names an
    `installation.id` and a repository. Genos looks the installation up, refuses it unless it is
    known and not suspended, requires the repository to be one the installation covers, and writes
    only into the teams linked to that installation (`GithubAppInstallationTeam`, made by the
    install flow after the person proved the installation is theirs). A team that is not linked
    gets nothing, whatever the payload says.
  * **An `installation` event can create a row but never a link.** `installation.created` stores
    the installation and its repositories so the later link (and the covered-repo check) have
    something to read; with no team link it routes nothing and drops nothing.
  * **Replays are no-ops.** `claim_delivery` records `X-GitHub-Delivery` before a delivery is
    processed (a unique row); a repeat of a processed delivery is answered `duplicate`. A failed
    delivery releases its claim so GitHub's redelivery (or ours) can run it again.
  * **Nothing here reads or returns a secret.** The webhook secret is read only inside
    `verify_signature`.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from origin.models.common.github_app_models import (
    INSTALLATION_ACCOUNT_TYPE_CHOICES,
    REPOSITORY_SELECTION_CHOICES,
    GithubAppInstallation,
    GithubAppInstallationRepository,
    GithubAppInstallationTeam,
    GithubAppWebhookDelivery,
)
from origin.services.github_app import is_enabled

log = logging.getLogger(__name__)

#: The largest body the endpoint reads. GitHub's own cap is 25 MB, but a pull request, review,
#: comment, check or status event is tens of KB; the installation events with a repository list
#: are the biggest and stay well under this. Checked from Content-Length before the body is read.
MAX_BODY_BYTES = 2 * 1024 * 1024

#: The most repositories one event may add or replace, so a hostile (or enormous) payload from a
#: verified sender cannot make one request write without bound.
MAX_REPOSITORIES_PER_EVENT = 5000

#: A delivery row older than this is dropped by the redeliver cron.
DELIVERY_RETENTION = timedelta(days=30)

# `X-GitHub-Delivery` is a GUID. Accept the shape only, so a header cannot carry anything else
# into a database key or a log line.
_DELIVERY_ID_RE = re.compile(r"^[0-9A-Fa-f-]{8,64}$")
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")

_ACCOUNT_TYPES = {value for value, _ in INSTALLATION_ACCOUNT_TYPE_CHOICES}
_SELECTIONS = {value for value, _ in REPOSITORY_SELECTION_CHOICES}

#: The events the endpoint handles; anything else is acknowledged and ignored.
INSTALLATION_EVENTS = frozenset({"installation", "installation_repositories"})
PR_EVENTS = frozenset(
    {"pull_request", "issue_comment", "pull_request_review_comment", "pull_request_review"}
)
CI_EVENTS = frozenset({"check_run", "status"})


# ---- verification -----------------------------------------------------------------------------


def verify_signature(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """Constant-time HMAC-SHA256 check of `X-Hub-Signature-256` against the App's webhook secret.

    False when there is no secret configured or no (or a malformed) header: it refuses rather than
    trusting everyone. Runs before the body is parsed.
    """
    secret = settings.GITHUB_APP_WEBHOOK_SECRET
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len("sha256=") :])


def valid_delivery_id(value: Optional[str]) -> bool:
    return isinstance(value, str) and bool(_DELIVERY_ID_RE.match(value))


# ---- replay guard -----------------------------------------------------------------------------


def claim_delivery(delivery_id: str, event: str) -> bool:
    """Record a delivery as being processed. False when it was already claimed (a replay or a
    duplicate); the caller then answers 200 and does nothing.

    The caller runs this and the processing in ONE transaction (`GithubAppWebhookView.post`): the
    claim is committed only together with what the delivery wrote. A crash, a killed worker or a
    timeout rolls both back, so the redelivery of the same GUID is processed; a concurrent
    duplicate waits on the unique key, then sees the committed claim."""
    try:
        with transaction.atomic():
            GithubAppWebhookDelivery.objects.create(
                delivery_id=delivery_id, event=(event or "")[:64]
            )
    except IntegrityError:
        return False
    return True


def prune_deliveries(*, now=None) -> int:
    cutoff = (now or timezone.now()) - DELIVERY_RETENTION
    deleted, _ = GithubAppWebhookDelivery.objects.filter(ts_received_at__lt=cutoff).delete()
    return deleted


# ---- who a delivery may write for -------------------------------------------------------------


def _installation_id(payload: dict) -> Optional[int]:
    raw = (payload.get("installation") or {}).get("id")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def active_installation(payload: dict) -> Optional[GithubAppInstallation]:
    """The known, not suspended installation a payload names, else None (write nothing)."""
    installation_id = _installation_id(payload)
    if installation_id is None:
        return None
    row = GithubAppInstallation.objects.filter(installation_id=installation_id).first()
    if row is None or not row.is_active:
        return None
    return row


def _coverage_q(owner: str, repo: str, repo_id: Optional[int]) -> Optional[Q]:
    """The ONE predicate for "this installation covers `owner/repo`", used by the App endpoint and
    by the legacy drop alike (so a renamed repo is judged the same by both): listed by name, or by
    GitHub's repository id (survives a rename), or an installation on "all repositories" of the very
    account that owns it. None when the name is not a valid repo reference."""
    key = f"{owner}/{repo}".lower()
    if not _FULL_NAME_RE.match(key):
        return None
    covered = Q(repositories__full_name=key) | Q(
        repository_selection="all", account_login__iexact=owner
    )
    if isinstance(repo_id, int) and not isinstance(repo_id, bool):
        covered |= Q(repositories__github_repo_id=repo_id)
    return covered


def covers_repo(
    installation: GithubAppInstallation, full_name: str, repo_id: Optional[int] = None
) -> bool:
    """Whether the installation covers `owner/name` (see `_coverage_q`)."""
    owner, _, repo = (full_name or "").partition("/")
    covered = _coverage_q(owner, repo, repo_id)
    if covered is None:
        return False
    return (
        GithubAppInstallation.objects.filter(pk=installation.pk).filter(covered).distinct().exists()
    )


def linked_team_ids(installation: GithubAppInstallation) -> list:
    """The teams an installation's events may write into: its links to live teams. Empty = nothing."""
    return list(
        GithubAppInstallationTeam.objects.filter(
            installation=installation, team__is_deleted=False
        ).values_list("team_id", flat=True)
    )


def app_team_ids_for_repo(owner: str, repo: str, repo_id: Optional[int] = None) -> set:
    """The teams the App serves for `owner/repo`: the live teams linked to an ACTIVE installation
    that covers it. Empty with the switch off, for a suspended or unlinked installation, or a repo
    nothing covers. The legacy per-repo hook's deliveries are dropped only for these teams (and
    only when they are all the hook would have written for): an installation linked to team B must
    not silence the hook of team A, which the App does not write for."""
    if not is_enabled():
        return set()
    covered = _coverage_q(owner, repo, repo_id)
    if covered is None:
        return set()
    return set(
        GithubAppInstallationTeam.objects.filter(
            installation__in=GithubAppInstallation.objects.filter(
                covered, suspended_at__isnull=True
            ),
            team__is_deleted=False,
        ).values_list("team_id", flat=True)
    )


# ---- installation lifecycle events ------------------------------------------------------------


def _installation_facts(raw) -> Optional[dict]:
    """account_login, account_type and repository_selection from a payload's `installation`, or
    None when it is not usable or names another App."""
    if not isinstance(raw, dict):
        return None
    app_id = raw.get("app_id")
    if app_id is not None and str(app_id) != str(settings.GITHUB_APP_ID):
        return None
    account = raw.get("account")
    if not isinstance(account, dict):
        return None
    login, account_type = account.get("login"), account.get("type")
    if not isinstance(login, str) or not login or account_type not in _ACCOUNT_TYPES:
        return None
    selection = raw.get("repository_selection")
    return {
        "account_login": login[:255],
        "account_type": account_type,
        "repository_selection": selection if selection in _SELECTIONS else "selected",
    }


def _repo_rows(raw_list) -> list[tuple[str, Optional[int]]]:
    """`(lower-cased full_name, id or None)` for each usable entry of a payload's repository list."""
    rows: dict[str, Optional[int]] = {}
    if not isinstance(raw_list, list):
        return []
    for raw in raw_list[:MAX_REPOSITORIES_PER_EVENT]:
        if not isinstance(raw, dict):
            continue
        name = raw.get("full_name")
        if not isinstance(name, str) or not _FULL_NAME_RE.match(name):
            continue
        repo_id = raw.get("id")
        if isinstance(repo_id, bool) or not isinstance(repo_id, int):
            repo_id = None
        rows[name.lower()] = repo_id
    return list(rows.items())


def _add_repositories(installation: GithubAppInstallation, rows) -> None:
    """One bulk upsert (a large organization lists thousands of repositories in one event)."""
    objs = [
        GithubAppInstallationRepository(
            installation=installation, full_name=full_name, github_repo_id=repo_id
        )
        for full_name, repo_id in rows
    ]
    if objs:
        GithubAppInstallationRepository.objects.bulk_create(
            objs,
            batch_size=500,
            update_conflicts=True,
            unique_fields=["installation", "full_name"],
            update_fields=["github_repo_id"],
        )


def _remove_repositories(installation: GithubAppInstallation, rows) -> None:
    names = [name for name, _ in rows]
    ids = [repo_id for _, repo_id in rows if repo_id is not None]
    GithubAppInstallationRepository.objects.filter(installation=installation).filter(
        Q(full_name__in=names) | Q(github_repo_id__in=ids)
    ).delete()


@transaction.atomic
def apply_installation_event(event: str, payload: dict) -> str:
    """Apply an `installation` or `installation_repositories` event. Returns a short outcome
    (`applied`, `ignored`, `unknown_installation`) for the response and the log.

    `installation.created` is the only event that creates a row, and never a team link.
    """
    action = payload.get("action")
    installation_id = _installation_id(payload)
    facts = _installation_facts(payload.get("installation"))
    if installation_id is None or facts is None:
        return "ignored"
    row = GithubAppInstallation.objects.filter(installation_id=installation_id).first()

    if event == "installation":
        if action == "created":
            if row is None:
                suspended = (
                    timezone.now()
                    if (payload.get("installation") or {}).get("suspended_at")
                    else None
                )
                row = GithubAppInstallation.objects.create(
                    installation_id=installation_id, suspended_at=suspended, **facts
                )
            else:
                # `suspended_at` belongs to the suspend/unsuspend events once the row exists.
                for field, value in facts.items():
                    setattr(row, field, value)
                row.save()
            rows = _repo_rows(payload.get("repositories"))
            GithubAppInstallationRepository.objects.filter(installation=row).delete()
            _add_repositories(row, rows)
            return "applied"
        if row is None:
            return "unknown_installation"
        if action == "deleted":
            # Cascades to the team links, the repository rows; cached tokens stop at GitHub.
            row.delete()
            return "applied"
        if action == "suspend":
            row.suspended_at = row.suspended_at or timezone.now()
            row.save(update_fields=["suspended_at", "ts_updated_at"])
            return "applied"
        if action == "unsuspend":
            row.suspended_at = None
            row.save(update_fields=["suspended_at", "ts_updated_at"])
            return "applied"
        if action == "new_permissions_accepted":
            for field, value in facts.items():
                setattr(row, field, value)
            row.save()
            return "applied"
        return "ignored"

    if event == "installation_repositories":
        if action not in ("added", "removed"):
            return "ignored"
        if row is None:
            # `added` can overtake `installation.created` (events are not ordered): store the
            # installation from this event's own facts so the repositories are not lost. It carries
            # no team link, so it routes nothing until the install flow links it. `removed` for an
            # installation Genos never stored has nothing to remove.
            if action != "added":
                return "unknown_installation"
            row = GithubAppInstallation.objects.create(installation_id=installation_id, **facts)
        row.repository_selection = facts["repository_selection"]
        row.save(update_fields=["repository_selection", "ts_updated_at"])
        if action == "added":
            _add_repositories(row, _repo_rows(payload.get("repositories_added")))
        else:
            _remove_repositories(row, _repo_rows(payload.get("repositories_removed")))
        return "applied"
    return "ignored"
