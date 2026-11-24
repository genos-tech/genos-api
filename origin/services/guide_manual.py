"""The user manual as guide notes (MIP-2324, user-guide milestone MIP-2304).

The manual's text is written once, in genos-frontend (`src/manual/`). `scripts/sync_manual.py` copies the
export into `origin/services/manual/manual.<lang>.json`; this module reads that copy and renders each
topic as a note body through the same Markdown-to-BlockNote converter the agent uses
(`blocknote_md.markdown_to_blocks`). `guide_notes.current_guide_notes` uses it instead of the
hand-written lists for every language that has a copy; a language without one (the frontend has not
exported it yet) keeps the hand-written guide, so a missing copy never leaves a new member without a guide.

The copy is read from disk once per process.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks

MANUAL_DIR = Path(__file__).resolve().parent / "manual"


@lru_cache(maxsize=None)
def load_manual(lang: str) -> dict | None:
    """The vendored manual of one language, or None when there is no copy of it."""
    path = MANUAL_DIR / f"manual.{lang}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def manual_topics(lang: str) -> list[dict]:
    manual = load_manual(lang)
    return list(manual["topics"]) if manual else []


def manual_notes(lang: str) -> list[tuple[str, list]] | None:
    """`(title, blocks)` per topic, in the manual's order; None when the language has no copy."""
    manual = load_manual(lang)
    if manual is None:
        return None
    return [(t["title"], markdown_to_blocks(t["markdown"])) for t in manual["topics"]]
