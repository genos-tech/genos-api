"""Dump the guide note lists of the checkout this runs in: {locale: {title: body}}.

Usage (inside the api container, cwd = a checkout): python scripts/dump_guide_notes.py <out.json>
Used by scripts/build_guide_shipped.py to read every text the guide has shipped.
"""

import json
import os
import sys

import django

sys.path.insert(0, os.getcwd())  # run from the repo root

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apis.settings")
django.setup()

from origin.services import guide_notes  # noqa: E402

out = {"en": {}, "ja": {}}
for locale, name in (("en", "GUIDE_NOTES"), ("ja", "JAPANESE_GUIDE_NOTES")):
    for title, body in getattr(guide_notes, name, []):
        out[locale][title] = body
with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(out, fh, ensure_ascii=False)
