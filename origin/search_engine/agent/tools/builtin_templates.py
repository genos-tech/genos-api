"""The app's built-in task templates, read from their one source file (MIP-1606).

`builtin_task_templates.json` (next to this module) is the only place the four templates
(`default`, `bug`, `spike`, `milestone`) are written down on the server. The browser keeps a
vendored copy of the same file and builds its BlockNote blocks from it
(`genos-frontend` `src/features/tasks/utils/builtinTaskTemplates.json`). Before this the server
typed the headings (`plan_task_bodies`) and the grey prompts (`template_prompts`) out by hand.

Shape: `{"templates": [{"id", "label", "sections": [{"heading", "items": [{"kind", "text"}]}]}]}`.
`label` is the English name the agent matches a `template` argument against. `kind` is
`placeholder` (a grey italic paragraph) or `bullet` (a grey italic bullet); both are prompts: text
the template shows and an author is meant to replace. The default template's headings and prompts also
carry translations (MIP-1894): a section's `heading_i18n` and an item's `i18n` map a locale code
(ja, zh, es, fr, hi, ar) to the text the app shows in that language, and the browser builds the
default body from them. The English `heading` / `text` stay the source and the agent's vocabulary;
every locale's prompt counts as a prompt (`PROMPT_TEXTS`), because a task quick-created in
Japanese is saved with the Japanese hints.

DRIFT: the two copies must stay equal. Each repo pins the value of `CANONICAL_SHA256` below (the same
value) in a test; that value is the hash of the file's parsed content, so formatting differences between the
repos' tooling do not count. Change the file: update both copies and both pins in the same change.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

SOURCE_PATH = Path(__file__).with_name("builtin_task_templates.json")

_KINDS = frozenset({"placeholder", "bullet"})


def canonical_sha256(data: Any) -> str:
    """The hash both repos pin: of the compact JSON of the parsed content, key order kept, so a
    reformat of the file (indent, line breaks) is not a change."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_i18n(tid: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) and v.strip() for k, v in value.items()
    ):
        raise ValueError(f"builtin_task_templates.json: {tid}: bad translations {value!r}")


def _validated(data: Any) -> list[dict[str, Any]]:
    """The template list, or a ValueError naming what is wrong: a bad file must stop the process
    at import, not hand the agent a half-read template."""
    templates = data.get("templates") if isinstance(data, dict) else None
    if not isinstance(templates, list) or not templates:
        raise ValueError("builtin_task_templates.json: `templates` must be a non-empty list")
    seen: set[str] = set()
    for tpl in templates:
        tid, label, sections = tpl.get("id"), tpl.get("label"), tpl.get("sections")
        if not isinstance(tid, str) or not tid.strip() or tid in seen:
            raise ValueError(f"builtin_task_templates.json: bad or repeated id {tid!r}")
        seen.add(tid)
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"builtin_task_templates.json: {tid}: `label` must be text")
        if not isinstance(sections, list) or not sections:
            raise ValueError(f"builtin_task_templates.json: {tid}: needs at least one section")
        for section in sections:
            heading = section.get("heading")
            if not isinstance(heading, str) or not heading.strip():
                raise ValueError(f"builtin_task_templates.json: {tid}: a section has no heading")
            for item in section.get("items", []):
                text = item.get("text")
                if item.get("kind") not in _KINDS or not isinstance(text, str) or not text.strip():
                    raise ValueError(f"builtin_task_templates.json: {tid}: bad item {item!r}")
                _check_i18n(tid, item.get("i18n"))
            _check_i18n(tid, section.get("heading_i18n"))
    return templates


with SOURCE_PATH.open(encoding="utf-8") as _fh:
    _RAW: Any = json.load(_fh)
_TEMPLATES = _validated(_RAW)

#: Pinned by a test in both repos; see the module docstring.
CANONICAL_SHA256 = canonical_sha256(_RAW)

#: id -> (English label, section headings in order). Insertion order is the file's order.
BUILT_IN_TEMPLATES: dict[str, tuple[str, tuple[str, ...]]] = {
    tpl["id"]: (tpl["label"], tuple(s["heading"] for s in tpl["sections"])) for tpl in _TEMPLATES
}

#: The text of every grey prompt in the four templates, English and every translation
#: (see `template_prompts`).
PROMPT_TEXTS: frozenset[str] = frozenset(
    text
    for tpl in _TEMPLATES
    for s in tpl["sections"]
    for item in s.get("items", [])
    for text in (item["text"], *item.get("i18n", {}).values())
)
