"""Every routed API view declares its access check (MIP-716).

The 2026-08-02 manual audit marked all its rows closed and still missed
routes that let any signed-in user read or change another tenant's data
by id (MIP-711). A reviewer reading a diff cannot see the route that
*isn't* guarded, so this walks the URL resolver instead and fails on:

  * a routed DRF view with no entry in `route_access.REGISTRY` — the
    message names the class, so adding a route means saying, in the same
    PR, which kind of check it has;
  * a registry entry for a class that is no longer routed (a stale entry
    would hide the next class to reuse the name);
  * a `public(...)` entry on a view that is not actually anonymous, or an
    anonymous view registered as anything else;
  * a `checked(...)` entry naming a helper that none of the view's
    handlers can reach — so the declaration cannot drift from the code —
    or a handler of it that reaches none and never refers to the caller
    (a `delete` added beside a guarded `get`);
  * a `self_only(...)` entry on a view whose handlers never refer to the
    caller at all, which is what a forgotten check looks like.

What it cannot prove is that a reachable helper is applied to the right
id. That is what the per-endpoint cross-tenant tests are for; this makes
sure the question gets asked for every route.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap
import types

from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.permissions import AllowAny

# Through `origin.views` first — see test_fail_closed_config for the
# circular import this avoids.
from origin.views.common.base_auth_api_view import APIView

from .route_access import REGISTRY, Checked, Public, SelfOnly, SignedIn

_HANDLERS = ("get", "post", "put", "patch", "delete")

# How the caller is spelled in a handler. A `self_only` view has to use one
# of these somewhere — in a filter, a comparison or an attribute read.
_CALLER = {"request.user", "request.user.id", "request.user.pk", "self.request.user"}

# Call depth followed out of a handler. Deep enough for handler → view
# helper → service → its private check (`request_connection` →
# `_manager_or_raise`), shallow enough that "reachable" still means
# "this view calls it", not "something in the codebase does".
_DEPTH = 4


def _walk(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p.callback


def _routed_classes() -> dict[str, tuple[type, list[str]]]:
    out: dict[str, tuple[type, list[str]]] = {}
    for route, callback in _walk(get_resolver().url_patterns):
        cls = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
        if isinstance(cls, type) and issubclass(cls, APIView):
            key = f"{cls.__module__}.{cls.__qualname__}"
            out.setdefault(key, (cls, []))[1].append(route)
    return out


def _is_public(cls) -> bool:
    perms = list(cls.permission_classes)
    return not perms or AllowAny in perms


_parsed: dict = {}


def _tree(fn):
    if fn not in _parsed:
        try:
            _parsed[fn] = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        except (OSError, TypeError, SyntaxError):
            _parsed[fn] = None
    return _parsed[fn]


def _ours(obj) -> bool:
    return isinstance(obj, types.FunctionType) and obj.__module__.startswith("origin")


def _reach(fn, cls, depth, seen) -> tuple[set[str], bool]:
    """Names reachable from `fn`, and whether it refers to the caller.

    Follows plain calls to `origin` functions — module globals, local
    `from origin... import`, `module.func`, and `self.method` — which is
    how every view here reaches its checks. Names, not call sites: a
    helper passed as a value (`_openable_task_q` handed to `.filter`)
    counts the same as one called.
    """
    if fn in seen:
        return set(), False
    seen.add(fn)
    tree = _tree(fn)
    if tree is None:
        return set(), False
    names: set[str] = set()
    caller = False
    g = getattr(fn, "__globals__", {})
    nxt = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            names.add(n.id)
            if _ours(g.get(n.id)):
                nxt.append(g[n.id])
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
            if ast.unparse(n) in _CALLER:
                caller = True
            base = n.value
            if isinstance(base, ast.Name) and base.id in ("self", "cls"):
                if _ours(getattr(cls, n.attr, None)):
                    nxt.append(getattr(cls, n.attr))
            elif isinstance(base, ast.Name) and isinstance(g.get(base.id), types.ModuleType):
                if _ours(getattr(g[base.id], n.attr, None)):
                    nxt.append(getattr(g[base.id], n.attr))
        elif isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("origin"):
            mod = importlib.import_module(n.module)
            nxt.extend(getattr(mod, a.name) for a in n.names if _ours(getattr(mod, a.name, None)))
    if depth > 0:
        for f in nxt:
            more, c = _reach(f, cls, depth - 1, seen)
            names |= more
            caller = caller or c
    return names, caller


def _handlers(cls):
    return [h for h in _HANDLERS if _ours(getattr(cls, h, None))]


def _reach_view(cls) -> tuple[set[str], bool]:
    names: set[str] = set()
    caller = False
    for h in _handlers(cls):
        got, c = _reach(getattr(cls, h), cls, _DEPTH, set())
        names |= got
        caller = caller or c
    return names, caller


class TestEveryRouteDeclaresItsAccess(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.routed = _routed_classes()

    def test_every_routed_view_is_registered(self):
        missing = sorted(
            f"{key}  ({routes[0]})"
            for key, (_, routes) in self.routed.items()
            if key not in REGISTRY
        )
        self.assertEqual(
            missing,
            [],
            "Routed views with no access declaration. Add each to "
            "origin/tests/route_access.py as public(...), self_only(...) "
            "or checked(...) — see that file's docstring.",
        )

    def test_no_registry_entry_outlives_its_route(self):
        self.assertEqual(sorted(set(REGISTRY) - set(self.routed)), [])

    def test_public_entries_are_exactly_the_anonymous_views(self):
        wrong = []
        for key, (cls, _) in self.routed.items():
            entry = REGISTRY.get(key)
            if entry is None:
                continue
            if _is_public(cls) != isinstance(entry, Public):
                wrong.append(key)
        self.assertEqual(wrong, [])

    def test_every_entry_gives_a_reason_or_a_helper(self):
        empty = sorted(
            key
            for key, entry in REGISTRY.items()
            if (isinstance(entry, (Public, SignedIn, SelfOnly)) and not entry.reason.strip())
            or (isinstance(entry, Checked) and not entry.guards)
        )
        self.assertEqual(empty, [])

    def test_checked_helpers_are_reachable_from_the_view(self):
        unreached = []
        for key, (cls, _) in self.routed.items():
            entry = REGISTRY.get(key)
            if not isinstance(entry, Checked):
                continue
            names, _ = _reach_view(cls)
            unreached += [f"{key}: {g}" for g in entry.guards if g not in names]
        self.assertEqual(unreached, [])

    def test_every_handler_of_a_checked_view_is_guarded(self):
        """The view-level check above passes if `get` is guarded and a
        `delete` added later isn't. Per handler: reach a named helper, or
        at least act on the caller's own rows (`InboxItemView.get` lists
        the caller's own items; its `post`, which addresses someone else,
        is the handler that needs the helper)."""
        bare = []
        for key, (cls, _) in self.routed.items():
            entry = REGISTRY.get(key)
            if not isinstance(entry, Checked):
                continue
            for h in _handlers(cls):
                names, caller = _reach(getattr(cls, h), cls, _DEPTH, set())
                if not (set(entry.guards) & names) and not caller:
                    bare.append(f"{key}.{h}")
        self.assertEqual(bare, [])

    def test_self_only_views_refer_to_the_caller(self):
        blind = []
        for key, (cls, _) in self.routed.items():
            if isinstance(REGISTRY.get(key), SelfOnly) and not _reach_view(cls)[1]:
                blind.append(key)
        self.assertEqual(blind, [])
