"""MIP-1649: a new transactional test must use `CheapFlushTransactionTestCase`.

Django's own `TransactionTestCase` empties the database after EVERY test with a `TRUNCATE` of
all ~129 tables plus `post_migrate` (about 34 MB of disk writes per test on a disk-backed
Postgres). MIP-1643 moved every transactional class of the suite to
`origin/tests/transaction_support.py::CheapFlushTransactionTestCase` (-97 % bytes). Nothing else
stops the next `class Foo(TransactionTestCase)` from paying the old cost again, so this module
scans `origin/tests/` and fails when a class reaches Django's transactional base without going
through the cheap one.

It is a STATIC scan (`ast`, no import of the scanned modules, no database), so it also runs when a
test module would not import (a file that does not even parse fails it with the parser's error).
It follows what a class inherits from:

  * `TransactionTestCase` / `LiveServerTestCase` / `StaticLiveServerTestCase` / DRF's
    `APILiveServerTestCase` (a live server needs real commits, so these are transactional too;
    the repo has none today, they are covered so the first one is caught), however they are
    imported: `from django.test import X as Y`, `import django.test as t`, `import django.test`,
    `from django import test`, `from django.test import *`, a module-level `Alias = X`;
  * an intermediate base class, in the same module or another one of the repo (relative imports
    and re-exports followed): the subclass is fine only if that base itself reaches
    `CheapFlushTransactionTestCase`;
  * `TestCase` (rolls back, no flush) and `SimpleTestCase` are NOT transactional here, though
    Django's `TestCase` derives from `TransactionTestCase` at runtime: the roots are matched by
    name, never derived.

Opt-out for a class that really needs Django's own full flush: a class attribute carrying a reason,
`full_flush_reason = "why this test cannot use the cheap flush"` (see the docstring of
`transaction_support.py`). A subclass inherits it, like any attribute. An empty or non-string
reason is a failure of its own.

What it cannot see: a base built dynamically (`type(...)`, a factory function, `getattr`), a base
class from a third-party package other than the roots listed in TRANSACTIONAL_ROOTS (a test below
fails when the installed Django or DRF has a transactional class that is missing there), and which
of two same-named imports or assignments in one module is the live one (names are resolved per
module, not per scope or flow).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = ROOT / "origin" / "tests"

OPT_OUT_ATTRIBUTE = "full_flush_reason"
SANCTIONED_BASE = "origin.tests.transaction_support.CheapFlushTransactionTestCase"
SANCTIONED_CLASS = tuple(SANCTIONED_BASE.rsplit(".", 1))
TRANSACTION_SUPPORT_PATH = "origin/tests/transaction_support.py"

# Classes whose teardown is a real flush (they commit for real). Matched by fully qualified name
# after import aliases are resolved.
TRANSACTIONAL_ROOTS = frozenset(
    {
        "django.test.TransactionTestCase",
        "django.test.testcases.TransactionTestCase",
        "django.test.LiveServerTestCase",
        "django.test.testcases.LiveServerTestCase",
        "django.test.selenium.SeleniumTestCase",
        "django.contrib.staticfiles.testing.StaticLiveServerTestCase",
        "django.contrib.admin.tests.AdminSeleniumTestCase",
        "rest_framework.test.APITransactionTestCase",
        "rest_framework.test.APILiveServerTestCase",
        "channels.testing.ChannelsLiveServerTestCase",
    }
)
# Only the repo's own packages are read from disk to follow a base class; anything else is a
# third-party module (never a candidate for a stray file of the same name).
_REPO_PACKAGES = ("origin", "apis")


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    cls: str
    problem: str

    def format(self):
        return (
            f"{self.path}:{self.line}: class {self.cls}: {self.problem}\n"
            f"    Subclass CheapFlushTransactionTestCase from {TRANSACTION_SUPPORT_PATH}. If this "
            f"test really needs Django's own full flush, set\n"
            f'    `{OPT_OUT_ATTRIBUTE} = "<why>"` on the class (documented in the docstring of '
            f"{TRANSACTION_SUPPORT_PATH})."
        )


@dataclass
class ScanResult:
    files: int = 0
    classes: int = 0
    compliant_transactional: list = field(default_factory=list)  # [(path, class name)]
    opted_out: list = field(default_factory=list)
    violations: list = field(default_factory=list)


@dataclass(frozen=True)
class _Verdict:
    transactional: bool = False
    cheap: bool = False
    opted_out: bool = False
    chain: tuple = ()  # how the class reaches a transactional root, for the message


def _dotted(node):
    """`a.b.c` for a Name / Attribute / Subscript chain, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else None
    if isinstance(node, ast.Subscript):
        return _dotted(node.value)
    return None


def _module_level(body):
    """Statements of a module body, looking inside if/try/with (conditional imports)."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.If):
            yield from _module_level(stmt.body + stmt.orelse)
        elif isinstance(stmt, ast.Try):
            yield from _module_level(
                stmt.body
                + stmt.orelse
                + stmt.finalbody
                + [s for h in stmt.handlers for s in h.body]
            )
        elif isinstance(stmt, ast.With):
            yield from _module_level(stmt.body)


class _Module:
    def __init__(self, name, source, path, is_package):
        self.name = name
        self.path = path
        self.tree = ast.parse(source, filename=path)
        self.imports = {}  # local name -> fully qualified name
        self.assigns = {}  # module-level `Alias = a.b.C` -> "a.b.C"
        self.stars = []  # modules imported with `*`
        self.classes = {}  # simple name -> [ClassDef]
        package = name if is_package else name.rpartition(".")[0]
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.imports[alias.asname] = alias.name
                    else:  # `import a.b.c` binds `a`
                        self.imports[alias.name.partition(".")[0]] = alias.name.partition(".")[0]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".") if package else []
                    parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                    base = ".".join(parts + ([node.module] if node.module else []))
                for alias in node.names:
                    if alias.name == "*":
                        self.stars.append(base)
                    else:
                        self.imports[alias.asname or alias.name] = f"{base}.{alias.name}"
            elif isinstance(node, ast.ClassDef):
                self.classes.setdefault(node.name, []).append(node)
        for stmt in _module_level(self.tree.body):
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                value = _dotted(stmt.value) if stmt.value is not None else None
                if value:
                    for target in targets:
                        if isinstance(target, ast.Name):
                            self.assigns[target.id] = value


class Scanner:
    """Scans `sources` ({dotted module name: source}); a base defined elsewhere in the repo is read
    from `root` when it is given. Synthetic sources never touch the disk."""

    def __init__(self, sources, root=None, paths=None):
        self._sources = dict(sources)
        self._paths = paths or {}
        self._root = root
        self._modules = {}
        self._verdicts = {}

    # -- modules ---------------------------------------------------------------------------
    def _module(self, name):
        if name in self._modules:
            return self._modules[name]
        module = None
        if name in self._sources:
            path = self._paths.get(name, f"{name}.py")
            module = _Module(name, self._sources[name], path, path.endswith("__init__.py"))
        elif self._root is not None and name.partition(".")[0] in _REPO_PACKAGES:
            base = Path(self._root, *name.split("."))
            for candidate, is_package in (
                (base.with_suffix(".py"), False),
                (base / "__init__.py", True),
            ):
                if candidate.is_file():
                    module = _Module(name, candidate.read_text(), str(candidate), is_package)
                    break
        self._modules[name] = module
        return module

    # -- name resolution -------------------------------------------------------------------
    def _resolve(self, module, dotted, seen=()):
        """What `dotted` (as written in `module`) is: ("root", fqn), ("cheap",), ("classes",
        module, [ClassDef]) or None when it is not something this scan can follow."""
        key = (module.name, dotted)
        if key in seen:
            return None
        seen += (key,)
        head, _, rest = dotted.partition(".")
        tail = f".{rest}" if rest else ""
        if not rest and head in module.classes:
            return ("classes", module, module.classes[head])
        if head in module.imports:
            return self._resolve_fqn(module.imports[head] + tail, seen)
        if head in module.assigns:
            return self._resolve(module, module.assigns[head] + tail, seen)
        for star in module.stars:
            found = self._resolve_fqn(f"{star}.{dotted}", seen)
            if found:
                return found
        return None

    def _resolve_fqn(self, fqn, seen):
        if fqn in TRANSACTIONAL_ROOTS:
            return ("root", fqn)
        if fqn == SANCTIONED_BASE:
            return ("cheap",)
        parts = fqn.split(".")
        for i in range(len(parts) - 1, 0, -1):
            module = self._module(".".join(parts[:i]))
            if module is not None:
                return self._resolve(module, ".".join(parts[i:]), seen)
        return None

    # -- verdicts --------------------------------------------------------------------------
    @staticmethod
    def _opt_out(cdef):
        """None (no opt-out), True (valid: a non-blank string literal) or False (no reason)."""
        state = None
        for stmt in cdef.body:
            if isinstance(stmt, ast.Assign):
                targets, value = stmt.targets, stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                targets, value = [stmt.target], stmt.value
            else:
                continue
            if any(isinstance(t, ast.Name) and t.id == OPT_OUT_ATTRIBUTE for t in targets):
                ok = isinstance(value, ast.Constant) and isinstance(value.value, str)
                state = ok and bool(value.value.strip())
        return state

    def _verdict(self, module, cdef):
        key = (module.name, id(cdef))
        if key in self._verdicts:
            return self._verdicts[key]
        self._verdicts[key] = _Verdict()  # a cycle (invalid code) ends here
        if (module.name, cdef.name) == SANCTIONED_CLASS:
            verdict = _Verdict(transactional=True, cheap=True)
        else:
            transactional = cheap = False
            opted = self._opt_out(cdef) is True
            chain = ()
            for base in cdef.bases:
                written = _dotted(base)
                found = self._resolve(module, written) if written else None
                if found is None:
                    continue
                if found[0] == "root":
                    transactional = True
                    chain = chain or (f"{written} = {found[1]}",)
                elif found[0] == "cheap":
                    transactional = cheap = True
                else:
                    for sub in found[2]:
                        v = self._verdict(found[1], sub)
                        transactional |= v.transactional
                        cheap |= v.cheap
                        opted |= v.opted_out
                        if v.transactional and not v.cheap:
                            chain = chain or (sub.name, *v.chain)
            verdict = _Verdict(transactional, cheap, opted, chain)
        self._verdicts[key] = verdict
        return verdict

    # -- the scan --------------------------------------------------------------------------
    def scan(self, only=None):
        result = ScanResult()
        for name in sorted(only if only is not None else self._sources):
            module = self._module(name)
            result.files += 1
            for cdef in sorted(
                (c for defs in module.classes.values() for c in defs), key=lambda c: c.lineno
            ):
                result.classes += 1
                verdict = self._verdict(module, cdef)
                if self._opt_out(cdef) is False:
                    result.violations.append(
                        Violation(
                            module.path,
                            cdef.lineno,
                            cdef.name,
                            f"`{OPT_OUT_ATTRIBUTE}` needs a reason: a non-empty string literal "
                            f"saying why Django's full flush is required",
                        )
                    )
                if not verdict.transactional or (module.name, cdef.name) == SANCTIONED_CLASS:
                    continue  # the sanctioned class is the definition, not a user of it
                if verdict.cheap:
                    result.compliant_transactional.append((module.path, cdef.name))
                elif verdict.opted_out:
                    result.opted_out.append((module.path, cdef.name))
                else:
                    how = (
                        f"inherits Django's transactional base directly ({verdict.chain[0]})"
                        if len(verdict.chain) == 1
                        else f"is transactional through {' -> '.join(verdict.chain)}, which does "
                        f"not derive from CheapFlushTransactionTestCase"
                    )
                    result.violations.append(Violation(module.path, cdef.lineno, cdef.name, how))
        return result


@lru_cache(maxsize=1)
def scan_tests_tree():
    sources, paths = {}, {}
    for path in sorted(TESTS_DIR.rglob("*.py")):
        rel = path.relative_to(ROOT)
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        sources[".".join(parts)] = path.read_text()
        paths[".".join(parts)] = rel.as_posix()
    return Scanner(sources, root=ROOT, paths=paths).scan()


# ---------------------------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------------------------


class TransactionalTestsUseTheCheapFlush(SimpleTestCase):
    def test_every_transactional_class_under_origin_tests_uses_cheap_flush_or_opts_out(self):
        result = scan_tests_tree()

        self.assertFalse(
            result.violations,
            f"\n{len(result.violations)} test class(es) would pay Django's full flush (a TRUNCATE "
            f"of every table after each test, ~34 MB of disk writes per test):\n\n"
            + "\n".join(v.format() for v in result.violations),
        )

    def test_the_scan_is_not_vacuous(self):
        """A resolver that follows nothing would 'pass': it must find the real classes."""
        result = scan_tests_tree()

        self.assertGreater(result.files, 100)
        found = set(result.compliant_transactional)
        # a standalone class, a base, and a subclass in ANOTHER module reached through the base
        self.assertIn(
            ("origin/tests/test_member_events.py", "TestConcurrency"), found, "direct subclass"
        )
        self.assertGreater(
            result.classes, len(found), "TestCase classes are scanned and not flagged"
        )
        self.assertIn(
            ("origin/tests/test_agent_decide_edits.py", "DecideEditBase"), found, "base class"
        )
        self.assertIn(
            ("origin/tests/test_agent_undo_capture.py", "TheModelSeesWhatItSawBefore"),
            found,
            "compliant through CaptureBase -> DecideEditBase (a relative import of another module)",
        )

    def test_the_root_list_covers_every_transactional_class_of_django_and_drf(self):
        """An upgrade that adds a transactional test base must not slip past the name match."""
        import importlib

        from django.test import TestCase, TransactionTestCase

        missing = set()
        for module_name in (
            "django.test.testcases",
            "django.test.selenium",
            "django.contrib.staticfiles.testing",
            "django.contrib.admin.tests",
            "rest_framework.test",
        ):
            module = importlib.import_module(module_name)
            for name, obj in vars(module).items():
                if (
                    isinstance(obj, type)
                    and obj.__module__ == module_name
                    and issubclass(obj, TransactionTestCase)
                    and not issubclass(obj, TestCase)
                    and f"{module_name}.{name}" not in TRANSACTIONAL_ROOTS
                ):
                    missing.add(f"{module_name}.{name}")
        self.assertEqual(missing, set(), "add these to TRANSACTIONAL_ROOTS")

    def test_the_sanctioned_base_and_the_docs_name_the_same_things(self):
        text = (ROOT / TRANSACTION_SUPPORT_PATH).read_text()
        self.assertIn("class CheapFlushTransactionTestCase(", text)
        # the opt-out and this guard are documented where a person who hits the failure looks
        self.assertIn(OPT_OUT_ATTRIBUTE, ast.get_docstring(ast.parse(text)))
        self.assertIn("test_transactional_test_guard", ast.get_docstring(ast.parse(text)))


# ---------------------------------------------------------------------------------------------
# The scanner on synthetic sources (nothing here is a test class of the suite)
# ---------------------------------------------------------------------------------------------


def scan(sources, only=None):
    if isinstance(sources, str):
        sources = {"origin.tests.test_fake": sources}
    return Scanner(sources).scan(only)


class ScannerFlagsDjangoTransactionBases(SimpleTestCase):
    def assertFlags(self, source, cls, *fragments):
        result = scan(source)
        self.assertEqual([v.cls for v in result.violations], [cls], result.violations)
        text = result.violations[0].format()
        for fragment in ("test_fake.py", f"class {cls}", TRANSACTION_SUPPORT_PATH, *fragments):
            self.assertIn(fragment, text)

    def test_direct_subclass_of_django_transaction_test_case(self):
        self.assertFlags(
            "from django.test import TransactionTestCase\n"
            "class Bad(TransactionTestCase):\n    pass\n",
            "Bad",
            "django.test.TransactionTestCase",
        )

    def test_import_as_alias(self):
        self.assertFlags(
            "from django.test import TransactionTestCase as TTC\nclass Bad(TTC):\n    pass\n",
            "Bad",
            "TTC = django.test.TransactionTestCase",
        )

    def test_module_alias_import_as(self):
        self.assertFlags(
            "import django.test as t\nclass Bad(t.TransactionTestCase):\n    pass\n",
            "Bad",
            "t.TransactionTestCase = django.test.TransactionTestCase",
        )

    def test_full_dotted_path_through_import_django_test(self):
        self.assertFlags(
            "import django.test\nclass Bad(django.test.TransactionTestCase):\n    pass\n", "Bad"
        )

    def test_import_django_then_dotted_path(self):
        self.assertFlags(
            "import django\nclass Bad(django.test.TransactionTestCase):\n    pass\n", "Bad"
        )

    def test_from_django_import_test(self):
        self.assertFlags(
            "from django import test as dt\nclass Bad(dt.TransactionTestCase):\n    pass\n", "Bad"
        )

    def test_the_testcases_module_path_and_star_import(self):
        self.assertFlags(
            "from django.test.testcases import TransactionTestCase\n"
            "class Bad(TransactionTestCase):\n    pass\n",
            "Bad",
        )
        self.assertFlags(
            "from django.test import *\nclass Bad(TransactionTestCase):\n    pass\n", "Bad"
        )

    def test_a_module_level_alias_of_the_base(self):
        self.assertFlags(
            "from django.test import TransactionTestCase\n"
            "Base = TransactionTestCase\n"
            "class Bad(Base):\n    pass\n",
            "Bad",
        )

    def test_live_server_and_drf_transaction_cases_are_transactional_too(self):
        for imp, name in (
            ("from django.test import LiveServerTestCase", "LiveServerTestCase"),
            (
                "from django.contrib.staticfiles.testing import StaticLiveServerTestCase",
                "StaticLiveServerTestCase",
            ),
            ("from rest_framework.test import APILiveServerTestCase", "APILiveServerTestCase"),
            ("from rest_framework.test import APITransactionTestCase", "APITransactionTestCase"),
        ):
            with self.subTest(name):
                self.assertFlags(f"{imp}\nclass Bad({name}):\n    pass\n", "Bad", name)

    def test_one_of_several_bases_is_enough(self):
        self.assertFlags(
            "from django.test import TransactionTestCase\n"
            "class Mixin:\n    pass\n"
            "class Bad(Mixin, TransactionTestCase):\n    pass\n",
            "Bad",
        )

    def test_a_nested_class_is_found(self):
        self.assertFlags(
            "from django.test import TransactionTestCase\n"
            "def make():\n"
            "    class Bad(TransactionTestCase):\n        pass\n"
            "    return Bad\n",
            "Bad",
        )

    def test_a_call_among_the_bases_is_skipped_and_the_other_bases_still_count(self):
        self.assertFlags(
            "from django.test import TransactionTestCase\n"
            "class Bad(make_base(), TransactionTestCase, metaclass=type):\n    pass\n",
            "Bad",
        )
        # a base the scan cannot see is the documented hole, not a crash
        result = scan("class Odd(make_base(), metaclass=type):\n    pass\n")
        self.assertEqual(result.violations, [])


class ScannerFollowsIntermediateBases(SimpleTestCase):
    def test_an_intermediate_base_that_is_not_cheap_is_the_offender_and_so_are_its_subclasses(self):
        result = scan(
            "from django.test import TransactionTestCase\n"
            "class Base(TransactionTestCase):\n    pass\n"
            "class Child(Base):\n    pass\n"
            "class GrandChild(Child):\n    pass\n"
        )

        self.assertEqual([v.cls for v in result.violations], ["Base", "Child", "GrandChild"])
        grand = result.violations[2].format()
        self.assertIn("transactional through Child -> Base -> TransactionTestCase", grand)
        self.assertIn("does not derive from CheapFlushTransactionTestCase", grand)

    def test_a_base_in_another_module_is_followed_through_absolute_relative_and_reexports(self):
        sources = {
            "origin.tests.base_mod": (
                "from django.test import TransactionTestCase as T\nclass RawBase(T):\n    pass\n"
            ),
            "origin.tests.reexport": "from origin.tests.base_mod import RawBase as Again\n",
            "origin.tests.test_abs": (
                "from origin.tests.base_mod import RawBase\nclass A(RawBase):\n    pass\n"
            ),
            "origin.tests.test_rel": "from .base_mod import RawBase\nclass B(RawBase):\n    pass\n",
            "origin.tests.test_pkg": (
                "from origin.tests import base_mod\nclass C(base_mod.RawBase):\n    pass\n"
            ),
            "origin.tests.test_re": "from .reexport import Again\nclass D(Again):\n    pass\n",
        }

        result = scan(sources)

        self.assertEqual(sorted(v.cls for v in result.violations), ["A", "B", "C", "D", "RawBase"])

    def test_a_relative_import_inside_a_package_init_is_resolved_from_that_package(self):
        sources = {
            "origin.tests.base_mod": (
                "from django.test import TransactionTestCase\nclass RawBase(TransactionTestCase):\n"
                "    pass\n"
            ),
            "origin.tests": "from .base_mod import RawBase\n",
            "origin.tests.test_x": "from origin.tests import RawBase\nclass X(RawBase):\n    pass\n",
        }

        result = Scanner(sources, paths={"origin.tests": "origin/tests/__init__.py"}).scan()

        self.assertEqual(sorted(v.cls for v in result.violations), ["RawBase", "X"])

    def test_an_intermediate_base_derived_from_cheap_is_fine_in_every_import_style(self):
        sources = {
            "origin.tests.base_mod": (
                "from origin.tests.transaction_support import CheapFlushTransactionTestCase\n"
                "class GoodBase(CheapFlushTransactionTestCase):\n    pass\n"
            ),
            "origin.tests.test_a": (
                "from origin.tests.base_mod import GoodBase\nclass A(GoodBase):\n    pass\n"
            ),
            "origin.tests.test_b": (
                "from origin.tests import transaction_support as ts\n"
                "class B(ts.CheapFlushTransactionTestCase):\n    pass\n"
            ),
            "origin.tests.test_c": (
                "from .transaction_support import CheapFlushTransactionTestCase as C0\n"
                "class C(C0):\n    pass\n"
            ),
        }

        result = scan(sources)

        self.assertEqual(result.violations, [])
        self.assertEqual(
            sorted(n for _, n in result.compliant_transactional), ["A", "B", "C", "GoodBase"]
        )

    def test_a_cheap_base_wins_even_beside_a_raw_one(self):
        result = scan(
            "from django.test import TransactionTestCase\n"
            "from origin.tests.transaction_support import CheapFlushTransactionTestCase as C\n"
            "class Ok(C, TransactionTestCase):\n    pass\n"
        )
        self.assertEqual(result.violations, [])

    def test_test_case_and_simple_test_case_subclasses_do_not_trigger(self):
        result = scan(
            "import unittest\n"
            "from django.test import TestCase, SimpleTestCase\n"
            "from rest_framework.test import APITestCase\n"
            "class A(TestCase):\n    pass\n"
            "class B(SimpleTestCase):\n    pass\n"
            "class C(APITestCase):\n    pass\n"
            "class D(unittest.TestCase):\n    pass\n"
            "class E(A):\n    pass\n"
            "class F:\n    pass\n"
        )

        self.assertEqual(result.violations, [])
        self.assertEqual(result.compliant_transactional, [])
        self.assertEqual(result.classes, 6)

    def test_a_cycle_in_invalid_code_does_not_hang(self):
        result = scan("class A(B):\n    pass\nclass B(A):\n    pass\n")
        self.assertEqual(result.violations, [])

    def test_the_sanctioned_class_itself_is_the_only_exemption_in_transaction_support(self):
        sources = {
            "origin.tests.transaction_support": (
                "from django.test import TransactionTestCase\n"
                "class CheapFlushTransactionTestCase(TransactionTestCase):\n    pass\n"
                "class Sneaky(TransactionTestCase):\n    pass\n"
            )
        }
        result = scan(sources)
        self.assertEqual([v.cls for v in result.violations], ["Sneaky"])


class ScannerOptOut(SimpleTestCase):
    RAW = "from django.test import TransactionTestCase\n"

    def test_an_opt_out_with_a_reason_passes_and_is_listed(self):
        result = scan(
            self.RAW + "class Needs(TransactionTestCase):\n"
            f'    {OPT_OUT_ATTRIBUTE} = "asserts the post_migrate rows are rebuilt"\n'
        )

        self.assertEqual(result.violations, [])
        self.assertEqual([n for _, n in result.opted_out], ["Needs"])

    def test_the_opt_out_is_inherited_by_subclasses_like_any_attribute(self):
        result = scan(
            self.RAW + "class Needs(TransactionTestCase):\n"
            f'    {OPT_OUT_ATTRIBUTE} = "needs the full flush"\n'
            "class Sub(Needs):\n    pass\n"
        )
        self.assertEqual(result.violations, [])
        self.assertEqual([n for _, n in result.opted_out], ["Needs", "Sub"])

    def test_the_opt_out_does_not_cover_a_sibling_class(self):
        result = scan(
            self.RAW + "class Needs(TransactionTestCase):\n"
            f'    {OPT_OUT_ATTRIBUTE} = "needs the full flush"\n'
            "class Other(TransactionTestCase):\n    pass\n"
        )
        self.assertEqual([v.cls for v in result.violations], ["Other"])

    def test_an_opt_out_without_a_usable_reason_fails_and_says_a_reason_is_needed(self):
        for label, value in (
            ("empty", '""'),
            ("blank", '"   "'),
            ("true", "True"),
            ("none", "None"),
            ("a name", "WHY"),
            ("an f-string", 'f"{WHY}"'),
        ):
            with self.subTest(label):
                result = scan(
                    self.RAW + "WHY = 'x'\nclass Needs(TransactionTestCase):\n"
                    f"    {OPT_OUT_ATTRIBUTE} = {value}\n"
                )

                problems = [v.problem for v in result.violations]
                self.assertTrue(any("needs a reason" in p for p in problems), problems)
                # and it does not count as an opt-out: the class is flagged for the base too
                self.assertTrue(any("directly" in p for p in problems), problems)

    def test_an_annotated_opt_out_counts(self):
        result = scan(
            self.RAW + "class Needs(TransactionTestCase):\n"
            f'    {OPT_OUT_ATTRIBUTE}: str = "needs the full flush"\n'
        )
        self.assertEqual(result.violations, [])

    def test_no_opt_out_at_all_is_a_plain_violation(self):
        result = scan(self.RAW + "class Needs(TransactionTestCase):\n    pass\n")
        self.assertEqual(len(result.violations), 1)
        self.assertNotIn("needs a reason", result.violations[0].problem)
