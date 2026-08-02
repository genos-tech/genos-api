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

DNS is resolved at validation AND re-checked at send time. A name that
resolves publicly today can resolve to a private address tomorrow
(DNS rebinding); re-checking closes the window between the two.

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
import ipaddress
import json
import socket
from datetime import timedelta
from urllib.parse import urlparse

import requests
from django.utils import timezone

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

    ALL of them, not any: a hostname with both a public and a private A
    record would otherwise pass validation and be sent to the private
    one, depending on resolver order.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local  # 169.254.0.0/16 — cloud metadata lives here
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


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
        # Re-validate immediately before sending: a name that resolved
        # publicly at configuration time can resolve privately now.
        validate_webhook_url(url)
    except WebhookUrlError as exc:
        return None, str(exc)

    try:
        resp = requests.post(
            url,
            data=body,
            headers=headers,
            timeout=TIMEOUT_SECONDS,
            # Refused, not followed. A 302 to a private address would
            # defeat every check above.
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return None, str(exc)[:300]

    if 200 <= resp.status_code < 300:
        return resp.status_code, ""
    return resp.status_code, f"HTTP {resp.status_code}"
