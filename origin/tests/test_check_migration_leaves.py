"""Two same-numbered migrations must be caught before the second one merges (MIP-1098).

On 2026-11-01 and again on 2026-11-06, two PRs each added `0NNN_*` off the same parent.
Each passed CI alone; on `main` together they left two leaf migrations, `manage.py migrate`
refused to run ("Conflicting migrations detected") and the deploy failed.

`scripts/check_migration_leaves.py` finds that from the files alone (standard library, no
install). `migration-guard.yml` runs it on a PR merged with its base, and again for every
open migration PR whenever `main` changes, posting the `migration-leaf` status. The checker
is tested on hand-made trees, the two shell steps with a fake `gh` and a real git repo, and
the workflow on its text (a trigger or permission lost there turns the guard off silently).
"""

import importlib.util
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path
from unittest import TestCase, skipUnless

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
_spec = importlib.util.spec_from_file_location(
    "check_migration_leaves", SCRIPTS / "check_migration_leaves.py"
)
leaves = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leaves)


def migration(deps=(), *, extra="", replaces=(), app="origin"):
    parts = ", ".join(f'("{app}", "{d}")' for d in deps)
    lines = [
        "from django.db import migrations",
        "",
        "",
        "class Migration(migrations.Migration):",
        f"    dependencies = [{parts}]"
        if not extra
        else f"    dependencies = [{extra}{', ' if parts else ''}{parts}]",
    ]
    if replaces:
        r = ", ".join(f'("{app}", "{x}")' for x in replaces)
        lines.append(f"    replaces = [{r}]")
    lines.append("    operations = []")
    return "\n".join(lines) + "\n"


def write(root: Path, app: str, files: dict[str, str]) -> None:
    folder = root / app / "migrations"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "__init__.py").write_text("")
    for name, text in files.items():
        (folder / f"{name}.py").write_text(text)


class CheckerTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_check(self):
        return subprocess.run(
            ["python3", str(SCRIPTS / "check_migration_leaves.py"), str(self.root)],
            capture_output=True,
            text=True,
        )

    def test_a_single_chain_has_one_leaf_and_passes(self):
        write(
            self.root,
            "origin",
            {
                "0001_initial": migration(),
                "0002_a": migration(["0001_initial"]),
                "0003_b": migration(["0002_a"]),
            },
        )
        self.assertEqual(leaves.leaves_by_app(self.root), {"origin": ["0003_b"]})
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("origin: 0003_b", result.stdout)

    def test_two_same_numbered_migrations_are_a_conflict_naming_both(self):
        write(
            self.root,
            "origin",
            {
                "0001_initial": migration(),
                "0220_task_recent": migration(["0001_initial"]),
                "0220_worklog": migration(["0001_initial"]),
            },
        )
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("CONFLICT in origin: 2 leaf migrations", result.stderr)
        self.assertIn("0220_task_recent", result.stderr)
        self.assertIn("0220_worklog", result.stderr)

    def test_a_merge_migration_resolves_it(self):
        write(
            self.root,
            "origin",
            {
                "0001_initial": migration(),
                "0220_task_recent": migration(["0001_initial"]),
                "0220_worklog": migration(["0001_initial"]),
                "0221_merge": migration(["0220_task_recent", "0220_worklog"]),
            },
        )
        self.assertEqual(self.run_check().returncode, 0)

    def test_different_numbers_on_the_same_parent_still_conflict(self):
        # The number is not the point: two heads off one parent is.
        write(
            self.root,
            "origin",
            {
                "0001_initial": migration(),
                "0002_a": migration(["0001_initial"]),
                "0003_b": migration(["0001_initial"]),
            },
        )
        self.assertEqual(self.run_check().returncode, 1)

    def test_a_swappable_dependency_does_not_hide_the_real_ones(self):
        # `migrations.swappable_dependency(...)` is a call, so the list is not a literal as a
        # whole. A first draft dropped the whole list and reported old migrations as leaves.
        write(
            self.root,
            "origin",
            {
                "0001_initial": migration(
                    extra="migrations.swappable_dependency(settings.AUTH_USER_MODEL)"
                ),
                "0002_a": migration(
                    ["0001_initial"],
                    extra="migrations.swappable_dependency(settings.AUTH_USER_MODEL)",
                ),
            },
        )
        self.assertEqual(leaves.leaves_by_app(self.root), {"origin": ["0002_a"]})

    def test_a_squashed_migration_retires_what_it_replaces(self):
        write(
            self.root,
            "origin",
            {
                "0001_initial": migration(),
                "0002_a": migration(["0001_initial"]),
                "0001_squashed_0002": migration(replaces=["0001_initial", "0002_a"]),
                "0003_b": migration(["0001_squashed_0002"]),
            },
        )
        self.assertEqual(leaves.leaves_by_app(self.root), {"origin": ["0003_b"]})

    def test_each_app_is_judged_on_its_own(self):
        write(
            self.root,
            "origin",
            {"0001_initial": migration(), "0002_a": migration(["0001_initial"])},
        )
        write(
            self.root / "origin",
            "search_engine",
            {
                "0001_initial": migration(),
                "0002_x": migration(["0001_initial"], app="search_engine"),
                "0002_y": migration(["0001_initial"], app="search_engine"),
            },
        )
        found = leaves.leaves_by_app(self.root)
        self.assertEqual(found["origin"], ["0002_a"])
        self.assertEqual(found["search_engine"], ["0002_x", "0002_y"])
        result = self.run_check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("CONFLICT in search_engine", result.stderr)
        self.assertNotIn("CONFLICT in origin", result.stderr)

    def test_a_dependency_on_another_apps_migration_does_not_retire_a_leaf(self):
        # Only a name in THIS app's folder retires a leaf of this app.
        write(
            self.root,
            "origin",
            {
                "0001_initial": migration(),
                "0002_a": migration(["0001_initial"]),
            },
        )
        write(
            self.root / "origin",
            "search_engine",
            {
                "0001_initial": 'from django.db import migrations\n\nclass Migration(migrations.Migration):\n    dependencies = [("origin", "0001_initial")]\n    operations = []\n'
            },
        )
        found = leaves.leaves_by_app(self.root)
        self.assertEqual(found["origin"], ["0002_a"])
        self.assertEqual(found["search_engine"], ["0001_initial"])

    def test_nothing_to_read_is_not_a_pass(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 2)

    def test_an_unreadable_file_is_exit_2_not_a_pass(self):
        write(self.root, "origin", {"0001_initial": "def broken(:\n"})
        self.assertEqual(self.run_check().returncode, 2)

    def test_the_real_tree_has_one_leaf_per_app(self):
        found = leaves.leaves_by_app(ROOT)
        self.assertIn("origin", found)
        self.assertIn("search_engine", found)
        for app, names in found.items():
            self.assertEqual(len(names), 1, f"{app}: {names}")


class FakeGh:
    """A `gh` on PATH that records `gh api` calls and answers `gh pr list` with fixed lines."""

    def __init__(self, folder: Path, pr_lines: str = ""):
        self.log = folder / "gh.log"
        self.log.write_text("")
        (folder / "pr_list.txt").write_text(pr_lines)
        script = folder / "gh"
        script.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env bash
                if [ "$1" = "pr" ] && [ "$2" = "list" ]; then cat "{folder}/pr_list.txt"; exit 0; fi
                printf '%s\\n' "$*" >> "{self.log}"
                """
            )
        )
        script.chmod(0o755)
        self.folder = folder

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line]

    def env(self) -> dict[str, str]:
        return {
            **os.environ,
            "PATH": f"{self.folder}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "genos-tech/genos-api",
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "42",
        }


class PostStatusTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.gh = FakeGh(Path(self._tmp.name))

    def post(self, code, out=""):
        subprocess.run(
            ["bash", str(SCRIPTS / "post_migration_status.sh"), "abc123def", str(code), out],
            env=self.gh.env(),
            check=True,
            capture_output=True,
        )
        return self.gh.calls()[-1]

    def test_exit_0_is_success(self):
        call = self.post(0)
        self.assertIn("statuses/abc123def", call)
        self.assertIn("state=success", call)
        self.assertIn("context=migration-leaf", call)

    def test_exit_1_is_failure_with_the_conflict_line_as_its_description(self):
        call = self.post(1, "origin: a, b\nCONFLICT in origin: 2 leaf migrations (a, b). fix it")
        self.assertIn("state=failure", call)
        self.assertIn("description=CONFLICT in origin: 2 leaf migrations (a, b)", call)

    def test_any_other_exit_is_error_never_success(self):
        for code in (2, 127, 255):
            self.assertIn("state=error", self.post(code), code)

    def test_the_description_fits_a_status(self):
        call = self.post(1, "CONFLICT " + "x" * 400)
        desc = call.split("description=")[1].split(" -f ")[0]
        self.assertLessEqual(len(desc), 140)

    def test_it_links_to_the_run(self):
        self.assertIn("actions/runs/42", self.post(0))


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@skipUnless(shutil.which("git"), "needs git (CI has it)")
class RetestOpenPrsTests(TestCase):
    """The push-to-main step, on a real git repo: A merged into `main`, B still open."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.origin = tmp / "origin.git"
        self.clone = tmp / "clone"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.clone)],
            check=True,
            capture_output=True,
        )
        write(
            self.clone,
            "origin",
            {"0001_initial": migration(), "0002_a": migration(["0001_initial"])},
        )
        # The script lives in the repo it runs in (as in CI).
        (self.clone / "scripts").mkdir()
        for name in (
            "check_migration_leaves.py",
            "post_migration_status.sh",
            "retest_migration_prs.sh",
        ):
            (self.clone / "scripts" / name).write_text((SCRIPTS / name).read_text())
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-qm", "base")
        git(self.clone, "push", "-q", "origin", "main")
        self.fakes = tmp / "fakes"
        self.fakes.mkdir()

    def open_pr(self, number, filename, deps):
        """A PR branch off the base that adds one migration; returns its head sha and publishes
        refs/pull/<n>/head like GitHub does."""
        git(self.clone, "checkout", "-q", "-b", f"pr{number}", "main")
        (self.clone / "origin" / "migrations" / f"{filename}.py").write_text(migration(deps))
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-qm", f"pr {number}")
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.clone, capture_output=True, text=True, check=True
        ).stdout.strip()
        git(self.clone, "push", "-q", "origin", f"HEAD:refs/pull/{number}/head")
        git(self.clone, "checkout", "-q", "main")
        return sha

    def land(self, filename, deps):
        """Merge another migration straight into main (a PR that was merged)."""
        (self.clone / "origin" / "migrations" / f"{filename}.py").write_text(migration(deps))
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-qm", f"land {filename}")
        git(self.clone, "push", "-q", "origin", "main")

    def retest(self, pr_lines):
        gh = FakeGh(self.fakes, pr_lines)
        result = subprocess.run(
            ["bash", "scripts/retest_migration_prs.sh", "main"],
            cwd=self.clone,
            env=gh.env(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return gh.calls(), result.stdout

    def test_a_pr_that_clashes_with_what_just_landed_turns_red(self):
        b = self.open_pr(7, "0003_b", ["0002_a"])
        self.land("0003_a", ["0002_a"])  # PR A merged first, same number
        calls, out = self.retest(f"7 {b}\n")
        self.assertEqual(len(calls), 1, out)
        self.assertIn(f"statuses/{b}", calls[0])
        self.assertIn("state=failure", calls[0])
        self.assertIn("0003_a", calls[0] + out)

    def test_a_pr_that_still_fits_stays_green(self):
        b = self.open_pr(7, "0003_b", ["0002_a"])
        calls, out = self.retest(f"7 {b}\n")
        self.assertIn("state=success", calls[0], out)

    def test_a_pr_built_on_what_landed_stays_green(self):
        # It depends on the migration that landed, so there is still one leaf.
        self.land("0003_a", ["0002_a"])
        b = self.open_pr(7, "0004_b", ["0003_a"])
        calls, out = self.retest(f"7 {b}\n")
        self.assertIn("state=success", calls[0], out)

    def test_each_open_pr_gets_its_own_status(self):
        b = self.open_pr(7, "0003_b", ["0002_a"])
        c = self.open_pr(8, "0003_c", ["0002_a"])
        calls, _ = self.retest(f"7 {b}\n8 {c}\n")
        self.assertEqual(len(calls), 2)
        self.assertTrue(any(f"statuses/{b}" in c_ for c_ in calls))
        self.assertTrue(any(f"statuses/{c}" in c_ for c_ in calls))

    def test_no_migration_pr_means_no_status_and_a_clean_run(self):
        calls, out = self.retest("")
        self.assertEqual(calls, [])
        self.assertIn("no open PR", out)

    def test_a_pr_that_no_longer_merges_cleanly_is_red_not_skipped(self):
        # Same file name, different content: a textual conflict with what landed.
        b = self.open_pr(7, "0003_x", ["0002_a"])
        self.land("0003_x", ["0001_initial"])
        calls, out = self.retest(f"7 {b}\n")
        self.assertIn("state=failure", calls[0], out)
        self.assertIn("no longer merges cleanly", calls[0] + out)


class WorkflowTextTests(TestCase):
    """The workflow's wiring. Lose one of these and the guard is off while every check is green."""

    text = (ROOT / ".github" / "workflows" / "migration-guard.yml").read_text()

    def test_it_runs_on_prs_on_pushes_to_main_and_by_hand(self):
        self.assertRegex(self.text, r"(?m)^  push:\n    branches: \[main\]")
        self.assertRegex(self.text, r"(?m)^  pull_request:")
        self.assertRegex(self.text, r"(?m)^  workflow_dispatch:")

    def test_it_may_write_statuses_and_nothing_else_wide(self):
        self.assertIn("statuses: write", self.text)
        self.assertNotIn("contents: write", self.text)
        self.assertNotIn("pull-requests: write", self.text)

    def test_the_context_is_the_one_the_ruleset_will_require(self):
        self.assertIn("CONTEXT: migration-leaf", self.text)

    def test_it_checks_out_full_history_to_merge_a_pr_head(self):
        self.assertIn("fetch-depth: 0", self.text)

    def test_actions_are_pinned_to_a_commit(self):
        for line in self.text.splitlines():
            if re.match(r"\s*(- )?uses:", line):
                self.assertRegex(line, r"@[0-9a-f]{40}\b", line)

    def test_ci_runs_the_checker_before_installing_anything(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("python3 scripts/check_migration_leaves.py .", ci)
        self.assertLess(
            ci.index("check_migration_leaves.py"), ci.index("pip install pipenv==2026.7.1")
        )
