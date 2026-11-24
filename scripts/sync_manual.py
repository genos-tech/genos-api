#!/usr/bin/env python3
"""Copy the user manual from the frontend into the api (MIP-2324, user-guide milestone MIP-2304).

The manual's source lives in genos-frontend (`src/manual/`, exported by `npm run manual:export` to
`manual.<lang>.json`). The api keeps a vendored COPY in `origin/services/manual/`, the same pattern as
`official_catalog/manifest.json` for skills. The guide notes (`guide_notes.py`) are rendered from the copy.

    python scripts/sync_manual.py                         # copy from ../genos-frontend/src/manual
    python scripts/sync_manual.py --source DIR            # copy from another checkout
    python scripts/sync_manual.py --check                 # exit 1 when the copy differs from the source
    python scripts/sync_manual.py --github main --check   # source = the frontend's `main` on GitHub
                                                          # (needs `gh` and a token that can read it)

The copy is byte for byte the frontend's file, so `--check` is a plain comparison. A language the
frontend does not export yet is not an error: the api falls back to its hand-written guide for it. A
copy of a language the frontend no longer exports is "behind" (it would keep serving removed text).
Every file is validated before anything is written, so a bad export never half-updates the copy.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET_DIR = REPO / "origin" / "services" / "manual"
DEFAULT_SOURCE = REPO.parent / "genos-frontend" / "src" / "manual"
LANGUAGES = ("en", "ja")
FRONTEND_REPO = "genos-tech/genos-frontend"
FRONTEND_PATH = "src/manual"


def filename(lang: str) -> str:
    return f"manual.{lang}.json"


def validate(lang: str, raw: bytes) -> list[str]:
    """Problems with one exported file; an empty list means it is usable."""
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return [f"{filename(lang)}: not valid JSON ({exc})"]
    if not isinstance(data, dict):
        return [f"{filename(lang)}: top level is not an object"]
    problems = []
    if data.get("language") != lang:
        problems.append(
            f"{filename(lang)}: language is {data.get('language')!r}, expected {lang!r}"
        )
    topics = data.get("topics")
    if not isinstance(topics, list) or not topics:
        return [*problems, f"{filename(lang)}: no topics"]
    ids: set[str] = set()
    titles: set[str] = set()
    for index, topic in enumerate(topics):
        where = f"{filename(lang)} topic #{index}"
        if not isinstance(topic, dict):
            problems.append(f"{where}: not an object")
            continue
        for key in ("id", "title", "markdown"):
            value = topic.get(key)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{where}: {key} is missing or empty")
        if topic.get("id") in ids:
            problems.append(f"{where}: duplicate id {topic.get('id')!r}")
        if topic.get("title") in titles:
            problems.append(f"{where}: duplicate title {topic.get('title')!r}")
        ids.add(topic.get("id"))
        titles.add(topic.get("title"))
    return problems


def read_local(source: Path) -> dict[str, bytes]:
    return {
        lang: (source / filename(lang)).read_bytes()
        for lang in LANGUAGES
        if (source / filename(lang)).is_file()
    }


def read_github(ref: str) -> dict[str, bytes]:
    found = {}
    for lang in LANGUAGES:
        api_path = f"repos/{FRONTEND_REPO}/contents/{FRONTEND_PATH}/{filename(lang)}?ref={ref}"
        result = subprocess.run(
            ["gh", "api", "-H", "Accept: application/vnd.github.raw", api_path],
            capture_output=True,
        )
        if result.returncode == 0:
            found[lang] = result.stdout
        elif b"404" in result.stderr:
            continue  # not exported there (yet)
        else:
            raise SystemExit(f"gh api failed for {filename(lang)}: {result.stderr.decode()[:300]}")
    return found


def read_copy() -> dict[str, bytes]:
    return read_local(TARGET_DIR)


def differences(source: dict[str, bytes], copy: dict[str, bytes]) -> list[str]:
    out = []
    for lang in sorted(source.keys() | copy.keys()):
        if lang not in copy:
            out.append(f"{filename(lang)}: in the frontend, not copied yet")
        elif lang not in source:
            out.append(f"{filename(lang)}: copied, but the frontend no longer exports it")
        elif source[lang] != copy[lang]:
            out.append(f"{filename(lang)}: differs from the frontend")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--github", metavar="REF", help="read the frontend's file at REF via gh")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if behind")
    args = parser.parse_args(argv)

    source = read_github(args.github) if args.github else read_local(args.source)
    if not source:
        where = f"{FRONTEND_REPO}@{args.github}" if args.github else str(args.source)
        print(f"no manual.<lang>.json found in {where}", file=sys.stderr)
        return 2
    problems = [p for lang, raw in source.items() for p in validate(lang, raw)]
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 2

    behind = differences(source, read_copy())
    if args.check:
        if behind:
            print("The api's manual copy is behind the frontend:", file=sys.stderr)
            print("\n".join(f"  - {line}" for line in behind), file=sys.stderr)
            print(
                "Run: python scripts/sync_manual.py  (then commit origin/services/manual/)",
                file=sys.stderr,
            )
            return 1
        print(f"manual copy is current ({', '.join(sorted(source))})")
        return 0

    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    for lang, raw in source.items():
        (TARGET_DIR / filename(lang)).write_bytes(raw)
    for lang in set(read_copy()) - set(source):
        (TARGET_DIR / filename(lang)).unlink()
    print("synced:", "; ".join(behind) if behind else "already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
