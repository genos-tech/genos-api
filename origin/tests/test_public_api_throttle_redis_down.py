"""The public API rate limit with Redis down (MIP-2266).

It used to fail OPEN: a dead counter store meant no limit at all, so a leaked key could read at
machine speed during an outage. It now counts in this process against the same rate and refuses
past it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from origin.models.common.api_key_models import SCOPE_READ, ApiKey
from origin.services import throttles
from origin.tests.test_throttles import _DeadCaches
from origin.views.public import permissions
from origin.views.public.permissions import McpApiThrottle, PublicApiThrottle


def _request(key: ApiKey):
    return SimpleNamespace(
        auth=key, user=SimpleNamespace(pk=1, is_authenticated=True), method="GET", path="/x"
    )


class TestPublicApiThrottleWithRedisDown(TestCase):
    def setUp(self):
        super().setUp()
        self.person = get_user_model().objects.create_user(
            username="thr", email="thr@example.com", password="pw"
        )
        permissions._local_counts.clear()
        self.addCleanup(permissions._local_counts.clear)
        self.addCleanup(setattr, throttles, "_down_until", 0.0)
        patcher = mock.patch.object(throttles, "caches", _DeadCaches())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _key(self, key_id):
        return ApiKey(id=key_id, user=self.person, name="k", prefix="gnos_aaaaaa", scope=SCOPE_READ)

    def _admitted(self, throttle_class, key, n, rate):
        with mock.patch.object(throttle_class, "rate", rate, create=True):
            out = []
            for _ in range(n):
                throttle = throttle_class()
                out.append(throttle.allow_request(_request(key), None))
            return out

    def test_it_refuses_past_the_rate_instead_of_allowing_everything(self):
        key = self._key("11111111-1111-1111-1111-111111111111")
        with self.assertLogs("origin.views.public.permissions", "WARNING") as logs:
            results = self._admitted(PublicApiThrottle, key, 5, "3/min")
        self.assertEqual(results, [True, True, True, False, False])
        self.assertTrue(any("action=refuse" in line for line in logs.output))

    def test_each_credential_has_its_own_count(self):
        first = self._key("11111111-1111-1111-1111-111111111111")
        second = self._key("22222222-2222-2222-2222-222222222222")
        with self.assertLogs("origin.views.public.permissions", "WARNING"):
            self.assertEqual(
                self._admitted(PublicApiThrottle, first, 3, "2/min"), [True, True, False]
            )
            self.assertEqual(self._admitted(PublicApiThrottle, second, 2, "2/min"), [True, True])

    def test_the_mcp_backstop_counts_too(self):
        key = self._key("33333333-3333-3333-3333-333333333333")
        with self.assertLogs("origin.views.public.permissions", "WARNING"):
            self.assertEqual(self._admitted(McpApiThrottle, key, 3, "2/min"), [True, True, False])

    def test_a_request_with_no_credential_is_left_to_the_permission_class(self):
        anonymous = SimpleNamespace(auth=None, user=None, method="GET", path="/x")
        self.assertTrue(PublicApiThrottle().allow_request(anonymous, None))
