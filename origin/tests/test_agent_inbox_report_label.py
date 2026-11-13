"""MIP-1160: `list_my_inbox` labels a scheduled report and caps its body, behind AGENT_INBOX_REPORT_LABEL.

Off (the default) a report (item type 10) reads as `item_type_label: "type_10"` with its whole
body, exactly as `origin/main` returns it (`test_schedule_inbox_report` pins the small case too).
On, its label is `genos_report` and its body is cut at 5,000 characters of content, whole items
in order, with `item_body_cut` saying what was left out.

PINS. The off-state goldens were recorded on PRISTINE `origin/main` (09f52388), where the flag
does not exist, over a fixed inbox (a small and a very large report, an unversioned report, a
digest, a reminder, an activity and three requests):

    git -C <genos-api> worktree add --detach <wt> origin/main
    cp origin/tests/test_agent_inbox_report_label.py <wt>/origin/tests/
    RECORD_GOLDEN=1 python manage.py test origin.tests.test_agent_inbox_report_label.FlagOffTests --noinput
    # prints `GOLDEN <key> = "<sha256>"`; paste below

`inbox/*` hash `list_my_inbox`'s result (ids replaced by their order of appearance, the rest
verbatim: key order, labels, bodies, timestamps); `declarations` hashes every tool declaration
the model is sent. FlagOffTests imports nothing this change adds, so it runs on main as it is.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.models.common.inbox_models import InboxItems
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.tests.test_base import BaseAPITestCase

FLAG = "AGENT_INBOX_REPORT_LABEL"

GOLDEN: dict[str, str] = {
    "inbox/all": "3b8c6ad4cb42d1c9a5ce80f1116d9c572e4fe5ace60e4526d19a9c8539794e1d",
    "inbox/activity": "d9e1938fa41cc6f8a4b2060bddff28612112845e0fcae08adf593b359e0fa769",
    "inbox/requests": "ea7240167db1c3a878fcc951c7a2e5e8851bf5789a3fe000fe472821cb1e74ac",
    "inbox/limit-1": "df5493ac8c0e0128c606a9a8a5144ec1a91316510075316309565c8660de0ed9",
    "declarations": "ef496d745cfc175a1a0e02e92e254e7f4304bab500d1ffd93d9016da2731832a",
}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def normalised(result) -> str:
    seen: dict[str, str] = {}

    def walk(value, key=""):
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, key) for v in value]
        if isinstance(value, int) and not isinstance(value, bool) and key == "item_id":
            return "<item>"
        if isinstance(value, str):
            return _UUID.sub(lambda m: seen.setdefault(m.group(0), f"<uuid{len(seen)}>"), value)
        return value

    return json.dumps(walk(result), ensure_ascii=False, indent=1)


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def on(extra=None):
    return override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, FLAG: True, **(extra or {})})


def line(n: int, width: int = 60) -> str:
    return f"item {n:03d} " + "x" * (width - 9)


def report_body(sections: int, items: int, *, width: int = 60, version: bool = True) -> dict:
    """A report as `schedule_delivery.clean_report` stores it (typed fields included)."""
    body = {
        "title": "Weekly report",
        "text": "Two things need you.",
        "sections": [
            {
                "heading": f"Section {s}",
                "key": "overdue",
                "emoji": "x",
                "items": [
                    {
                        "text": line(n, width),
                        "link": f"/workspace/tasks/KDS-{n}",
                        "kind": "task",
                        "project": "Website",
                        "status": "WIP",
                    }
                    for n in range(items)
                ],
            }
            for s in range(sections)
        ],
        "stats": [{"label": "open", "count": 3}],
    }
    if version:
        body["version"] = 2
    return body


class InboxBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.n = 0

    def add(self, item_type, body, *, read=False, status="", optionals=None, sender=None):
        self.n += 1
        row = InboxItems.objects.create(
            team=self.team,
            receiver=self.user,
            sender=sender,
            item_type=item_type,
            item_body=body,
            item_optionals=optionals,
            is_read=read,
            request_status=status,
        )
        InboxItems.objects.filter(pk=row.pk).update(
            ts_created_at=datetime(2026, 11, 2, 9, self.n, tzinfo=timezone.utc)
        )
        return row

    def fill(self):
        """A fixed inbox, oldest first: each kind of item the tool knows, and the reports."""
        self.add(0, {"title": "Alice closed WRD-5", "text": "done"}, sender=self.user2)
        self.add(6, {"title": "Digest", "text": "Your week"})
        self.add(9, {"title": "Reminder", "text": "Reply to Ben"}, optionals={"kind": "reminder"})
        self.add(
            2,
            {"title": "Join", "text": "Ben asks"},
            status="pending",
            optionals={"project_id": 7},
            sender=self.user2,
        )
        self.add(
            3,
            {"title": "Join GM", "text": "Ben asks"},
            status="approved",
            optionals={"gm_id": "11111111-1111-4111-8111-111111111111"},
            sender=self.user2,
        )
        self.add(4, {"title": "Note", "text": "Ben asks"}, status="rejected", sender=self.user2)
        self.add(10, report_body(1, 2), optionals={"kind": "report"})  # small
        self.add(10, report_body(12, 30), optionals={"kind": "report"})  # ~36k characters
        self.add(10, report_body(3, 30, version=False), optionals={"kind": "report"})  # old
        self.add(10, {"title": "Odd", "text": "x", "sections": "not a list"}, read=True)

    def run_tool(self, **args):
        return REGISTRY["list_my_inbox"].run({"include_read": True, "limit": 50, **args}, self.ctx)


class FlagOffTests(InboxBase):
    """What the model is sent with the flag off: main's, byte for byte (hashes from main)."""

    def pin(self, key, text):
        digest = digest_text(text)
        if os.environ.get("RECORD_GOLDEN"):
            print(f'GOLDEN {key} = "{digest}"')
            return
        self.assertEqual(digest, GOLDEN[key], f"what the model is sent changed: {key}")

    def test_the_inbox_as_the_model_reads_it(self):
        self.fill()
        self.pin("inbox/all", normalised(self.run_tool()))
        self.pin("inbox/activity", normalised(self.run_tool(category="activity")))
        self.pin("inbox/requests", normalised(self.run_tool(category="requests")))
        self.pin("inbox/limit-1", normalised(self.run_tool(limit=1)))

    def test_the_declarations_the_model_reads(self):
        text = json.dumps([asdict(d) for d in _build_tool_declarations()], sort_keys=True)
        self.pin("declarations", text)

    def test_a_report_is_type_10_and_whole_with_no_cut_key(self):
        self.fill()
        rows = [r for r in self.run_tool()["items"] if r["item_type"] == 10]
        self.assertEqual({r["item_type_label"] for r in rows}, {"type_10"})
        self.assertTrue(all("item_body_cut" not in r for r in rows))
        big = max(rows, key=lambda r: len(json.dumps(r)))
        self.assertEqual(len(big["item_body"]["sections"]), 12)
        self.assertEqual(sum(len(s["items"]) for s in big["item_body"]["sections"]), 360)


# --------------------------------------------------------------------------- #
# With the flag on                                                            #
# --------------------------------------------------------------------------- #


class FlagOnTests(InboxBase):
    def test_a_report_is_labelled_and_nothing_else_changes_label(self):
        self.fill()
        before = self.run_tool()["items"]
        with on():
            after = self.run_tool()["items"]
        self.assertEqual([r["item_id"] for r in before], [r["item_id"] for r in after])
        for b, a in zip(before, after):
            if b["item_type"] == 10:
                self.assertEqual(b["item_type_label"], "type_10")
                self.assertEqual(a["item_type_label"], "genos_report")
            else:
                self.assertEqual(a, b)

    def test_a_report_within_the_cap_is_returned_exactly_as_before(self):
        self.fill()
        before = {r["item_id"]: r for r in self.run_tool()["items"] if r["item_type"] == 10}
        with on():
            after = {r["item_id"]: r for r in self.run_tool()["items"] if r["item_type"] == 10}
        small = [i for i, r in before.items() if len(json.dumps(r)) < 3000]
        self.assertEqual(len(small), 2)  # the small report and the odd one
        for item_id in small:
            self.assertEqual(after[item_id]["item_body"], before[item_id]["item_body"])
            self.assertNotIn("item_body_cut", after[item_id])

    def test_a_large_report_is_cut_in_order_and_says_what_was_left_out(self):
        self.add(10, report_body(12, 30), optionals={"kind": "report"})
        original = self.run_tool()["items"][0]["item_body"]
        with on():
            (row,) = self.run_tool()["items"]
        from origin.search_engine.agent.tools import inbox_report  # noqa: PLC0415

        body, cut = row["item_body"], row["item_body_cut"]
        self.assertLessEqual(inbox_report.content_chars(body), 5000)
        self.assertEqual(set(body), {"title", "text", "sections"})
        self.assertEqual(body["title"], original["title"])
        self.assertEqual(body["text"], original["text"])
        kept = sum(len(s["items"]) for s in body["sections"])
        total = sum(len(s["items"]) for s in original["sections"])
        self.assertEqual(kept + cut["items_omitted"], total)
        self.assertGreater(cut["items_omitted"], 0)
        self.assertEqual(cut["sections_omitted"], 12 - len(body["sections"]))
        self.assertEqual(cut["limit_chars"], 5000)
        self.assertIn("Inbox", cut["note"])
        # A prefix of the original, in order: whole sections, then a whole prefix of the next.
        flat = [e["text"] for s in original["sections"] for e in s["items"]]
        shown = [e["text"] for s in body["sections"] for e in s["items"]]
        self.assertEqual(shown, flat[: len(shown)])
        for sec, orig in zip(body["sections"], original["sections"]):
            self.assertEqual(sec["heading"], orig["heading"])

    def test_the_whitelist_shape_of_the_body_and_its_items_is_kept(self):
        self.add(10, report_body(12, 30))
        with on():
            (row,) = self.run_tool()["items"]
        for section in row["item_body"]["sections"]:
            self.assertEqual(set(section), {"heading", "items"})
            for entry in section["items"]:
                self.assertEqual(set(entry), {"text", "link"})

    def test_a_call_of_fifty_large_reports_is_bounded(self):
        for _ in range(50):
            self.add(10, report_body(12, 30))
        from origin.search_engine.agent.tools import inbox_report  # noqa: PLC0415

        with off_flag():
            whole = len(json.dumps(self.run_tool()))
        with on():
            result = self.run_tool()
        self.assertEqual(len(result["items"]), 50)
        for row in result["items"]:
            self.assertLessEqual(inbox_report.content_chars(row["item_body"]), 5000)
        self.assertLess(len(json.dumps(result)) * 5, whole)

    def test_the_stored_row_is_never_changed(self):
        row = self.add(10, report_body(12, 30))
        stored = json.dumps(InboxItems.objects.get(pk=row.pk).item_body, sort_keys=True)
        with on():
            self.run_tool()
        self.assertEqual(
            json.dumps(InboxItems.objects.get(pk=row.pk).item_body, sort_keys=True), stored
        )

    def test_an_unversioned_report_is_capped_too(self):
        self.add(10, report_body(3, 30, version=False))
        from origin.search_engine.agent.tools import inbox_report  # noqa: PLC0415

        with on():
            (row,) = self.run_tool()["items"]
        self.assertLessEqual(inbox_report.content_chars(row["item_body"]), 5000)
        self.assertIn("item_body_cut", row)

    def test_other_kinds_of_item_are_never_capped_or_given_a_cut_key(self):
        self.add(0, {"title": "x", "text": "y" * 20000}, sender=self.user2)
        with on():
            (row,) = self.run_tool()["items"]
        self.assertEqual(len(row["item_body"]["text"]), 20000)
        self.assertNotIn("item_body_cut", row)

    def test_another_members_report_is_still_not_shown(self):
        InboxItems.objects.create(
            team=self.team, receiver=self.user2, item_type=10, item_body=report_body(1, 1)
        )
        with on():
            self.assertEqual(self.run_tool()["items"], [])

    def test_one_sentence_is_added_to_the_declaration_and_nothing_else_moves(self):
        from origin.search_engine.agent.tools import inbox_report  # noqa: PLC0415

        before = {d.name: d for d in _build_tool_declarations()}
        with on():
            after = {d.name: d for d in _build_tool_declarations()}
        for name in before:
            if name != "list_my_inbox":
                self.assertEqual(before[name], after[name], name)
        self.assertEqual(
            after["list_my_inbox"].description,
            before["list_my_inbox"].description + inbox_report.DESCRIPTION_ADDITION,
        )
        self.assertEqual(
            before["list_my_inbox"].parameters_schema, after["list_my_inbox"].parameters_schema
        )

    def test_the_flag_defaults_off(self):
        self.assertIs(settings.SEARCH_ENGINE[FLAG], False)


def off_flag():
    return override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, FLAG: False})


class CapTests(SimpleTestCase):
    """The cut itself, on bodies built by hand."""

    def body(self, *sections, title="T", text="s"):
        return {
            "title": title,
            "text": text,
            "sections": [
                {"heading": h, "items": [{"text": t} for t in items]} for h, items in sections
            ],
        }

    def cap(self, body, limit):
        from origin.search_engine.agent.tools.inbox_report import cap_report_body  # noqa: PLC0415

        return cap_report_body(body, limit)

    def test_exactly_the_limit_is_not_cut_and_one_over_is(self):
        body = self.body(("h", ["a" * 5, "b" * 5]), title="T", text="s")  # 1+1+1+5+5 = 13
        out, cut = self.cap(body, 13)
        self.assertIs(out, body)
        self.assertIsNone(cut)
        out, cut = self.cap(body, 12)
        self.assertEqual(cut["items_omitted"], 1)
        self.assertEqual([e["text"] for e in out["sections"][0]["items"]], ["a" * 5])

    def test_the_cut_stops_at_the_first_item_that_does_not_fit_even_if_a_later_one_would(self):
        body = self.body(("h", ["a" * 4, "b" * 50, "c" * 1]), ("g", ["d" * 1]))
        out, cut = self.cap(body, 20)
        self.assertEqual([e["text"] for e in out["sections"][0]["items"]], ["a" * 4])
        self.assertEqual(len(out["sections"]), 1)
        self.assertEqual(cut["items_omitted"], 3)
        self.assertEqual(cut["sections_omitted"], 1)

    def test_a_section_with_nothing_shown_is_left_out_and_counted_not_charged(self):
        body = self.body(("first", ["a" * 8]), ("second", ["b" * 40]))
        out, cut = self.cap(body, 15)
        self.assertEqual([s["heading"] for s in out["sections"]], ["first"])
        self.assertEqual((cut["sections_omitted"], cut["items_omitted"]), (1, 1))

    def test_a_long_title_and_summary_are_shortened_to_leave_room(self):
        body = self.body(("h", ["a" * 5]), title="T" * 9000, text="s" * 9000)
        out, cut = self.cap(body, 2000)
        self.assertEqual(len(out["title"]), 300)
        self.assertEqual(len(out["text"]), 300)
        self.assertIsNotNone(cut)

    def test_a_body_that_is_not_a_dict_comes_back_untouched(self):
        for odd in (None, "text", ["a"], 5):
            out, cut = self.cap(odd, 1)
            self.assertIs(out, odd)
            self.assertIsNone(cut)

    def test_the_body_given_is_not_mutated(self):
        body = self.body(("h", ["a" * 50, "b" * 50]))
        before = json.dumps(body, sort_keys=True)
        self.cap(body, 20)
        self.assertEqual(json.dumps(body, sort_keys=True), before)


class SeedTests(InboxBase):
    """The eval seed of the t1160 cases (`group/seeds.py`): one long report, idempotent."""

    def info(self, team="v1"):
        return {"team_id": str(self.team.team_id), "user_id": str(self.user.id), "team": team}

    def test_the_seed_files_one_unread_report_far_over_the_cap_and_only_once(self):
        from origin.search_engine.agent.evals.group import seeds  # noqa: PLC0415
        from origin.search_engine.agent.tools import inbox_report  # noqa: PLC0415

        for team in ("v1", "ja"):
            InboxItems.objects.all().delete()
            seeds.apply(["inbox_report"], self.info(team))
            seeds.apply(["inbox_report"], self.info(team))
            (row,) = InboxItems.objects.filter(item_type=10)
            self.assertFalse(row.is_read)
            self.assertEqual(row.receiver_id, self.user.id)
            self.assertGreater(inbox_report.content_chars(row.item_body), 7000)
            flat = [e["text"] for s in row.item_body["sections"] for e in s["items"]]
            self.assertEqual(len(flat), 133)
            self.assertIn("ZULU-77", flat[-1])
            self.assertIn("CLP-9", flat[0])
            with on():
                (shown,) = self.run_tool()["items"]
            self.assertIn("CLP-9", json.dumps(shown["item_body"], ensure_ascii=False))
            self.assertNotIn("ZULU-77", json.dumps(shown["item_body"], ensure_ascii=False))
            self.assertGreater(shown["item_body_cut"]["items_omitted"], 50)

    def test_an_unknown_seed_is_refused_by_name(self):
        from origin.search_engine.agent.evals.group import seeds  # noqa: PLC0415

        with self.assertRaises(seeds.SeedError):
            seeds.apply(["nope"], self.info())

    def test_the_seed_does_not_touch_anyone_elses_inbox(self):
        from origin.search_engine.agent.evals.group import seeds  # noqa: PLC0415

        seeds.apply(["inbox_report"], self.info())
        self.assertFalse(InboxItems.objects.filter(receiver=self.user2).exists())
