"""No view reads the request body, or the query string, with `[...]` (MIP-1020).

`request.data["team_id"]` raises KeyError when the key is missing, and a KeyError nothing
catches is a 500. After MIP-1013 the body is always an object (or `{}`), but a missing or
partial body still reaches the read. 55 reads in `origin/views` were of this kind; a few could
raise (a reaction with only a `task_id`, an inbox request with no `item_body`), the rest sat
behind a presence check or inside an `except` that answered. All 55 now go through
`body_object` and the shared `missing_fields` / `require_fields`, or `.get`.

This walks every module under `origin/views` and fails on:

  * `request.data[...]` and `self.request.data[...]`;
  * the same through a name that was assigned from it (`data = request.data`,
    `data = request.data or {}`) in the same function;
  * `request.data.get("a")["b"]` (a TypeError on a missing `a`);
  * `request.GET[...]`, `request.POST[...]` and `request.query_params[...]`, which raise
    `MultiValueDictKeyError`, a KeyError, the same way.

Only a READ is looked at: `data["x"] = ...` is an assignment and cannot raise. What it does not
follow: a copy (`data = request.data.copy()`), and a value that came out of `body_object(request)`
(the dict-or-`{}` reader every view is meant to use, whose `body[...]` reads come after the
view's own presence check). Both are the view's to guard; the 20 reads on copies in the PUT
views are behind an `"x" in data` check.

A site that really is deliberate goes in `ALLOWED` with its reason, keyed by module,
`Class.method` and the text of the read, NOT its line, so a second read in the same method
still fails. Every entry must still match a site: a stale one fails, so the list only shrinks.
"""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

import origin.views

VIEWS_ROOT = Path(origin.views.__file__).parent

# (module relative to origin/views, "Class.method", source text of the read) -> why it stays.
ALLOWED: dict[tuple[str, str, str], str] = {}


def _is_request_attr(node: ast.AST, *names: str) -> bool:
    """`request.<name>` or `self.request.<name>`."""
    if not (isinstance(node, ast.Attribute) and node.attr in names):
        return False
    base = node.value
    if isinstance(base, ast.Name):
        return base.id == "request"
    return (
        isinstance(base, ast.Attribute)
        and base.attr == "request"
        and isinstance(base.value, ast.Name)
        and base.value.id == "self"
    )


def _is_request_data(node: ast.AST) -> bool:
    """`request.data`, or `request.data or ...` / `... or request.data`."""
    if _is_request_attr(node, "data"):
        return True
    if isinstance(node, ast.BoolOp):
        return any(_is_request_data(v) for v in node.values)
    return False


class _Reads(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.aliases: list[set[str]] = [set()]
        self.hits: list[tuple[str, str, int]] = []

    def _function(self, node) -> None:
        self.scope.append(node.name)
        self.aliases.append(set())
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Assign)
                and len(child.targets) == 1
                and isinstance(child.targets[0], ast.Name)
                and _is_request_data(child.value)
            ):
                self.aliases[-1].add(child.targets[0].id)
        self.generic_visit(node)
        self.aliases.pop()
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _is_data(self, node: ast.AST) -> bool:
        return _is_request_data(node) or (
            isinstance(node, ast.Name) and node.id in self.aliases[-1]
        )

    def visit_Subscript(self, node: ast.Subscript) -> None:
        value = node.value
        flagged = (
            self._is_data(value)
            or _is_request_attr(value, "GET", "POST", "query_params")
            or (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "get"
                and self._is_data(value.func.value)
            )
        )
        if flagged and isinstance(node.ctx, ast.Load):
            self.hits.append((".".join(self.scope), ast.unparse(node), node.lineno))
        self.generic_visit(node)


def find_reads(source: str) -> list[tuple[str, str, int]]:
    """`(Class.method, source text, line)` for every forbidden read in `source`."""
    visitor = _Reads()
    visitor.visit(ast.parse(source))
    return visitor.hits


def view_modules(root: Path = VIEWS_ROOT) -> list[Path]:
    """Every module the walk reads."""
    return sorted(root.rglob("*.py"))


def site_keys(root: Path = VIEWS_ROOT) -> dict[tuple[str, str, str], list[int]]:
    found: dict[tuple[str, str, str], list[int]] = {}
    for path in view_modules(root):
        rel = path.relative_to(root).as_posix()
        for qualname, text, line in find_reads(path.read_text()):
            found.setdefault((rel, qualname, text), []).append(line)
    return found


def unlisted(found, allowed):
    return {key: lines for key, lines in found.items() if key not in allowed}


def stale(found, allowed):
    return sorted(key for key in allowed if key not in found)


class NoRequestDataIndexTests(SimpleTestCase):
    def test_no_view_reads_the_request_with_an_index(self):
        found = unlisted(site_keys(), ALLOWED)
        self.assertEqual(
            found,
            {},
            "\nThese reads raise KeyError (a 500) on a missing field. Read the body with "
            "`body_object(request)` and ask `require_fields` / `missing_fields` first, or use "
            ".get(); if one is deliberate, add it to ALLOWED with the reason:\n"
            + "\n".join(
                f"  origin/views/{rel}:{lines} {qualname}: {text}"
                for (rel, qualname, text), lines in sorted(found.items())
            ),
        )

    def test_every_allowance_names_a_read_that_is_still_there_and_says_why(self):
        self.assertEqual(
            stale(site_keys(), ALLOWED), [], "remove these from ALLOWED: the read is gone"
        )
        for key, reason in ALLOWED.items():
            self.assertTrue(reason.strip(), f"{key} has no reason")

    def test_the_walk_reads_the_views_it_is_meant_to(self):
        names = {p.relative_to(VIEWS_ROOT).as_posix() for p in view_modules()}
        self.assertLessEqual(
            {
                "task/task_views.py",
                "project/prj_views.py",
                "common/inbox_views.py",
                "note/team_note_folder_views.py",
            },
            names,
        )
        self.assertGreater(len(names), 40)


class ScannerTests(SimpleTestCase):
    """The scanner finds what it says it does, and nothing it says it does not."""

    def reads(self, source):
        return [(q, text) for q, text, _ in find_reads(source)]

    def test_a_subscript_on_request_data_is_found(self):
        src = "class V:\n    def post(self, request):\n        return request.data['a']\n"
        self.assertEqual(self.reads(src), [("V.post", "request.data['a']")])

    def test_self_request_data_is_found(self):
        src = "class V:\n    def post(self):\n        return self.request.data['a']\n"
        self.assertEqual(self.reads(src), [("V.post", "self.request.data['a']")])

    def test_an_alias_is_followed_inside_its_own_function_only(self):
        src = (
            "def a(request):\n    data = request.data\n    return data['x']\n"
            "def b(request, data):\n    return data['x']\n"
        )
        self.assertEqual(self.reads(src), [("a", "data['x']")])

    def test_an_alias_through_or_is_followed(self):
        src = "def a(request):\n    data = request.data or {}\n    return data['x']\n"
        self.assertEqual(self.reads(src), [("a", "data['x']")])

    def test_a_nested_read_is_found_once_at_the_request(self):
        src = "def a(request):\n    return request.data['o']['k']\n"
        self.assertEqual(self.reads(src), [("a", "request.data['o']")])

    def test_a_get_then_an_index_is_found(self):
        src = "def a(request):\n    return request.data.get('o')['k']\n"
        self.assertEqual(self.reads(src), [("a", "request.data.get('o')['k']")])

    def test_the_query_string_is_held_to_the_same_rule(self):
        src = (
            "def a(request):\n    return request.GET['a'], request.POST['b'], "
            "request.query_params['c']\n"
        )
        self.assertEqual(len(self.reads(src)), 3)

    def test_a_read_in_a_nested_function_is_found(self):
        src = "def a(request):\n    def inner():\n        return request.data['x']\n"
        self.assertEqual(self.reads(src), [("a.inner", "request.data['x']")])

    def test_what_cannot_raise_is_left_alone(self):
        src = (
            "def a(request):\n"
            "    data = request.data\n"
            "    data['x'] = 1\n"
            "    del data['y']\n"
            "    request.data['z'] = 2\n"
            "    body = body_object(request)\n"
            "    copy = request.data.copy()\n"
            "    return request.data.get('a'), data.get('b'), 'k' in request.data, "
            "body['q'], copy['r'], request.query_params.get('c')\n"
        )
        self.assertEqual(self.reads(src), [])

    def test_an_allowance_is_keyed_by_the_text_not_the_line(self):
        src = "class V:\n    def post(self, request):\n        request.data['a']\n        request.data['b']\n"
        found = {}
        for qualname, text, line in find_reads(src):
            found.setdefault(("m.py", qualname, text), []).append(line)
        allowed = {("m.py", "V.post", "request.data['a']"): "deliberate"}
        self.assertEqual(list(unlisted(found, allowed)), [("m.py", "V.post", "request.data['b']")])

    def test_an_allowance_with_no_read_behind_it_is_stale(self):
        found = {("m.py", "V.post", "request.data['a']"): [3]}
        allowed = {
            ("m.py", "V.post", "request.data['a']"): "deliberate",
            ("m.py", "V.post", "request.data['gone']"): "deliberate",
        }
        self.assertEqual(stale(found, allowed), [("m.py", "V.post", "request.data['gone']")])
