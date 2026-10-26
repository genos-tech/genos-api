"""Signing and sending outbound webhooks.

Customer-supplied URLs are the **first user-controlled outbound
destination in this codebase**. Every other outbound call — GitHub, the
push services, Resend, Stripe, OpenSearch — targets a host we chose. So
there was no SSRF guard to reuse and this module has to be the one.

## The SSRF problem, concretely

The API runs inside a private network with reachable neighbours: the
Postgres host, Redis, OpenSearch, the sockets and collab services, and —
on a cloud host — the instance metadata endpoint that hands out
credentials. A webhook URL is a request to make an authenticated-looking
POST from inside that network to any address the customer names. Without
a guard, "webhook" is a proxy.

`validate_webhook_url` therefore rejects anything that does not resolve
to a **public** address, and redirects are refused outright at send time
rather than followed — a 302 to `169.254.169.254` would defeat a check
that only looked at the URL the user typed.

DNS is checked when the webhook is configured, but that check cannot be what
protects the send: a name that resolves publicly now can resolve to a private
address a moment later (DNS rebinding). Re-checking just before `requests.post`
narrowed the window without closing it, because the check and the connection
each did their own lookup. The send now goes through
`outbound_url.post_public`, which resolves ONCE, requires every address to be
public, and connects to that checked address itself (certificate verified
against the typed name). There is no second lookup left to rebind (MIP-819).

## Signing

HMAC-SHA256 over `<timestamp>.<raw body>`, mirroring the inbound GitHub
verifier (`views/common/github_views._verify_signature`) with one
addition: **the timestamp is inside the signed string.** The inbound
pattern has no replay window at all — a captured GitHub payload can be
replayed at us forever — and copying that into a signature we ask other
people to trust would be exporting the flaw.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from urllib.parse import urlparse

import urllib3
from django.utils import timezone

from origin.services.outbound_url import OutboundUrlError, is_public_host, post_public

SIGNATURE_HEADER = "X-Genos-Signature"
TIMESTAMP_HEADER = "X-Genos-Timestamp"
EVENT_HEADER = "X-Genos-Event"
DELIVERY_HEADER = "X-Genos-Delivery"

# Matches the house convention for outbound HTTP everywhere else.
TIMEOUT_SECONDS = 10
# Exponential, capped. 1m, 2m, 4m, 8m, 16m — five attempts spans ~30
# minutes of transient outage, which covers a deploy but not a dead URL.
_BACKOFF_BASE_MINUTES = 1
_MAX_BACKOFF_MINUTES = 60


class WebhookUrlError(ValueError):
    """The URL is not a legal webhook destination."""


def _is_public_address(host: str) -> bool:
    """Does every address this host resolves to sit on the public net?

    The check lives in `outbound_url` now, shared with the link-title
    fetch. This module's own list passed 100.64.0.0/10 (shared / CGNAT
    space) and site-local `fec0::/10` as public (MIP-717).
    """
    return is_public_host(host)


def validate_webhook_url(url: str) -> str:
    """Return the URL, or raise `WebhookUrlError` explaining the refusal.

    The messages are deliberately specific. This one is read by the
    person configuring an integration, not by an attacker probing —
    they already know what they typed — so a vague error would just cost
    a support round trip.
    """
    # `urlparse` accepts bytes and raises on other non-strings, so a
    # JSON body carrying `"url": 123` became a 500 rather than the
    # explanatory 400 the rest of this function exists to produce.
    if url is not None and not isinstance(url, str):
        raise WebhookUrlError("Webhook URL must be a string.")
    parsed = urlparse(url or "")
    if parsed.scheme != "https":
        raise WebhookUrlError("Webhook URLs must use https.")
    if not parsed.hostname:
        raise WebhookUrlError("Webhook URL has no host.")
    if parsed.username or parsed.password:
        raise WebhookUrlError("Webhook URLs must not contain credentials.")
    if not _is_public_address(parsed.hostname):
        raise WebhookUrlError(
            "Webhook URL must resolve to a public address "
            "(private, loopback and link-local ranges are refused)."
        )
    return url


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """`sha256=<hex>` over `<timestamp>.<body>`."""
    signed = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def backoff_for(attempts: int) -> timedelta:
    minutes = min(_BACKOFF_BASE_MINUTES * (2 ** max(attempts - 1, 0)), _MAX_BACKOFF_MINUTES)
    return timedelta(minutes=minutes)


def post_delivery(url: str, secret: str, event: str, delivery_id: str, payload: dict):
    """POST one delivery. Returns `(status_code, error_message)`.

    Never raises: the caller is a cron draining a queue, and one
    unreachable customer must not end the pass.
    """
    body = json.dumps(payload, default=str, separators=(",", ":")).encode()
    timestamp = str(int(timezone.now().timestamp()))
    headers = {
        "Content-Type": "application/json",
        EVENT_HEADER: event,
        DELIVERY_HEADER: delivery_id,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign(secret, timestamp, body),
        "User-Agent": "Genos-Webhooks/1",
    }
    try:
        # The shape checks (https, no credentials, a host) run here too, so a
        # row that was valid when it was queued and is not now is reported in
        # the same words the form used.
        validate_webhook_url(url)
    except WebhookUrlError as exc:
        return None, str(exc)

    try:
        # Resolves the host ONCE, requires every address to be public, and
        # connects to that checked address. The check above and a separate
        # `requests.post` used to resolve it twice, so a name with a short TTL
        # could answer publicly to the check and privately to the connection
        # (DNS rebinding; CodeQL py/full-ssrf, MIP-819). Redirects are not
        # followed: a 3xx comes back as a status.
        status_code = post_public(url, body, headers=headers, timeout=TIMEOUT_SECONDS)
    except OutboundUrlError as exc:
        # Safe to show: it names the refusal, not the network.
        return None, str(exc)
    except (urllib3.exceptions.HTTPError, OSError) as exc:
        return None, str(exc)[:300]

    if 200 <= status_code < 300:
        return status_code, ""
    return status_code, f"HTTP {status_code}"
