#!/usr/bin/env python3
"""Record every vendored manual text as "shipped" (MIP-2395, user-guide milestone MIP-2304).

Why: `refresh_guide_notes` replaces a member's guide copy only while its text is still one we shipped
(nobody edited it). The hand-written guide texts are recorded in `guide_shipped.json` (built from git
history by `build_guide_shipped.py`), but the guide is now rendered from the vendored manual
(MIP-2324), whose texts were not in that file: an unedited copy of the PREVIOUS manual text read as
"edited by its owner" and was left alone, so no manual change could ever reach an existing folder.

This script adds the fingerprint of every topic of a vendored manual file to
`origin/services/guide_shipped_manual.json` ({locale: {title: {"sha256": [...]}}}). It only adds
(a recorded text stays recorded), so run it on every version the copy ever had.

    # run inside the api image, cwd = repo root

    python scripts/record_manual_shipped.py                       # the vendored copies as they are now
    python scripts/record_manual_shipped.py --file old.en.json    # an older copy (the language is read from it)
    python scripts/record_manual_shipped.py --check               # exit 1 when a current text is not recorded

Backfill, once, from git history (on the host; the image has no git history):

    for sha in $(git log --format=%h -- origin/services/manual/manual.en.json origin/services/manual/manual.ja.json); do
        for lang in en ja; do
            git show $sha:origin/services/manual/manual.$lang.json > /tmp/manual-dumps/$sha.$lang.json 2>/dev/null
        done
    done

then run this script with one `--file` per dump. A test (`test_guide_shipped_manual`) fails when a
vendored text is not recorded, so a new manual copy cannot land without its text being recorded here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import django

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # run from the repo root

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apis.settings")
django.setup()

from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks  # noqa: E402
from origin.services.guide_notes import body_fingerprint  # noqa: E402

RECORD_PATH = REPO / "origin" / "services" / "guide_shipped_manual.json"
MANUAL_DIR = REPO / "origin" / "services" / "manual"
LANGUAGES = ("en", "ja")
HEADER = (
    "Fingerprints of every text the vendored manual has had, per language and topic title "
    "(MIP-2395). Written by scripts/record_manual_shipped.py; a test pins the current texts to it."
)


def fingerprints(path: Path) -> tuple[str, dict[str, str]]:
    """(language, {topic title: fingerprint of the note body the guide renders from it})."""
    data = json.loads(path.read_text(encoding="utf-8"))
    lang = data["language"]
    return lang, {
        topic["title"]: body_fingerprint(markdown_to_blocks(topic["markdown"]))
        for topic in data["topics"]
    }


def load_record() -> dict:
    if RECORD_PATH.is_file():
        return json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    return {"_comment": HEADER}


def write_record(record: dict) -> None:
    out = {"_comment": HEADER}
    for lang in sorted(k for k in record if k != "_comment"):
        out[lang] = {
            title: {"sha256": sorted(set(entry["sha256"]))}
            for title, entry in sorted(record[lang].items())
        }
    RECORD_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def add(record: dict, path: Path) -> int:
    """Add one manual file's fingerprints; returns how many were new."""
    lang, found = fingerprints(path)
    new = 0
    for title, fp in found.items():
        entry = record.setdefault(lang, {}).setdefault(title, {"sha256": []})
        if fp not in entry["sha256"]:
            entry["sha256"].append(fp)
            new += 1
    return new


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--file", action="append", type=Path, default=[])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    record = load_record()
    if args.check:
        missing = []
        for lang in LANGUAGES:
            path = MANUAL_DIR / f"manual.{lang}.json"
            if not path.is_file():
                continue
            _, found = fingerprints(path)
            for title, fp in found.items():
                if fp not in record.get(lang, {}).get(title, {}).get("sha256", ()):
                    missing.append(f"{lang}: {title}")
        for line in missing:
            print(f"not recorded: {line}")
        return 1 if missing else 0

    files = args.file or [MANUAL_DIR / f"manual.{lang}.json" for lang in LANGUAGES]
    total = 0
    for path in files:
        if path.is_file():
            total += add(record, path)
    write_record(record)
    print(f"recorded {total} new text(s) in {RECORD_PATH.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
