"""Build the record of every guide note text we have shipped (MIP-2325).

Why: a guide copy may be replaced only while its text is still one we shipped (nobody edited it).
Hand-written lists change often, so the fingerprints of ALL past texts are taken from git history, not
remembered. Inputs are one JSON per commit, made by running `scripts/dump_guide_notes.py` (see below) in a checkout of
each commit that touched `guide_notes.py` or `guide_company_notes.py`:

    for sha in $(git log --format=%h <last-hand-written-commit> -- origin/services/guide_notes.py \
            origin/services/guide_company_notes.py); do
        git worktree add --detach /tmp/wt-$sha $sha      # then, in the api image, in that tree:
        python scripts/dump_guide_notes.py dump_$sha.json                # {locale: {title: body}} of GUIDE_NOTES lists
    done

Run (inside the api image, cwd = repo root):

    python scripts/build_guide_shipped.py <dumps-dir> <commit-dates.tsv>

where the TSV is `sha<TAB>YYYY-MM-DD` per commit. Writes
  origin/tests/fixtures/guide_notes_shipped.json   the bodies (a test pins the hashes)
  origin/services/guide_shipped.json               {locale: {title: {"first_shipped": date, "sha256": [..]}}}
Commits are read oldest first, so a text's `first_shipped` is the date of the first commit with it.
The manual-based text (MIP-2324) is NOT in here: every text the vendored manual has had is recorded in
  origin/services/guide_shipped_manual.json by scripts/record_manual_shipped.py (MIP-2395).
"""

import json
import os
import sys

import django

sys.path.insert(0, os.getcwd())  # run from the repo root

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apis.settings")
django.setup()

from origin.services.guide_notes import body_fingerprint  # noqa: E402


def main(dumps_dir: str, dates_tsv: str) -> None:
    rows = [
        line.split("\t") for line in open(dates_tsv, encoding="utf-8").read().split("\n") if line
    ]
    rows.sort(key=lambda r: r[1])  # oldest first
    fixture: dict = {"en": {}, "ja": {}}
    shipped: dict = {"en": {}, "ja": {}}
    for sha, date in rows:
        path = os.path.join(dumps_dir, f"dump_{sha}.json")
        if not os.path.isfile(path):
            raise SystemExit(f"missing dump for {sha}")
        dump = json.load(open(path, encoding="utf-8"))
        for locale, notes in dump.items():
            for title, body in notes.items():
                fingerprint = body_fingerprint(body)
                entry = shipped[locale].setdefault(title, {"first_shipped": date, "sha256": []})
                if fingerprint in entry["sha256"]:
                    continue
                entry["sha256"].append(fingerprint)
                fixture[locale].setdefault(title, []).append(
                    {"commit": sha, "date": date, "sha256": fingerprint, "body": body}
                )
    with open("origin/tests/fixtures/guide_notes_shipped.json", "w", encoding="utf-8") as fh:
        json.dump(fixture, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    with open("origin/services/guide_shipped.json", "w", encoding="utf-8") as fh:
        json.dump(shipped, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    for locale in shipped:
        print(
            locale,
            len(shipped[locale]),
            "titles",
            sum(len(v["sha256"]) for v in shipped[locale].values()),
            "texts",
        )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
