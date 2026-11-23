"""The GitHub App webhook endpoint (MIP-2088, design MIP-2057 section 4).

    POST /api/v2/github/app/webhook/

One central webhook replaces the per-repo hooks: GitHub signs every delivery with the App's webhook
secret, and which Genos team an event may write into comes from the stored installation-to-team
link (`GithubAppInstallationTeam`), never from the payload. The pull-request handlers are the
legacy hook's (`github_views.handle_verified_event`), so a PR event closes, links and comments by
one rule whichever door it came through.

Order of checks (genos-security): switch on, body size, HMAC signature, and only then the body is
parsed. Then the delivery id is claimed (a replay is a no-op), the installation must be known and
active, the repository must be one it covers, and the teams are the ones linked to it.

Answers: 404 with the switch off (the endpoint does not exist), 413 oversize, 401 bad signature,
400 malformed, 200 for everything GitHub should not retry (including every ignored delivery), 500
only when processing crashed (the claim and the delivery's writes roll back together, so the
redelivery runs in full).
"""

from __future__ import annotations

import json
import logging

import requests
from django.core.cache import cache
from django.db import transaction
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from origin.services import github_app
from origin.services import github_app_webhook as hook
from origin.services.pr_signal_reader import InstallationReader
from origin.views.common.github_views import (
    _PR_HEAD_REF_CACHE_TTL,
    _payload_repo,
    _pr_head_ref_cache_key,
    handle_verified_event,
)

logger = logging.getLogger(__name__)

_HANDLED_EVENTS = hook.INSTALLATION_EVENTS | hook.PR_EVENTS | hook.CI_EVENTS


def _installation_head_ref_fetcher(installation):
    """`issue_comment` payloads carry no head branch: look it up with an installation token narrowed
    to the one repository and to `pull_requests: read`. Cached like the legacy lookup (the branch
    name is the same for everyone with access to the repo, and is only ever matched against tasks
    inside the linked teams). The token is used for one request and dropped."""

    def fetch(owner: str, repo: str, number: int):
        key = _pr_head_ref_cache_key(owner, repo, number)
        cached = cache.get(key)
        if cached is not None:
            return cached or None
        try:
            token = github_app.get_installation_token(
                installation.installation_id,
                repositories=[repo],
                permissions={"pull_requests": "read"},
            )
            resp = requests.get(
                f"{github_app.API_ROOT}/repos/{owner}/{repo}/pulls/{int(number)}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": github_app.API_VERSION,
                },
                timeout=github_app.TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except github_app.GithubAppError as exc:
            logger.warning("GitHub App webhook: PR head-ref token failed: %s", exc)
            return None
        except requests.RequestException as exc:
            logger.warning("GitHub App webhook: PR head-ref lookup failed: %s", type(exc).__name__)
            return None
        if not resp.ok:
            cache.set(key, "", _PR_HEAD_REF_CACHE_TTL)
            return None
        try:
            head_ref = ((resp.json() or {}).get("head") or {}).get("ref") or ""
        except ValueError:
            head_ref = ""
        if not isinstance(head_ref, str):
            head_ref = ""
        cache.set(key, head_ref, _PR_HEAD_REF_CACHE_TTL)
        return head_ref or None

    return fetch


@method_decorator(csrf_exempt, name="dispatch")
class GithubAppWebhookView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []

    def post(self, request: Request):
        if not github_app.is_enabled():
            return Response({"detail": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        # Size first, from the header, before the body is read at all.
        try:
            declared = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            declared = 0
        if declared > hook.MAX_BODY_BYTES:
            return Response(
                {"detail": "too_large"}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            )
        raw_body = request.body
        if len(raw_body) > hook.MAX_BODY_BYTES:
            return Response(
                {"detail": "too_large"}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            )

        # Verified before anything is parsed or looked up.
        signature = request.headers.get("X-Hub-Signature-256")
        if not hook.verify_signature(raw_body, signature):
            logger.warning("GitHub App webhook: bad / missing signature")
            return Response({"detail": "invalid_signature"}, status=status.HTTP_401_UNAUTHORIZED)

        event = request.headers.get("X-GitHub-Event") or ""
        delivery_id = request.headers.get("X-GitHub-Delivery")
        if not hook.valid_delivery_id(delivery_id):
            return Response({"detail": "bad_delivery_id"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except (ValueError, RecursionError):
            payload = None
        if not isinstance(payload, dict):
            return Response({"detail": "bad_json"}, status=status.HTTP_400_BAD_REQUEST)

        if event == "ping":
            return Response({"detail": "pong"}, status=status.HTTP_200_OK)
        if event not in _HANDLED_EVENTS:
            return Response({"detail": "ignored"}, status=status.HTTP_200_OK)

        try:
            # The claim and everything the delivery writes commit together or not at all: a crash,
            # a killed worker or a timeout rolls the claim back with the writes, so the redelivery of
            # this GUID is processed in full and never half-repeated. A concurrent duplicate waits
            # on the claim's unique key and then sees it committed.
            with transaction.atomic():
                if not hook.claim_delivery(delivery_id, event):
                    return Response({"detail": "duplicate"}, status=status.HTTP_200_OK)
                return self._process(event, payload)
        except Exception:  # noqa: BLE001
            logger.exception("GitHub App webhook: %s delivery failed", event)
            return Response({"detail": "error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def _process(self, event: str, payload: dict) -> Response:
        if event in hook.INSTALLATION_EVENTS:
            outcome = hook.apply_installation_event(event, payload)
            return Response({"detail": outcome}, status=status.HTTP_200_OK)

        installation = hook.active_installation(payload)
        if installation is None:
            # Unknown, suspended or without an id: nothing is written, whatever else it says.
            logger.info("GitHub App webhook: %s for an unknown or suspended installation", event)
            return Response({"detail": "unknown_installation"}, status=status.HTTP_200_OK)

        repo_ref = _payload_repo(payload)
        repo_id = (payload.get("repository") or {}).get("id")
        if repo_ref is None or not hook.covers_repo(installation, "/".join(repo_ref), repo_id):
            logger.info("GitHub App webhook: %s for a repo the installation does not cover", event)
            return Response({"detail": "repo_not_covered"}, status=status.HTTP_200_OK)

        team_ids = hook.linked_team_ids(installation)
        if not team_ids:
            return Response({"detail": "no_team"}, status=status.HTTP_200_OK)

        return handle_verified_event(
            event,
            payload,
            repo_ref,
            lambda _owner, _repo: team_ids,
            head_ref_fetcher=_installation_head_ref_fetcher(installation),
            pr_reader=InstallationReader(installation.installation_id),
        )
