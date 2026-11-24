"""Print every routed view as JSON lines, for the red-team walker (MIP-2267).

Run inside the api container:  python manage.py shell < scripts/redteam/dump_routes.py > routes.jsonl

Each line: {"route": "/api/v2/task/<int:task_id>/", "kwargs": {"task_id": "int"}, "view": "...",
"methods": ["GET", ...], "access": "checked|self_only|signed_in|public|?"}. This is what an attacker
learns from the public source; the walker substitutes another team's ids into the kwargs.
"""

import json
import re

from django.urls import URLPattern, URLResolver, get_resolver

try:
    from origin.tests.route_access import REGISTRY
except Exception:  # noqa: BLE001 - the registry is optional context only
    REGISTRY = {}

_CONVERTER = re.compile(r"<(?:(?P<conv>[^>:]+):)?(?P<name>[^>]+)>")
_REGEX_GROUP = re.compile(r"\(\?P<(?P<name>[^>]+)>(?P<body>[^)]*)\)")


def _pattern_text(pattern) -> str:
    return str(pattern.pattern)


def _normalise(text: str) -> tuple[str, dict]:
    kwargs: dict[str, str] = {}
    if text.startswith("^"):
        text = text[1:]
    text = text.rstrip("$").replace("\\Z", "").replace("\\", "")

    def repl_regex(m):
        body = m.group("body")
        kind = "int" if body in ("[0-9]+", "d+", "[0-9]{1,18}") else "str"
        if "a-f" in body or "uuid" in body.lower():
            kind = "uuid"
        kwargs[m.group("name")] = kind
        return "<%s:%s>" % (kind, m.group("name"))

    text = _REGEX_GROUP.sub(repl_regex, text)
    for m in _CONVERTER.finditer(text):
        kwargs.setdefault(m.group("name"), m.group("conv") or "str")
    return text, kwargs


def _walk(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + _pattern_text(p))
        elif isinstance(p, URLPattern):
            yield prefix + _pattern_text(p), p


def _view_info(p):
    cb = p.callback
    cls = getattr(cb, "view_class", None) or getattr(cb, "cls", None)
    actions = getattr(cb, "actions", None)
    if actions:
        methods = sorted(m.upper() for m in actions)
    elif cls is not None:
        methods = sorted(
            m.upper()
            for m in getattr(cls, "http_method_names", [])
            if hasattr(cls, m) and m not in ("options", "head")
        )
    else:
        methods = ["GET", "POST"]
    name = (
        f"{cls.__module__}.{cls.__name__}" if cls is not None else f"{cb.__module__}.{cb.__name__}"
    )
    return name, methods, cls


_PARAM = re.compile(
    r"""(?:query_params|GET|data|body|payload|params)(?:\.get|\.getlist)?\(\s*["']([a-zA-Z_][a-zA-Z0-9_]*)["']"""
    r"""|(?:query_params|GET|data|body|payload)\[\s*["']([a-zA-Z_][a-zA-Z0-9_]*)["']\s*\]"""
)


def _params(cls, cb):
    """The request parameter names the view's own source reads (what a reader of the public
    source learns): `request.query_params.get("task_id")`, `request.data["note_id"]`, ..."""
    import inspect

    target = cls or cb
    names = set()
    try:
        src = inspect.getsource(target)
    except (OSError, TypeError):
        return []
    for m in _PARAM.finditer(src):
        names.add(m.group(1) or m.group(2))
    return sorted(names)


for raw, p in _walk(get_resolver().url_patterns):
    route, kwargs = _normalise(raw)
    name, methods, cls = _view_info(p)
    params = _params(cls, p.callback)
    access = "?"
    if cls is not None and REGISTRY:
        entry = REGISTRY.get(cls.__name__) or REGISTRY.get(name)
        if entry is not None:
            access = type(entry).__name__.lower()
    print(
        json.dumps(
            {
                "route": "/" + route,
                "kwargs": kwargs,
                "view": name,
                "methods": methods,
                "access": access,
                "params": params,
            }
        )
    )
