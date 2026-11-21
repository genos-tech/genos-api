"""Every test starts with an empty default cache (MIP-1803, `origin/tests/cache_isolation.py`).

Unittest runs the methods of a class in name order, so the two classes below meet the same key in
both orders: the writer first and the reader second, then the reader first and the writer second.
Both readers must find the cache empty.
"""

import unittest
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

KEY = "mip1803:leak-probe"


class WriterFirstTests(SimpleTestCase):
    def test_a_writes(self):
        self.assertIsNone(cache.get(KEY))
        cache.set(KEY, "left behind", 3600)

    def test_b_sees_an_empty_cache(self):
        self.assertIsNone(cache.get(KEY))


class ReaderFirstTests(SimpleTestCase):
    def test_a_sees_an_empty_cache(self):
        self.assertIsNone(cache.get(KEY))

    def test_b_writes(self):
        self.assertIsNone(cache.get(KEY))
        cache.set(KEY, "left behind", 3600)


class DatabaseTestCaseTests(TestCase):
    """`TestCase` (with its transaction) is covered too: it is what most of the suite subclasses."""

    def test_a_writes(self):
        cache.set(KEY, "left behind", 3600)

    def test_b_sees_an_empty_cache(self):
        self.assertIsNone(cache.get(KEY))


class NestedProbe(SimpleTestCase):
    def test_sees_the_cache(self):
        self.seen = cache.get(KEY)


class HookTests(SimpleTestCase):
    def test_a_test_run_after_a_write_sees_an_empty_cache_whatever_the_method_order(self):
        # Order-independent: write here, then run another test case on the spot.
        cache.set(KEY, "left behind", 3600)
        case = NestedProbe("test_sees_the_cache")
        result = unittest.TestResult()
        unittest.TestSuite([case]).run(result)  # `case(result)`, as the runner does
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertIsNone(case.seen)

    def test_a_cache_that_is_not_in_process_is_left_alone(self):
        from django.test import override_settings

        class Redisish:
            cleared = False

            def clear(self):
                self.cleared = True

        fake = Redisish()
        cfg = {"default": {"BACKEND": "django_redis.cache.RedisCache", "LOCATION": "redis://x"}}
        with override_settings(CACHES=cfg), mock.patch("origin.tests.cache_isolation.cache", fake):
            unittest.TestSuite([NestedProbe("test_sees_the_cache")]).run(unittest.TestResult())
        self.assertFalse(fake.cleared)

    def test_the_hook_is_installed(self):
        from origin.tests import cache_isolation

        self.assertTrue(SimpleTestCase._cache_isolation)
        cache_isolation.install()  # a second call is harmless
