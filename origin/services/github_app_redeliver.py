"""Redeliver the App's failed webhook deliveries (MIP-2088, design MIP-2057 section 4).

GitHub does not retry a failed delivery on its own. One that lands while Genos is redeploying gets a
platform 5xx and is lost (MIP-861 for the legacy hook). The App has a deliveries API, authenticated
as the App itself (the App JWT), so the `reconcile_pr_deliveries` cron asks it for failures and
redelivers them:

    GET  /app/hook/deliveries                       (newest first, cursor paged)
    POST /app/hook/deliveries/{id}/attempts         (202: GitHub queues a redelivery)

Bounded and idempotent:

  * **Only transient failures.** A delivery is retried only when its answer was a 5xx, a timeout (no
    status), 408 or 429. A 4xx from Genos (bad signature, malformed) would fail the same way again.
  * **At most `MAX_ATTEMPTS` per delivery**, counted from the deliveries listed under the same
    `guid` (a redelivery keeps its GUID), and never one whose GUID already has a successful attempt:
    running the cron twice, or while a redelivery is in flight, queues nothing more.
  * **Recent only** (`MAX_AGE`): an old event replayed after later ones could undo newer state.
    It is also never redelivered in its first `MIN_AGE`, so a delivery still in flight is left alone.
  * **At most `MAX_PER_RUN` redeliveries and `MAX_PAGES` pages per run.**
  * Replays are harmless on our side too: the endpoint claims each delivery GUID (a duplicate is a
    no-op) and releases the claim when processing failed, so a redelivery of a failed one runs.

Nothing here logs a token, the key or GitHub's response body.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from origin.services import github_app

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_PER_RUN = 25
MAX_PAGES = 5
PER_PAGE = 100
MIN_AGE = timedelta(minutes=2)
MAX_AGE = timedelta(hours=6)
_RETRYABLE_STATUSES = frozenset({408, 429})


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {github_app.app_jwt()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": github_app.API_VERSION,
    }


def _parse_time(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _status_code(delivery: dict) -> Optional[int]:
    code = delivery.get("status_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _succeeded(delivery: dict) -> bool:
    code = _status_code(delivery)
    return code is not None and 200 <= code < 300


def _retryable(delivery: dict) -> bool:
    code = _status_code(delivery)
    return not code or code >= 500 or code in _RETRYABLE_STATUSES


def _list_deliveries(max_pages: int) -> list[dict]:
    """The App's recent deliveries, newest first, at most `max_pages` pages."""
    found: list[dict] = []
    cursor: Optional[str] = None
    for _ in range(max_pages):
        params: dict = {"per_page": PER_PAGE}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = requests.get(
                f"{github_app.API_ROOT}/app/hook/deliveries",
                params=params,
                headers=_headers(),
                timeout=github_app.TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise github_app.GithubAppError(
                f"listing deliveries failed: {type(exc).__name__}"
            ) from None
        if resp.status_code != 200:
            raise github_app.GithubAppError(f"listing deliveries returned HTTP {resp.status_code}")
        try:
            rows = resp.json()
        except ValueError:
            raise github_app.GithubAppError("listing deliveries returned no JSON") from None
        if not isinstance(rows, list):
            raise github_app.GithubAppError("listing deliveries returned an unexpected body")
        found.extend(row for row in rows if isinstance(row, dict))
        next_url = (resp.links.get("next") or {}).get("url")
        cursor = None
        if next_url:
            values = parse_qs(urlparse(next_url).query).get("cursor")
            cursor = values[0] if values else None
        if not cursor:
            break
    return found


def _redeliver(delivery_id: int) -> bool:
    try:
        resp = requests.post(
            f"{github_app.API_ROOT}/app/hook/deliveries/{int(delivery_id)}/attempts",
            headers=_headers(),
            timeout=github_app.TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        log.warning("GitHub App redelivery request failed: %s", type(exc).__name__)
        return False
    if resp.status_code != 202:
        log.warning("GitHub App redelivery returned HTTP %s", resp.status_code)
        return False
    return True


def redeliver_failed_deliveries(*, now: Optional[datetime] = None, dry_run: bool = False) -> dict:
    """Ask GitHub to redeliver this App's transiently failed deliveries. Returns counts:
    `listed`, `failed` (GUIDs with no successful attempt), `redelivered` (queued; with `dry_run`,
    the ones that would be).

    Raises `GithubAppDisabled` with the switch off, and `GithubAppError` when GitHub's listing
    cannot be read (the caller logs it and goes on: this is a recovery aid, not the only path,
    `reconcile_pr_deliveries` recovers PR state from the PR itself).
    """
    github_app._require_enabled()
    now = now or datetime.now(timezone.utc)
    deliveries = _list_deliveries(MAX_PAGES)

    by_guid: dict[str, list[dict]] = {}
    for delivery in deliveries:
        guid = delivery.get("guid")
        if isinstance(guid, str) and guid:
            by_guid.setdefault(guid, []).append(delivery)

    counts = {"listed": len(deliveries), "failed": 0, "redelivered": 0}
    for attempts in by_guid.values():
        if any(_succeeded(a) for a in attempts):
            continue
        counts["failed"] += 1
        if len(attempts) >= MAX_ATTEMPTS:
            continue
        stamped = [(_parse_time(a.get("delivered_at")), a) for a in attempts]
        stamped = [(when, a) for when, a in stamped if when is not None]
        if not stamped:
            continue
        newest_at, newest = max(stamped, key=lambda pair: pair[0])
        age = now - newest_at
        if age < MIN_AGE or age > MAX_AGE or not _retryable(newest):
            continue
        delivery_id = newest.get("id")
        if isinstance(delivery_id, bool) or not isinstance(delivery_id, int):
            continue
        if counts["redelivered"] >= MAX_PER_RUN:
            break
        if dry_run or _redeliver(delivery_id):
            counts["redelivered"] += 1
    return counts
