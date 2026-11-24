"""GitHub secret scanning verification endpoint (MIP-2271), on fixtures signed by a local P-256 key
that stands in for GitHub's published one."""

import base64
import json
from datetime import timedelta
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache
from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.api_key_models import ApiKey, generate_key, hash_key
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.services import secret_scanning
from origin.tests.test_base import BaseAPITestCase

URL = "/api/public/v1/secret-scanning/"
KEY_ID = "test-key-1"

_PRIVATE = ec.generate_private_key(ec.SECP256R1())
_PEM = (
    _PRIVATE.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)


def _sign(body: bytes, private=_PRIVATE) -> str:
    return base64.b64encode(private.sign(body, ec.ECDSA(hashes.SHA256()))).decode()


class SecretScanningTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.keys = mock.patch.object(secret_scanning, "_fetch_keys", return_value={KEY_ID: _PEM})
        self.fetch = self.keys.start()
        self.addCleanup(self.keys.stop)

    def _make_key(self, **kwargs):
        raw = generate_key()
        key = ApiKey.objects.create(
            user=self.user, name="ci", key_hash=hash_key(raw), prefix=raw[:11], **kwargs
        )
        return raw, key

    def _post(self, payload, *, sign=True, key_id=KEY_ID, private=_PRIVATE, signature=None):
        body = json.dumps(payload).encode()
        headers = {}
        if key_id is not None:
            headers["HTTP_GITHUB_PUBLIC_KEY_IDENTIFIER"] = key_id
        if sign:
            headers["HTTP_GITHUB_PUBLIC_KEY_SIGNATURE"] = signature or _sign(body, private)
        return self.client.post(URL, data=body, content_type="application/json", **headers)

    def _match(self, raw):
        return {
            "token": raw,
            "type": "genos_api_key",
            "url": "https://github.com/x/y",
            "source": "content",
        }

    def test_live_key_is_revoked_and_reported_true_positive(self):
        raw, key = self._make_key()
        resp = self._post([self._match(raw)])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            [{"token_raw": raw, "token_type": "genos_api_key", "label": "true_positive"}],
        )
        key.refresh_from_db()
        self.assertIsNotNone(key.revoked_at)

    def test_revocation_writes_event_and_notifies_owner_without_the_token(self):
        raw, key = self._make_key()
        self._post([self._match(raw)])
        event = SecurityEvent.objects.get(action=SecurityAction.API_KEY_REVOKED)
        self.assertEqual(event.metadata["by"], "github_secret_scanning")
        self.assertEqual(event.target_id, str(key.id))
        self.assertNotIn(raw, json.dumps(event.metadata) + event.target_label)
        notice = Activity.objects.filter(
            recipient=self.user, activity_type=ActivityType.AI_APP
        ).first()
        if notice is not None:  # needs a team membership to file under; the test user has one
            self.assertEqual(notice.meta["action"], "leaked_key")
            self.assertNotIn(raw, json.dumps(notice.meta))

    def test_owner_notice_is_made(self):
        raw, _ = self._make_key()
        self._post([self._match(raw)])
        self.assertTrue(
            Activity.objects.filter(
                recipient=self.user,
                activity_type=ActivityType.AI_APP,
                meta__action="leaked_key",
            ).exists()
        )

    def test_unknown_revoked_and_expired_keys_are_false_positives(self):
        raw_revoked, key = self._make_key()
        key.revoke()
        raw_expired, _ = self._make_key(expires_at=timezone.now() - timedelta(days=1))
        unknown = generate_key()
        resp = self._post([self._match(t) for t in (raw_revoked, raw_expired, unknown)])
        self.assertEqual([a["label"] for a in resp.json()], ["false_positive"] * 3)
        self.assertFalse(
            SecurityEvent.objects.filter(metadata__by="github_secret_scanning").exists()
        )

    def test_malformed_token_is_a_false_positive_and_is_not_looked_up(self):
        with mock.patch.object(secret_scanning.ApiKey.objects, "select_related") as lookup:
            resp = self._post([self._match("gnos_short"), {"token": 5}, {}])
        self.assertEqual([a["label"] for a in resp.json()], ["false_positive"] * 3)
        lookup.assert_not_called()

    def test_several_matches_in_one_delivery(self):
        raw_a, key_a = self._make_key()
        raw_b, key_b = self._make_key()
        resp = self._post([self._match(raw_a), self._match(raw_b)])
        self.assertEqual([a["label"] for a in resp.json()], ["true_positive"] * 2)
        key_a.refresh_from_db()
        key_b.refresh_from_db()
        self.assertTrue(key_a.revoked_at and key_b.revoked_at)

    def test_missing_signature_headers_are_refused_and_nothing_is_revoked(self):
        raw, key = self._make_key()
        self.assertEqual(self._post([self._match(raw)], sign=False).status_code, 401)
        self.assertEqual(self._post([self._match(raw)], key_id=None).status_code, 401)
        key.refresh_from_db()
        self.assertIsNone(key.revoked_at)

    def test_signature_by_another_key_is_refused(self):
        raw, key = self._make_key()
        other = ec.generate_private_key(ec.SECP256R1())
        resp = self._post([self._match(raw)], private=other)
        self.assertEqual(resp.status_code, 401)
        key.refresh_from_db()
        self.assertIsNone(key.revoked_at)

    def test_tampered_body_and_garbage_signature_are_refused(self):
        raw, key = self._make_key()
        body = json.dumps([self._match(raw)]).encode()
        sig = _sign(b"something else")
        resp = self.client.post(
            URL,
            data=body,
            content_type="application/json",
            HTTP_GITHUB_PUBLIC_KEY_IDENTIFIER=KEY_ID,
            HTTP_GITHUB_PUBLIC_KEY_SIGNATURE=sig,
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self._post([self._match(raw)], signature="!!!not base64").status_code, 401)
        key.refresh_from_db()
        self.assertIsNone(key.revoked_at)

    def test_unknown_key_id_is_refused_and_refetches_keys_at_most_once_a_minute(self):
        raw, _ = self._make_key()
        for _ in range(3):
            self.assertEqual(self._post([self._match(raw)], key_id="nope").status_code, 401)
        # one cached fetch at first use + one refresh; the cooldown stops the rest
        self.assertLessEqual(self.fetch.call_count, 2)

    def test_unreachable_github_keys_fail_closed(self):
        raw, key = self._make_key()
        self.fetch.side_effect = OSError("down")
        self.assertEqual(self._post([self._match(raw)]).status_code, 401)
        key.refresh_from_db()
        self.assertIsNone(key.revoked_at)

    def test_signed_but_not_a_bounded_array_is_400(self):
        self.assertEqual(self._post({"token": "x"}).status_code, 400)
        too_many = [{"token": "x"}] * (secret_scanning.MAX_MATCHES + 1)
        self.assertEqual(self._post(too_many).status_code, 400)

    def test_oversize_body_is_413(self):
        pad = "a" * (secret_scanning.MAX_BODY_BYTES + 1)
        self.assertEqual(self._post([{"token": pad}]).status_code, 413)

    def test_unsigned_requests_are_throttled_per_address(self):
        for _ in range(60):
            self.assertEqual(self._post([], sign=False).status_code, 401)
        self.assertEqual(self._post([], sign=False).status_code, 429)

    def test_no_session_or_key_is_needed_and_get_is_not_allowed(self):
        self.assertEqual(self.client.get(URL).status_code, 405)

    def test_token_is_never_logged(self):
        raw, _ = self._make_key()
        with self.assertLogs("origin", level="DEBUG") as logs:
            self._post([self._match(raw)])
            self._post([self._match(raw)], sign=False)
        self.assertNotIn(raw, "\n".join(logs.output))

    def test_regex_matches_generated_keys(self):
        import re

        for _ in range(50):
            self.assertRegex(generate_key(), rf"\A{secret_scanning.TOKEN_REGEX}\Z")
        self.assertIsNone(re.match(secret_scanning.TOKEN_REGEX, "gnos_short"))
