"""How `list_my_inbox` shows a scheduled Genos report (item type 10), MIP-1160.

Today a report reads as `item_type_label: "type_10"` (the label map predates the type) and its
whole body is returned. A report can run to many sections of many items (the stored limits allow
12 x 30 lines of up to 240 characters), a call can return 50 items, so one call can hand the model
tens of kilobytes of inbox it asked a one-line question about.

Behind `SEARCH_ENGINE["AGENT_INBOX_REPORT_LABEL"]` (default off):

  * the label is `genos_report`;
  * the body is capped at `REPORT_BODY_CHARS` characters of CONTENT (title, summary, section
    headings, item texts and links; the JSON punctuation is not counted). A report within the cap
    is returned exactly as before. A longer one keeps its title and summary and then whole sections
    and whole items in order, until the next item would not fit; nothing is shortened mid-sentence.
    The row then carries `item_body_cut`: `{limit_chars, sections_omitted, items_omitted}` and a
    `note` saying where the rest is. Absent when nothing was cut.

The body's shape is the existing whitelist, `{title, text, sections: [{heading, items: [{text,
link?}]}]}` (MIP-1235), untouched: the cut only removes list entries.

Off: no label change, no cut, no `item_body_cut`: the result is what it was.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

FLAG = "AGENT_INBOX_REPORT_LABEL"

#: Characters of content one report may show the model. About 1,200 tokens: the 12 sections a
#: report can have, with a handful of items each, fit; a long "everything open" list does not.
REPORT_BODY_CHARS = 5000

#: Title and summary are bounded when stored (200, 280); a body from before that is shortened to
#: this so that the head of a report cannot use the whole budget.
HEAD_CHARS = 300

CUT_NOTE = "The rest of this report is on the Inbox page."

REPORT_LABEL = "genos_report"

#: The sentence the flag adds to the tool's description.
DESCRIPTION_ADDITION = (
    " A scheduled Genos report is `item_type_label` `genos_report`: its `item_body` is "
    "{title, text, sections}, cut to about 5,000 characters; when `item_body_cut` is present it "
    "says how many sections and items were left out (the full report is on the Inbox page)."
)


def enabled() -> bool:
    """Read at call time, so an eval arm or `override_settings` can flip it."""
    return bool(settings.SEARCH_ENGINE.get(FLAG))


def _len(value: Any) -> int:
    return len(value) if isinstance(value, str) else 0


def _item_cost(entry: Any) -> int:
    if not isinstance(entry, dict):
        return 0
    return _len(entry.get("text")) + _len(entry.get("link"))


def content_chars(body: dict[str, Any]) -> int:
    total = _len(body.get("title")) + _len(body.get("text"))
    sections = body.get("sections")
    for section in sections if isinstance(sections, list) else []:
        if not isinstance(section, dict):
            continue
        total += _len(section.get("heading"))
        items = section.get("items")
        total += sum(_item_cost(e) for e in (items if isinstance(items, list) else []))
    return total


def cap_report_body(body: Any, limit: int = REPORT_BODY_CHARS) -> tuple[Any, dict[str, Any] | None]:
    """`(body, cut)`: the body within `limit` characters of content, and what was left out.

    A body that is not a dict, or already within the limit, comes back as the same object with no
    cut. Never changes the body it is given."""
    if not isinstance(body, dict) or content_chars(body) <= limit:
        return body, None
    out = {k: v for k, v in body.items() if k != "sections"}
    for key in ("title", "text"):
        if _len(out.get(key)) > HEAD_CHARS:
            out[key] = out[key][:HEAD_CHARS]
    budget = limit - _len(out.get("title")) - _len(out.get("text"))
    sections_in = body.get("sections")
    sections_out: list[Any] = []
    sections_omitted = items_omitted = 0
    full = False  # once one item did not fit, everything after it is left out too
    for section in sections_in if isinstance(sections_in, list) else []:
        if not isinstance(section, dict):
            continue
        items = section.get("items") if isinstance(section.get("items"), list) else []
        kept: list[Any] = []
        left = budget - _len(section.get("heading"))
        for position, entry in enumerate(items):
            cost = _item_cost(entry)
            if full or cost > left:
                full = True
                items_omitted += len(items) - position
                break
            kept.append(entry)
            left -= cost
        if kept:
            budget = left
            sections_out.append({**section, "items": kept})
        else:
            # Nothing of this section was shown: its heading is not charged either.
            sections_omitted += 1
    out["sections"] = sections_out
    return out, {
        "limit_chars": limit,
        "sections_omitted": sections_omitted,
        "items_omitted": items_omitted,
        "note": CUT_NOTE,
    }
