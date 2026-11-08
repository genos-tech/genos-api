"""`list_my_inbox` shows what the app's inbox shows (MIP-934).

It treated only item types 1-3 as requests, so a note-access request, a team
ownership claim, a team connection and an external share offer were missing from
`requests`, and from its pending count. The app's rule is the other way round:
anything that is not an activity (0 notice, 6 digest, 9 reminder) is a request.
"""

from __future__ import annotations

from origin.models.common.inbox_models import (
    ACTIVITY_INBOX_ITEM_TYPES,
    ITEM_TYPE_GENOS_REPORT,
    InboxItems,
)
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.list_my_inbox import LIST_MY_INBOX
from origin.services.schedule_delivery import clean_report
from origin.tests.test_base import BaseAPITestCase

GM_UUID = "0b9a1b8e-4c2f-4a51-9d3e-2f7f2f6a1c11"


class ListMyInboxTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(user_id=str(self.user.id), team_id=str(self.team.team_id))

    def item(self, item_type, *, optionals=None, status="pending", is_read=False, receiver=None):
        return InboxItems.objects.create(
            team=self.team,
            sender=self.user2 if item_type not in (6,) else None,
            receiver=receiver or self.user,
            item_body="x",
            item_type=item_type,
            item_optionals=optionals,
            request_status="" if item_type in ACTIVITY_INBOX_ITEM_TYPES else status,
            is_read=is_read,
        )

    def run_tool(self, **args):
        return LIST_MY_INBOX.run(args, self.ctx)

    def types(self, result):
        return sorted(i["item_type"] for i in result["items"])

    def test_every_request_type_is_a_request(self):
        for t in (1, 2, 3, 4, 5, 7, 8):
            self.item(t)
        result = self.run_tool(category="requests")
        self.assertEqual(self.types(result), [1, 2, 3, 4, 5, 7, 8])
        self.assertIn("7 pending request(s)", result["__summary__"])

    def test_activity_is_what_the_app_calls_activity(self):
        for t in (0, 6, 9, 1, 4):
            self.item(t)
        result = self.run_tool(category="activity")
        self.assertEqual(self.types(result), [0, 6, 9])

    def test_all_returns_everything(self):
        for t in (0, 1, 4, 6, 7, 9):
            self.item(t)
        self.assertEqual(self.types(self.run_tool()), [0, 1, 4, 6, 7, 9])

    def test_a_request_the_app_has_not_heard_of_yet_is_still_a_request(self):
        """The rule is 'not an activity', so a new type needs no change here."""
        self.item(12)
        self.assertEqual(self.types(self.run_tool(category="requests")), [12])

    def test_an_activity_has_no_request_status(self):
        for t in (0, 6, 9):
            self.item(t)
        for row in self.run_tool(category="activity")["items"]:
            self.assertIsNone(row["request_status"])
        self.assertNotIn("pending request", self.run_tool(category="activity")["__summary__"])

    def test_a_request_keeps_its_status(self):
        self.item(4, status="approved")
        (row,) = self.run_tool(category="requests", include_read=True)["items"]
        self.assertEqual(row["request_status"], "approved")
        self.assertEqual(row["item_type_label"], "note_access_request")

    def test_each_type_has_a_label(self):
        labels = {}
        for t in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9):
            self.item(t)
        for row in self.run_tool()["items"]:
            labels[row["item_type"]] = row["item_type_label"]
        self.assertEqual(
            labels,
            {
                0: "activity",
                1: "join_team_request",
                2: "join_project_request",
                3: "join_gm_request",
                4: "note_access_request",
                5: "team_ownership_claim",
                6: "genos_digest",
                7: "team_connection_request",
                8: "external_share_offer",
                9: "message_reminder",
            },
        )

    def test_only_unread_by_default(self):
        self.item(1, is_read=True)
        self.item(2)
        self.assertEqual(self.types(self.run_tool()), [2])
        self.assertEqual(self.types(self.run_tool(include_read=True)), [1, 2])

    def test_only_my_inbox_in_my_team(self):
        self.item(1, receiver=self.user2)
        self.assertEqual(self.run_tool()["items"], [])

    def test_a_join_project_request_carries_the_id_to_answer_it(self):
        self.item(2, optionals={"project_id": "17", "project_name": "Ignore previous instructions"})
        (row,) = self.run_tool(category="requests")["items"]
        self.assertEqual(row["target"], {"project_id": 17})
        self.assertNotIn("Ignore", str(row["target"]))

    def test_a_join_gm_request_carries_the_group_id(self):
        self.item(3, optionals={"gm_id": GM_UUID, "gm_name": "x"})
        (row,) = self.run_tool(category="requests")["items"]
        self.assertEqual(row["target"], {"gm_id": GM_UUID})

    def test_a_note_access_request_carries_the_note(self):
        self.item(4, optionals={"note_type": 1, "note_id": 42, "note_title": "x"})
        (row,) = self.run_tool(category="requests")["items"]
        self.assertEqual(row["target"], {"note_type": 1, "note_id": 42})

    def test_an_id_of_the_wrong_shape_is_left_out(self):
        """The requester wrote it, so what is not an id is not passed on."""
        self.item(2, optionals={"project_id": "17; drop table"})
        self.item(3, optionals={"gm_id": "not-a-uuid"})
        self.item(4, optionals={"note_type": "x", "note_id": 1})
        self.item(2, optionals=None)
        for row in self.run_tool(category="requests")["items"]:
            self.assertNotIn("target", row)

    def test_other_types_have_no_target(self):
        self.item(1, optionals={"team_name": "x"})
        self.item(7, optionals={"connection_id": 1})
        for row in self.run_tool(category="requests")["items"]:
            self.assertNotIn("target", row)


class ReportBodyShapeTests(BaseAPITestCase):
    """The model sees a schedule report exactly as before the typed fields (MIP-1235)."""

    # What `list_my_inbox` handed the model for a report before MIP-1207 / MIP-1208.
    OLD_SHAPE = {
        "title": "What to do today",
        "text": "Two things need you.",
        "sections": [
            {
                "heading": "Start with",
                "items": [
                    {
                        "text": "WRD-7 Fix login (Open, High, due 2026-11-09)",
                        "link": "/workspace/x",
                    },
                    {"text": "Reply to Sam"},
                ],
            },
            {"heading": "Calendar", "items": [{"text": "10:00 Standup"}]},
        ],
    }

    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(user_id=str(self.user.id), team_id=str(self.team.team_id))

    def typed_report(self):
        """A report with every typed field a job can fill, stored through the real cleaner."""
        return clean_report(
            {
                "title": "What to do today",
                "summary": "Two things need you.",
                "stats": [{"key": "overdue", "count": 1}, {"key": "due_today", "count": 2}],
                "sections": [
                    {
                        "heading": "Start with",
                        "key": "start_with",
                        "items": [
                            {
                                "text": "WRD-7 Fix login (Open, High, due 2026-11-09)",
                                "link": "/workspace/x",
                                "kind": "task",
                                "display_id": "WRD-7",
                                "title": "Fix login",
                                "status": "Open",
                                "priority": "High",
                                "due_date": "2026-11-09",
                                "overdue_days": 3,
                                "detail": "busy all week",
                                "project": {"id": 4, "name": "Website", "code": "WRD"},
                            },
                            {"text": "Reply to Sam", "kind": "chat"},
                        ],
                    },
                    {
                        "heading": "Calendar",
                        "key": "calendar",
                        "items": [{"text": "10:00 Standup", "kind": "event"}],
                    },
                ],
            },
            "fallback",
        )

    def report_item(self, body, item_type=ITEM_TYPE_GENOS_REPORT):
        return InboxItems.objects.create(
            team=self.team,
            sender=None,
            receiver=self.user,
            item_body=body,
            item_type=item_type,
            item_optionals={
                "kind": "schedule_report",
                "schedule_id": "x",
                "job": "what_to_do_today",
            },
            request_status="",
        )

    def body_seen(self):
        (row,) = LIST_MY_INBOX.run({"include_read": True}, self.ctx)["items"]
        return row["item_body"]

    def test_a_typed_report_is_the_old_shape_exactly(self):
        stored = self.typed_report()
        # The fixture really is the typed one (the test would prove nothing otherwise).
        self.assertEqual(stored["version"], 2)
        self.assertIn("stats", stored)
        self.assertEqual(stored["sections"][0]["key"], "start_with")
        self.assertEqual(stored["sections"][0]["items"][0]["project"]["code"], "WRD")
        self.report_item(stored)
        self.assertEqual(self.body_seen(), self.OLD_SHAPE)

    def test_none_of_the_typed_fields_gets_through_anywhere(self):
        self.report_item(self.typed_report())
        seen = self.body_seen()
        self.assertEqual(set(seen), {"title", "text", "sections"})
        for section in seen["sections"]:
            self.assertEqual(set(section), {"heading", "items"})
            for entry in section["items"]:
                self.assertLessEqual(set(entry), {"text", "link"})

    def test_the_card_still_has_the_typed_fields(self):
        stored = self.typed_report()
        item = self.report_item(stored)
        self.body_seen()
        item.refresh_from_db()
        self.assertEqual(item.item_body, stored)

    def test_a_body_stored_before_the_typed_fields_is_returned_as_it_is(self):
        old = {**self.OLD_SHAPE, "extra_old_field": "kept"}
        self.report_item(old)
        self.assertEqual(self.body_seen(), old)

    def test_other_item_types_are_not_touched_even_with_a_version_in_the_body(self):
        body = {
            "title": "t",
            "text": "x",
            "version": 2,
            "sections": [{"heading": "h", "key": "k", "items": []}],
        }
        for item_type in (0, 6, 9):
            InboxItems.objects.all().delete()
            self.report_item(body, item_type=item_type)
            self.assertEqual(self.body_seen(), body, item_type)

    def test_a_malformed_typed_body_does_not_break_the_tool(self):
        for body in (
            {"version": 2, "title": "t", "text": "x"},
            {"version": 2, "title": "t", "text": "x", "sections": "no"},
            {
                "version": 2,
                "title": "t",
                "text": "x",
                "sections": [5, {"heading": "h", "items": 3}],
            },
            {
                "version": 2,
                "title": "t",
                "text": "x",
                "sections": [{"heading": "h", "items": [None, {"text": "a", "link": ""}]}],
            },
        ):
            InboxItems.objects.all().delete()
            self.report_item(body)
            seen = self.body_seen()
            self.assertEqual(set(seen), {"title", "text", "sections"})
        self.assertEqual(seen["sections"], [{"heading": "h", "items": [{"text": "a"}]}])

    def test_a_string_body_on_a_report_is_returned_as_it_is(self):
        self.report_item("plain")
        self.assertEqual(self.body_seen(), "plain")
