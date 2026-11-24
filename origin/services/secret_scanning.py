"""GitHub secret scanning partner program: the verification endpoint's logic (MIP-2271).

GitHub scans public repositories (and gists, issues, ...) for strings that match a partner's
registered token pattern. For each match it POSTs a JSON array to the partner's endpoint:

    [{"token": "gnos_...", "type": "genos_api_key", "url": "https://github.com/...", "source": "content"}]

signed with ECDSA P-256 / SHA-256. The signature is in `Github-Public-Key-Signature` (base64 of the
ASN.1 DER signature) and the key it was made with is named in `Github-Public-Key-Identifier`; GitHub
publishes the current public keys at `KEYS_URL`. The partner answers with a JSON array

    [{"token_raw": "gnos_...", "token_type": "genos_api_key", "label": "true_positive"}]

where `label` is `true_positive` (a live credential: GitHub notifies the committer) or
`false_positive`.

**What this module does.** Verifies the signature against GitHub's published keys (fetched through
`outbound_url.fetch_public`, cached for an hour, refreshed once when a delivery names an unknown
key id), looks each token up by its SHA-256 hash (the key itself is never stored), revokes a live
key, writes an `api_key.revoked` security event (`by: github_secret_scanning`) and tells the owner
in the app. A key that is unknown, already revoked or expired is a `false_positive`: nothing live
leaked, and answering `true_positive` for it would make GitHub send a pointless alert.

**Never logged:** the token, the matched URL and the signature. Only counts and ids.

**Registration** (the partner form Ken sends; see the task): token type name `genos_api_key`,
regex `gnos_[A-Za-z0-9_-]{43}` (`generate_key`: the prefix plus `secrets.token_urlsafe(32)`, which is
always 43 characters), endpoint `https://api.genosai.dev/api/public/v1/secret-scanning/`, example
token `gnos_` followed by 43 characters, e.g. `gnos_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCdE`
(a placeholder; never a real key).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache

from origin.models.common.api_key_models import ApiKey, hash_key
from origin.models.common.security_event_models import SecurityAction, SecuritySource
from origin.services import mcp_notice, outbound_url
from origin.services.api_keys import key_label
from origin.services.security_events import record_security_event

logger = logging.getLogger(__name__)

KEYS_URL = "https://api.github.com/meta/public_keys/secret_scanning"
TOKEN_TYPE = "genos_api_key"
# What the key looks like (documented for the partner form). `token_urlsafe(32)` is 43 characters.
TOKEN_REGEX = r"gnos_[A-Za-z0-9_-]{43}"
_TOKEN_RE = re.compile(rf"\A{TOKEN_REGEX}\Z")

MAX_BODY_BYTES = 256 * 1024
# GitHub batches matches; a larger array is not GitHub.
MAX_MATCHES = 500
_KEYS_CACHE_KEY = "secret_scanning:github_keys"
_KEYS_TTL = 3600
# At most one refetch per this many seconds when a delivery names a key id we do not hold, so a
# stream of garbage key ids cannot make this endpoint hammer api.github.com.
_REFRESH_COOLDOWN_KEY = "secret_scanning:github_keys_refresh"
_REFRESH_COOLDOWN = 60

LABEL_TRUE = "true_positive"
LABEL_FALSE = "false_positive"


class SignatureError(Exception):
    """The delivery is not provably from GitHub."""


def _fetch_keys() -> dict[str, str]:
    """GitHub's current public keys, `{key_identifier: PEM}`. Raises on any failure."""
    result = outbound_url.fetch_public(
        KEYS_URL, timeout=5.0, max_bytes=64 * 1024, headers={"Accept": "application/json"}
    )
    if result.status != 200:
        raise SignatureError("keys unavailable")
    data = json.loads(result.body.decode("utf-8"))
    keys = {}
    for item in data.get("public_keys") or []:
        ident, pem = item.get("key_identifier"), item.get("key")
        if isinstance(ident, str) and isinstance(pem, str):
            keys[ident] = pem
    return keys


def _keys(*, refresh: bool = False) -> dict[str, str]:
    if not refresh:
        cached = cache.get(_KEYS_CACHE_KEY)
        if cached:
            return cached
    keys = _fetch_keys()
    if keys:
        cache.set(_KEYS_CACHE_KEY, keys, _KEYS_TTL)
    return keys


def _key_for(identifier: str) -> str | None:
    try:
        pem = _keys().get(identifier)
        if pem is None and cache.add(_REFRESH_COOLDOWN_KEY, 1, _REFRESH_COOLDOWN):
            pem = _keys(refresh=True).get(identifier)
    except (outbound_url.OutboundUrlError, OSError, ValueError, SignatureError):
        logger.warning("secret scanning: GitHub public keys could not be fetched")
        return None
    except Exception:  # noqa: BLE001 - urllib3 errors, cache errors: unverifiable means refused
        logger.warning("secret scanning: GitHub public keys lookup failed", exc_info=True)
        return None
    return pem


def verify_signature(body: bytes, key_identifier: str | None, signature_b64: str | None) -> None:
    """Raise `SignatureError` unless `body` was signed by the GitHub key named `key_identifier`."""
    if not key_identifier or not signature_b64 or len(key_identifier) > 128:
        raise SignatureError("missing headers")
    pem = _key_for(key_identifier)
    if pem is None:
        raise SignatureError("unknown key")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
        public_key = serialization.load_pem_public_key(pem.encode())
        if not isinstance(public_key, ec.EllipticCurvePublicKey):
            raise SignatureError("not an EC key")
        public_key.verify(signature, body, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, binascii.Error, ValueError) as exc:
        raise SignatureError("bad signature") from exc


@dataclass(frozen=True)
class Verdict:
    token: str
    label: str


def parse_matches(body: bytes) -> list[dict]:
    """The array GitHub sent. Raises `ValueError` for anything else."""
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, list) or len(data) > MAX_MATCHES:
        raise ValueError("expected a bounded array")
    return [m for m in data if isinstance(m, dict)]


def handle_match(token: object) -> str:
    """Revoke the key `token` names, if it is a live one. Returns the label to answer with."""
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        return LABEL_FALSE
    key = ApiKey.objects.select_related("user").filter(key_hash=hash_key(token)).first()
    if key is None or not key.is_active:
        return LABEL_FALSE
    key.revoke()
    record_security_event(
        SecurityAction.API_KEY_REVOKED,
        actor=key.user,
        team_id=key.team_id,
        target_type="api_key",
        target_id=key.id,
        target_label=key_label(key),
        metadata={"by": "github_secret_scanning", "owner_id": str(key.user_id)},
        force_source=SecuritySource.SYSTEM,
    )
    mcp_notice.notify_key_leaked(key)
    logger.warning("secret scanning: key %s revoked after a public leak", key.id)
    return LABEL_TRUE
