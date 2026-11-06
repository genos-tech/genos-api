"""The CI test split must never drop or double-run a test (MIP-1095).

CI runs the suite as three jobs, each given a list of test modules by
`scripts/ci_test_shard.py`. A module missing from every list is a test that silently
never runs while every check is green, so the properties that matter are pinned here:
every module is in exactly one shard, the split is the same every time, a new test
file lands somewhere on its own, and no shard gets all the big files.
"""

import importlib.util
import tempfile
from pathlib import Path
from unittest import TestCase, mock

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci_test_shard.py"
_spec = importlib.util.spec_from_file_location("ci_test_shard", SCRIPT)
shard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shard)


class RealSuiteTests(TestCase):
    def test_every_test_file_is_in_exactly_one_shard(self):
        everything = shard.test_files()
        for total in (1, 2, 3, 4, 7):
            placed = [p for files in shard.split(total) for p in files]
            self.assertEqual(sorted(placed), everything, f"total={total}")
            self.assertEqual(len(placed), len(set(placed)), f"total={total}: a file twice")

    def test_it_covers_the_files_django_discovery_collects(self):
        # `manage.py test origin.tests` collects test*.py below origin/tests.
        collected = {p for p in (shard.TESTS).rglob("test*.py")}
        self.assertEqual(set(shard.test_files()), collected)
        self.assertGreater(len(collected), 100)  # not an empty match

    def test_this_file_is_in_a_shard(self):
        here = Path(__file__).resolve()
        self.assertEqual(sum(here in files for files in shard.split(3)), 1)

    def test_the_split_is_the_same_every_time(self):
        self.assertEqual(shard.split(3), shard.split(3))

    def test_the_shards_are_balanced_by_test_count(self):
        loads = [sum(shard.weight(f) for f in files) for files in shard.split(3)]
        heaviest_file = max(shard.weight(f) for f in shard.test_files())
        # Heaviest-first greedy: the spread can't exceed one file.
        self.assertLessEqual(max(loads) - min(loads), heaviest_file)
        self.assertGreater(min(loads), 0)

    def test_labels_are_dotted_module_paths_manage_py_accepts(self):
        labels = [shard.label(p) for p in shard.test_files()]
        self.assertIn("origin.tests.test_ci_test_shard", labels)
        for label in labels:
            self.assertTrue(label.startswith("origin.tests."), label)
            self.assertNotIn("/", label)
            self.assertFalse(label.endswith(".py"), label)


class OnATreeTests(TestCase):
    """The same rules on a small tree whose answer can be read off."""

    def build(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, body in files.items():
            path = root / "origin" / "tests" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        patches = [
            mock.patch.object(shard, "ROOT", root),
            mock.patch.object(shard, "TESTS", root / "origin" / "tests"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return root

    def names(self, total):
        return [sorted(p.name for p in files) for files in shard.split(total)]

    def test_files_go_to_the_lightest_shard_heaviest_first(self):
        def tests(n):
            return "".join(f"    def test_{i}(self): pass\n" for i in range(n))

        self.build(
            {
                "test_big.py": tests(10),
                "test_mid_a.py": tests(5),
                "test_mid_b.py": tests(5),
                "test_small.py": tests(1),
            }
        )
        # big (10) -> shard 1; mid_a (5) and mid_b (5) -> shard 2; the two are level
        # at 10, so the small one goes to the lower-numbered shard.
        self.assertEqual(
            self.names(2),
            [["test_big.py", "test_small.py"], ["test_mid_a.py", "test_mid_b.py"]],
        )

    def test_a_new_test_file_lands_in_a_shard_without_any_edit(self):
        self.build({"test_a.py": "    def test_x(self): pass\n"})
        before = [p for files in shard.split(3) for p in files]
        self.assertEqual(len(before), 1)
        (shard.TESTS / "test_new.py").write_text("    def test_y(self): pass\n")
        after = sorted(p.name for files in shard.split(3) for p in files)
        self.assertEqual(after, ["test_a.py", "test_new.py"])

    def test_only_test_files_count_and_subpackages_are_found(self):
        self.build(
            {
                "test_a.py": "",
                "tests.py": "",
                "helpers.py": "",
                "cross_team_fixtures.py": "",
                "sub/test_b.py": "",
                "sub/util.py": "",
            }
        )
        labels = sorted(shard.label(p) for files in shard.split(2) for p in files)
        # `tests.py` matches Django's `test*.py` pattern, so discovery collects it too.
        self.assertEqual(
            labels, ["origin.tests.sub.test_b", "origin.tests.test_a", "origin.tests.tests"]
        )

    def test_a_file_with_no_test_method_still_counts_for_one(self):
        self.build({"test_empty.py": "x = 1\n"})
        self.assertEqual(shard.weight(shard.test_files()[0]), 1)

    def test_async_test_methods_are_counted(self):
        self.build(
            {"test_async.py": "    async def test_a(self): pass\n    def test_b(self): pass\n"}
        )
        self.assertEqual(shard.weight(shard.test_files()[0]), 2)

    def test_more_shards_than_files_leaves_some_empty_but_loses_nothing(self):
        self.build({"test_a.py": "", "test_b.py": ""})
        shards = shard.split(5)
        self.assertEqual(sum(len(f) for f in shards), 2)
        self.assertEqual(len(shards), 5)


class CommandLineTests(TestCase):
    def run_main(self, *args):
        with mock.patch("builtins.print") as printed:
            code = shard.main(["ci_test_shard.py", *args])
        return code, [call.args[0] for call in printed.call_args_list if call.args]

    def test_prints_one_shards_modules_space_separated(self):
        code, out = self.run_main("1", "3")
        self.assertEqual(code, 0)
        labels = out[0].split(" ")
        self.assertTrue(all(label.startswith("origin.tests.") for label in labels))
        self.assertEqual(labels, sorted(labels))

    def test_the_three_shards_printed_together_are_every_module(self):
        seen = []
        for n in ("1", "2", "3"):
            seen += self.run_main(n, "3")[1][0].split(" ")
        self.assertEqual(sorted(seen), sorted(shard.label(p) for p in shard.test_files()))

    def test_a_shard_outside_the_range_is_refused(self):
        for args in (("0", "3"), ("4", "3"), ("-1", "3")):
            self.assertEqual(self.run_main(*args)[0], 2, args)

    def test_wrong_arguments_are_refused(self):
        self.assertEqual(self.run_main()[0], 2)
        self.assertEqual(self.run_main("1")[0], 2)

    def test_report_lists_every_shard(self):
        code, out = self.run_main("--report", "3")
        self.assertEqual(code, 0)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(line.startswith("shard ") for line in out))

    def test_zero_shards_is_an_error(self):
        with self.assertRaises(ValueError):
            shard.split(0)
