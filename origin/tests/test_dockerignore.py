"""`.dockerignore` keeps out of the image what a container never reads (MIP-1102).

The Dockerfile ends with `COPY . .`, so `.dockerignore` decides what ships. Leaving out
too little ships the git history, the tests and any local `.env`. Leaving out too MUCH is
the dangerous direction: a file the app reads at runtime that is not in the image fails in
production, and only there. Nothing else would catch it, because every test runs against
the working tree, so these tests read the ignore file the way Docker does and check:

  * nothing the container reads is ignored: the app, its YAML and fixtures, the eval cases,
    every cron entrypoint and every file a Railway start command names, the lock files;
  * what it exists to keep out IS ignored, so an edit that empties it fails too;
  * nothing the app imports lives in the one directory it leaves out, `origin/tests`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
IGNORE_FILE = ROOT / ".dockerignore"
TESTS_DIR = "origin/tests"


def _regex(pattern: str) -> re.Pattern[str]:
    """One `.dockerignore` pattern as a regex, following the rules Docker uses: `*` and `?`
    do not cross a `/`, `**` does, and `**/` also matches no directory at all."""
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern[i + 1 : i + 2] == "*":
                while pattern[i + 1 : i + 2] == "*":
                    i += 1
                if i + 1 == len(pattern):
                    out += ".*"
                elif pattern[i + 1] == "/":
                    out += "(.*/)?"
                    i += 1
                else:
                    out += ".*"
            else:
                out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    return re.compile(f"^{out}$")


def load_patterns(text: str) -> list[tuple[bool, re.Pattern[str]]]:
    """`[(is_exclusion, regex)]` in file order. Patterns are relative to the context root
    whether or not they start with `/`."""
    patterns = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        patterns.append((not negated, _regex(line.lstrip("!").lstrip("/").rstrip("/"))))
    return patterns


def is_ignored(path: str, patterns) -> bool:
    """Docker's rule: the LAST pattern that matches the path, or any directory above it,
    decides. A matching directory takes everything under it with it."""
    parts = path.strip("/").split("/")
    ignored = False
    for is_exclusion, regex in patterns:
        if any(regex.match("/".join(parts[: n + 1])) for n in range(len(parts))):
            ignored = is_exclusion
    return ignored


def _walk(*roots: str):
    for root in roots:
        for p in sorted((ROOT / root).rglob("*")):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_file() and "__pycache__" not in rel and not rel.startswith(f"{TESTS_DIR}/"):
                yield rel


class TestTheMatcherFollowsDocker(SimpleTestCase):
    def check(self, patterns_text, path):
        return is_ignored(path, load_patterns(patterns_text))

    def test_double_star_crosses_directories_and_matches_none(self):
        text = "**/__pycache__"
        self.assertTrue(self.check(text, "__pycache__/a.pyc"))
        self.assertTrue(self.check(text, "origin/views/__pycache__/a.pyc"))
        self.assertFalse(self.check(text, "origin/views/a.py"))

    def test_a_single_star_stays_inside_one_directory(self):
        self.assertTrue(self.check("*.pyc", "a.pyc"))
        self.assertFalse(self.check("*.pyc", "origin/a.pyc"), "no `**/`: root only, as in Docker")
        self.assertTrue(self.check(".env.*", ".env.local"))
        self.assertFalse(self.check(".env.*", ".env"))

    def test_a_leading_slash_anchors_to_the_root_and_a_directory_takes_its_contents(self):
        self.assertTrue(self.check("/uploads", "uploads/a/b.png"))
        self.assertFalse(self.check("/uploads", "origin/uploads/a.png"))
        self.assertFalse(self.check("uploads_old", "uploads/a.png"))

    def test_the_last_matching_pattern_wins_and_a_bang_brings_a_file_back(self):
        text = "docs\n!docs/keep.md"
        self.assertTrue(self.check(text, "docs/other.md"))
        self.assertFalse(self.check(text, "docs/keep.md"))
        self.assertTrue(self.check("!docs/keep.md\ndocs", "docs/keep.md"))

    def test_comments_and_blank_lines_are_not_patterns(self):
        self.assertEqual(load_patterns("# a comment\n\n   \n"), [])


class TestWhatTheContainerReadsIsNotIgnored(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.patterns = load_patterns(IGNORE_FILE.read_text())

    def ignored(self, rel):
        return is_ignored(rel, self.patterns)

    def test_the_file_exists(self):
        self.assertTrue(IGNORE_FILE.is_file())
        self.assertTrue(self.patterns)

    def test_no_file_of_the_app_is_ignored(self):
        """`apis/` and `origin/`: the code, `apis/*.yaml`, `origin/fixtures/`, the eval cases
        and baselines, the migrations. Only `origin/tests` may go."""
        hit = [rel for rel in _walk("apis", "origin") if self.ignored(rel)]
        self.assertEqual(hit, [], "the app reads these at runtime, and `.dockerignore` lists them")

    def test_the_cron_entrypoints_and_railway_config_are_not_ignored(self):
        """The cron services run `/app/railway-*-entrypoint.sh` from this same image."""
        names = {p.name for p in ROOT.glob("railway*") if p.is_file()}
        names |= {"manage.py", "Pipfile", "Pipfile.lock", "requirements-dev.txt"}
        self.assertTrue(any(n.endswith("-entrypoint.sh") for n in names))
        self.assertEqual([n for n in sorted(names) if self.ignored(n)], [])
        railway_dir = [
            p.relative_to(ROOT).as_posix() for p in (ROOT / ".railway").rglob("*") if p.is_file()
        ]
        self.assertEqual([p for p in railway_dir if self.ignored(p)], [])

    def test_every_file_a_railway_start_command_names_exists_and_is_not_ignored(self):
        named = set()
        for toml in ROOT.glob("railway*.toml"):
            for line in toml.read_text().splitlines():
                if line.strip().startswith("startCommand"):
                    named |= set(re.findall(r"/app/([A-Za-z0-9_./-]+)", line))
        self.assertTrue(named, "no start command found: this test would pass for nothing")
        for rel in sorted(named):
            self.assertTrue((ROOT / rel).exists(), f"{rel} is named by a start command but missing")
            self.assertFalse(self.ignored(rel), f"{rel} is named by a start command but ignored")

    def test_what_the_dockerfile_and_the_dev_image_copy_is_not_ignored(self):
        copied = set()
        for line in (ROOT / "Dockerfile").read_text().splitlines():
            m = re.match(r"COPY\s+(.+?)\s+\./?$", line.strip())
            if m and m.group(1) != ".":
                copied |= set(m.group(1).split())
        self.assertEqual(copied, {"Pipfile", "Pipfile.lock"})
        # genos-platform's dev image uses this repo as its build context and copies these.
        for rel in sorted(copied | {"requirements-dev.txt"}):
            self.assertFalse(self.ignored(rel), rel)

    def test_nothing_the_app_imports_lives_in_the_tests(self):
        """`origin/tests` is the one directory left out, so nothing may import it at runtime."""
        offenders = []
        for rel in _walk("apis", "origin"):
            if not rel.endswith(".py"):
                continue
            for node in ast.walk(ast.parse((ROOT / rel).read_text(), filename=rel)):
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""] + [
                        f"{node.module or ''}.{a.name}" for a in node.names
                    ]
                else:
                    continue
                if any(re.match(r"(origin\.)?tests(\.|$)", m) for m in modules):
                    offenders.append(f"{rel}:{node.lineno}")
            text = (ROOT / rel).read_text()
            if re.search(r"""import_module\(\s*["'](origin\.)?tests""", text):
                offenders.append(rel)
        self.assertEqual(offenders, [])


class TestWhatItExistsToKeepOutIsIgnored(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.patterns = load_patterns(IGNORE_FILE.read_text())

    def test_the_things_it_is_for(self):
        for rel in (
            ".git/config",
            ".github/workflows/ci.yml",
            ".gitignore",
            ".env",
            ".env.local",
            ".venv/bin/python",
            "venv/bin/python",
            "uploads/team/avatar.png",
            "staticfiles/admin/css/base.css",
            "docs/setup.sh",
            "README.md",
            "ruff.toml",
            ".coveragerc",
            "origin/tests/test_anything.py",
            "origin/tests/golden/changes_response.json",
            "origin/views/__pycache__/x.cpython-311.pyc",
            "origin/x.pyc",
            "nested/dir/.DS_Store",
        ):
            self.assertTrue(is_ignored(rel, self.patterns), f"{rel} should not ship")

    def test_a_directory_that_only_looks_similar_is_not_caught(self):
        for rel in ("origin/uploads_helpers.py", "origin/services/docs.py", "origin/models/env.py"):
            self.assertFalse(is_ignored(rel, self.patterns), rel)
