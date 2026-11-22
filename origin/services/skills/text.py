"""The words a skill is made of: slug, title, description, tags, and the markdown file format
(MIP-2040). Pure functions, no database, so the rules are testable on their own.

A skill file is the Claude `SKILL.md` shape: `name` and `description` in a front matter block,
then the body. Importing one maps `name` to the slug, `description` to the description and the
body to the skill text; exporting writes the same keys back.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

SLUG_MIN = 2
SLUG_MAX = 40
TITLE_MAX = 120
DESCRIPTION_MAX = 280
TAGS_MAX = 5
TAG_MAX = 24
ARGUMENT_HINT_MAX = 120
BODY_MAX_CHARS = 12_000  # about 3k tokens: the block sent to the model counts toward input cost

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TAG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Names a person cannot take: the built-in commands of the `/` picker and the names the MCP
# server already publishes as prompts. The official skills' slugs are added when they ship
# (MIP-2048).
RESERVED_SLUGS = frozenset(
    {
        "help",
        "new",
        "clear",
        "schedule",
        "skills",
        "hub",
        "settings",
        "catch-me-up",
        "catch_me_up",
        "plan-my-day",
        "plan_my_day",
    }
)


class SkillTextError(ValueError):
    """A field does not meet the rule. The message is a sentence the page shows as written."""


def clean_slug(value: Any) -> str:
    """The name typed after `/`. Refuses instead of fixing: a changed name would surprise the
    person who typed it."""
    slug = str(value or "").strip().lower()
    if not (SLUG_MIN <= len(slug) <= SLUG_MAX):
        raise SkillTextError(f"The name must be {SLUG_MIN} to {SLUG_MAX} characters.")
    if not _SLUG_RE.match(slug):
        raise SkillTextError(
            "The name may use lower-case letters, digits and single hyphens (for example "
            "`weekly-update`)."
        )
    if slug in RESERVED_SLUGS:
        raise SkillTextError(f"`{slug}` is reserved. Choose another name.")
    return slug


def slugify(title: str) -> str:
    """A valid slug from a title (used when a skill is created or imported without a name).
    Not guaranteed free or unreserved: the caller still runs `clean_slug` and the uniqueness
    check."""
    folded = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:SLUG_MAX].strip("-")
    return slug if len(slug) >= SLUG_MIN else "skill"


def clean_title(value: Any) -> str:
    title = " ".join(str(value or "").split())
    if not title:
        raise SkillTextError("A skill needs a title.")
    if len(title) > TITLE_MAX:
        raise SkillTextError(f"The title may be at most {TITLE_MAX} characters.")
    return title


def clean_description(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > DESCRIPTION_MAX:
        raise SkillTextError(f"The description may be at most {DESCRIPTION_MAX} characters.")
    return text


def clean_argument_hint(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > ARGUMENT_HINT_MAX:
        raise SkillTextError(f"The argument hint may be at most {ARGUMENT_HINT_MAX} characters.")
    return text


def clean_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise SkillTextError("Tags must be a list.")
    out: list[str] = []
    for raw in value:
        tag = str(raw or "").strip().lower()
        if not tag:
            continue
        if len(tag) > TAG_MAX or not _TAG_RE.match(tag):
            raise SkillTextError(
                f"A tag uses lower-case letters, digits and hyphens, at most {TAG_MAX} characters."
            )
        if tag not in out:
            out.append(tag)
    if len(out) > TAGS_MAX:
        raise SkillTextError(f"A skill may have at most {TAGS_MAX} tags.")
    return out


def check_body_markdown(markdown: str) -> str:
    if len(markdown) > BODY_MAX_CHARS:
        raise SkillTextError(
            f"The skill text may be at most {BODY_MAX_CHARS:,} characters "
            f"({len(markdown):,} given): it is sent to the model on every run."
        )
    return markdown


# -- the SKILL.md file ---------------------------------------------------------------------------

_FRONT_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)
_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)[ \t]*:[ \t]*(.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return re.sub(r"\\(.)", r"\1", value[1:-1])
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    return value


def parse_skill_file(text: str) -> dict[str, str]:
    """`{"name", "description", "body"}` from a SKILL.md file. A file without front matter is all
    body. Only `name` and `description` are read; every other key is ignored (a Claude skill may
    carry more). Raises `SkillTextError` for an empty file."""
    text = (text or "").replace("\r\n", "\n").lstrip("﻿")
    if not text.strip():
        raise SkillTextError("The file is empty.")
    meta: dict[str, str] = {}
    body = text
    match = _FRONT_RE.match(text)
    if match:
        body = text[match.end() :]
        for line in match.group(1).split("\n"):
            key = _KEY_RE.match(line)
            if key and key.group(1).lower() in ("name", "description"):
                meta[key.group(1).lower()] = _unquote(key.group(2))
    return {
        "name": meta.get("name", ""),
        "description": meta.get("description", ""),
        "body": body.strip("\n"),
    }


def _quote(value: str) -> str:
    one_line = " ".join(value.split())
    return '"' + one_line.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_skill_file(*, slug: str, description: str, body_markdown: str) -> str:
    """The SKILL.md text for a skill (the inverse of `parse_skill_file`)."""
    return (
        f"---\nname: {slug}\ndescription: {_quote(description)}\n---\n\n{body_markdown.strip()}\n"
    )
