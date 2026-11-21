"""The patch-leak tripwire (MIP-1765, `origin/tests/patch_leak_tripwire.py`) catches what it claims to.

No database: the nested test cases are run on a private `TestResult`, so a failure here is
the tripwire's, never the suite's.
"""

import importlib
import threading
import unittest
from unittest import mock

from django.test import SimpleTestCase

from origin.search_engine import search as S
from origin.tests import patch_leak_tripwire as tripwire


def run_nested(case_class, name):
    """Run one test of `case_class` through the loader, so `setUpClass` and the class cleanups run."""
    result = unittest.TestResult()
    suite = unittest.TestSuite([case_class(name)])
    suite.run(result)
    return result


class WatchedModulesTests(SimpleTestCase):
    def test_every_watched_module_imports_and_has_callables(self):
        for name in tripwire.WATCHED_MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                self.assertTrue(
                    any(callable(v) for v in vars(module).values()), f"{name} has nothing to watch"
                )

    def test_the_hook_is_installed(self):
        self.assertTrue(SimpleTestCase._patch_leak_tripwire)
        tripwire.install()  # a second call is harmless
        self.assertTrue(SimpleTestCase._patch_leak_tripwire)


class TripwireTests(SimpleTestCase):
    def test_a_started_and_never_stopped_patch_fails_that_test_by_name_and_is_put_back(self):
        real = S._run_keyword

        patcher = mock.patch.object(S, "_run_keyword", mock.MagicMock())

        class Leaker(SimpleTestCase):
            def test_leaks(self):
                patcher.start()  # never stopped

        result = run_nested(Leaker, "test_leaks")
        try:
            self.assertEqual(len(result.failures), 1, result.failures + result.errors)
            test, message = result.failures[0]
            self.assertIn("Leaker.test_leaks", test.id())
            self.assertIn("Leaker.test_leaks", message)
            self.assertIn("origin.search_engine.search._run_keyword", message)
            self.assertIs(S._run_keyword, real, "the leaked attribute must be put back")
        finally:
            patcher.stop()
            self.assertIs(S._run_keyword, real)

    def test_a_leak_left_by_the_two_thread_patch_race_is_caught(self):
        # The MIP-1760 shape: a pool thread and the test thread both enter patch.object on the
        # same attribute and exit in the wrong order, so the module keeps the other's mock.
        real = S._embed_probe = S._run_keyword  # a stand-in lane of the same module

        class Racer(SimpleTestCase):
            def test_race(self):
                first_in = threading.Event()
                second_in = threading.Event()

                def pool_thread():
                    with mock.patch.object(S, "_embed_probe", mock.MagicMock()):
                        first_in.set()
                        second_in.wait(5)

                worker = threading.Thread(target=pool_thread)
                worker.start()
                first_in.wait(5)
                with mock.patch.object(S, "_embed_probe", mock.MagicMock()):
                    second_in.set()
                    worker.join()  # the worker restores first, then this thread restores its own "original"

        try:
            result = run_nested(Racer, "test_race")
            self.assertEqual(len(result.failures), 1, result.failures + result.errors)
            self.assertIn("_embed_probe", result.failures[0][1])
            self.assertIs(S._embed_probe, real)
        finally:
            del S._embed_probe

    def test_clean_patches_do_not_fail(self):
        class Clean(SimpleTestCase):
            @classmethod
            def setUpClass(cls):
                super().setUpClass()
                cls.class_patch = mock.patch.object(S, "_run_vector", mock.MagicMock())
                cls.class_patch.start()
                cls.addClassCleanup(cls.class_patch.stop)

            def setUp(self):
                patcher = mock.patch.object(S, "embed_one", mock.MagicMock())
                patcher.start()
                self.addCleanup(patcher.stop)

            def test_with(self):
                with mock.patch.object(S, "_run_keyword", mock.MagicMock()):
                    pass

            def test_enter_context(self):
                self.enterContext(mock.patch.object(S, "_run_keyword", mock.MagicMock()))

        for name in ("test_with", "test_enter_context"):
            with self.subTest(name):
                result = run_nested(Clean, name)
                self.assertEqual(result.failures + result.errors, [])
                self.assertTrue(result.wasSuccessful())

    def test_a_deleted_attribute_is_reported_as_removed_and_put_back(self):
        real = S.embed_one

        class Deleter(SimpleTestCase):
            def test_deletes(self):
                del S.embed_one

        try:
            result = run_nested(Deleter, "test_deletes")
            self.assertEqual(len(result.failures), 1, result.failures + result.errors)
            self.assertIn("embed_one: now removed", result.failures[0][1])
            self.assertIs(S.embed_one, real)
        finally:
            S.embed_one = real

    def test_a_test_that_fails_on_its_own_and_leaks_is_reported_for_both_and_restored(self):
        real = S._run_vector
        patcher = mock.patch.object(S, "_run_vector", mock.MagicMock())

        class Both(SimpleTestCase):
            def test_both(self):
                patcher.start()
                self.fail("its own failure")

        try:
            result = run_nested(Both, "test_both")
            messages = " ".join(m for _, m in result.failures)
            self.assertEqual(len(result.failures), 2, result.failures)
            self.assertIn("its own failure", messages)
            self.assertIn("_run_vector", messages)
            self.assertIs(S._run_vector, real)
        finally:
            patcher.stop()

    def test_a_skipped_test_is_not_a_failure(self):
        class Skipped(SimpleTestCase):
            @unittest.skip("not now")
            def test_skipped(self):
                pass

        result = run_nested(Skipped, "test_skipped")
        self.assertEqual((result.failures, result.errors, len(result.skipped)), ([], [], 1))

    def test_debug_mode_restores_and_reports_a_leak_too(self):
        real = S._run_keyword
        patcher = mock.patch.object(S, "_run_keyword", mock.MagicMock())

        class Leaker(SimpleTestCase):
            def test_leaks(self):
                patcher.start()

        try:
            with self.assertRaisesRegex(tripwire.PatchLeakError, "Leaker.test_leaks"):
                Leaker("test_leaks").debug()
            self.assertIs(S._run_keyword, real)
        finally:
            patcher.stop()
