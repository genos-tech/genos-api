"""A schedule's report lands in the member's own Inbox (MIP-1149, item_type 10).

The guarantees, each with a test that fails when it is removed:

* one item per run, owner only, activity (not a request);
* a repeat of one scheduled run files nothing new, a "Run now" always does;
* a report with nothing in it is still delivered;
* a job's output is rebuilt (shape, lengths, counts, links) before it is stored;
* push only when the schedule asked, and a push that fails never fails the run;
* the account's deletion and export cover the report.
"""

import logging
from datetime import timedelta
from unittest import mock

from rest_framework import status

from origin.models.common.inbox_models import (
    ACTIVITY_INBOX_ITEM_TYPES,
    ITEM_TYPE_GENOS_REPORT,
    InboxItems,
)
from origin.models.common.schedule_models import MemberSchedule
from origin.search_engine.agent.tools.list_my_inbox import LIST_MY_INBOX
from origin.services import schedule_delivery, schedules
from origin.services.account_deletion import delete_account
from origin.services.account_export import build_export
from origin.services.schedule_delivery import clean_link, clean_report
from origin.services.schedule_jobs import JOBS, Job, register_job
from origin.services.unread_badge import unread_badge_total
from origin.services.webpush_dispatch import _inbox_title
from origin.tests.test_schedules import NOW, SCHEDULED, ScheduleBase, utc

PUSH = "origin.services.schedule_delivery.dispatch_push_for_inbox_item"


def reports(user=None):
    return InboxItems.objects.filter(
        receiver=user or None, item_type=ITEM_TYPE_GENOS_REPORT
    ).order_by("item_id")


class DeliverTests(ScheduleBase):
    def report(self, **over):
        base = {
            "title": "Overdue",
            "summary": "Two tasks are overdue.",
            "sections": [
                {
                    "heading": "This week",
                    "items": [
                        {"text": "Ship it", "link": "/workspace/tasks/KDS-1"},
                        {"text": "Write it up"},
                    ],
                }
            ],
        }
        base.update(over)
        return base

    def test_one_item_for_the_owner_in_the_schedules_team(self):
        sched = self.make()
        item = schedule_delivery.deliver(sched, self.report())
        self.assertEqual(reports(self.user).count(), 1)
        self.assertEqual(InboxItems.objects.count(), 1)
        self.assertEqual(item.receiver_id, self.user.id)
        self.assertEqual(item.team_id, self.team.team_id)
        self.assertIsNone(item.sender_id)
        self.assertEqual(item.request_status, "")
        self.assertFalse(item.is_read)
        self.assertEqual(item.item_type, 10)
        self.assertEqual(
            item.item_body,
            {
                "version": 2,  # MIP-1208: the typed fields exist; an older body has no version
                "title": "Overdue",
                "text": "Two tasks are overdue.",
                "sections": [
                    {
                        "heading": "This week",
                        "items": [
                            {"text": "Ship it", "link": "/workspace/tasks/KDS-1"},
                            {"text": "Write it up"},
                        ],
                    }
                ],
            },
        )
        self.assertEqual(
            item.item_optionals,
            {"kind": "schedule_report", "schedule_id": str(sched.id), "job": "ok"},
        )

    def test_the_summary_is_kept_on_the_schedule_too(self):
        sched = self.make()
        schedule_delivery.deliver(sched, self.report())
        sched.refresh_from_db()
        self.assertEqual(sched.last_result, {"title": "Overdue", "items": 2})

    def test_it_is_an_activity_not_a_request(self):
        self.assertIn(ITEM_TYPE_GENOS_REPORT, ACTIVITY_INBOX_ITEM_TYPES)

    def test_it_does_not_count_toward_the_requests_badge(self):
        sched = self.make()
        schedule_delivery.deliver(sched, self.report())
        self.assertEqual(unread_badge_total(self.user.id, self.team.team_id), 0)

    def test_the_tick_delivers_through_the_runner(self):
        self.make(due_at=SCHEDULED)
        schedules.run_due(NOW)
        item = reports(self.user).get()
        self.assertEqual(item.item_body["title"], "Your day")
        self.assertEqual(item.item_optionals["run_at"], "2026-11-09T00:00:00+00:00")

    def test_a_schedule_deleted_mid_run_files_nothing_and_is_not_a_failure(self):
        sched = self.make()
        MemberSchedule.objects.filter(pk=sched.pk).delete()
        self.assertIsNone(schedule_delivery.deliver(sched, self.report(), run_at=SCHEDULED))
        self.assertEqual(InboxItems.objects.count(), 0)


class OwnerOnlyTests(ScheduleBase):
    def setUp(self):
        super().setUp()
        sched = self.make()
        self.item = schedule_delivery.deliver(
            sched, {"title": "Mine", "summary": "s", "sections": []}
        )

    def test_the_owner_reads_it_over_the_inbox_endpoint(self):
        self.authenticate(self.user)
        res = self.client.get("/api/v2/inbox/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        items = res.json()["data"]["items"]
        self.assertEqual([i["itemType"] for i in items], [10])
        self.assertEqual(items[0]["itemBody"]["title"], "Mine")

    def test_a_teammate_does_not_see_it(self):
        self.authenticate(self.user2)
        res = self.client.get("/api/v2/inbox/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.json()["data"]["items"], [])
        # Naming the owner is refused, not answered.
        res = self.client.get(
            "/api/v2/inbox/", {"team_id": str(self.team.team_id), "user_id": str(self.user.id)}
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_teammate_cannot_mark_it_read_or_change_it(self):
        self.authenticate(self.user2)
        res = self.client.put(
            "/api/v2/inbox/",
            {"team_id": str(self.team.team_id), "item_id": self.item.item_id, "is_read": True},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.item.refresh_from_db()
        self.assertFalse(self.item.is_read)

    def test_the_owner_can_mark_it_read_but_not_rewrite_it(self):
        self.authenticate(self.user)
        url_body = {"team_id": str(self.team.team_id), "item_id": self.item.item_id}
        ok = self.client.put("/api/v2/inbox/", {**url_body, "is_read": True}, format="json")
        self.assertEqual(ok.status_code, status.HTTP_200_OK)
        forged = self.client.put(
            "/api/v2/inbox/", {**url_body, "item_body": {"title": "forged"}}, format="json"
        )
        self.assertEqual(forged.status_code, status.HTTP_400_BAD_REQUEST)
        self.item.refresh_from_db()
        self.assertTrue(self.item.is_read)
        self.assertEqual(self.item.item_body["title"], "Mine")

    def test_the_agents_inbox_tool_shows_it_to_the_owner_only(self):
        result = LIST_MY_INBOX.run({"category": "activity"}, self.ctx)
        self.assertEqual([i["item_type"] for i in result["items"]], [10])
        other = LIST_MY_INBOX.run({"category": "all", "include_read": True}, self.ctx2)
        self.assertEqual(other["items"], [])

    def test_what_the_agents_inbox_tool_returns_for_the_new_type(self):
        """Documents today's behaviour (the tool is not changed here): the item is an
        activity, its body is returned whole (title, text, sections), and its label is the
        generic `type_10` because the label map does not know the type yet."""
        (row,) = LIST_MY_INBOX.run({"category": "activity"}, self.ctx)["items"]
        self.assertEqual(row["item_type"], 10)
        self.assertEqual(row["item_type_label"], "type_10")
        self.assertIsNone(row["request_status"])
        self.assertIsNone(row["sender_id"])
        self.assertEqual(row["item_body"]["title"], "Mine")
        self.assertEqual(row["item_body"]["sections"], [])
        self.assertEqual(
            LIST_MY_INBOX.run({"category": "requests"}, self.ctx)["items"],
            [],
        )


class IdempotenceTests(ScheduleBase):
    REPORT = {
        "title": "T",
        "summary": "s",
        "sections": [{"heading": "h", "items": [{"text": "a"}]}],
    }

    def test_a_repeat_of_one_scheduled_run_files_one_item(self):
        sched = self.make()
        first = schedule_delivery.deliver(sched, self.REPORT, run_at=SCHEDULED)
        again = schedule_delivery.deliver(sched, self.REPORT, run_at=SCHEDULED)
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertEqual(reports(self.user).count(), 1)

    def test_the_same_instant_in_another_zone_is_the_same_run(self):
        from zoneinfo import ZoneInfo

        sched = self.make()
        schedule_delivery.deliver(sched, self.REPORT, run_at=SCHEDULED)
        again = schedule_delivery.deliver(
            sched, self.REPORT, run_at=SCHEDULED.astimezone(ZoneInfo("Asia/Tokyo"))
        )
        self.assertIsNone(again)

    def test_the_next_scheduled_run_is_a_new_item(self):
        sched = self.make()
        schedule_delivery.deliver(sched, self.REPORT, run_at=SCHEDULED)
        schedule_delivery.deliver(sched, self.REPORT, run_at=SCHEDULED + timedelta(days=1))
        self.assertEqual(reports(self.user).count(), 2)

    def test_two_schedules_at_one_time_are_two_items(self):
        a, b = self.make(name="A"), self.make(name="B", time_of_day="09:30")
        schedule_delivery.deliver(a, self.REPORT, run_at=SCHEDULED)
        schedule_delivery.deliver(b, self.REPORT, run_at=SCHEDULED)
        self.assertEqual(reports(self.user).count(), 2)

    def test_a_report_the_member_deleted_does_not_come_back(self):
        sched = self.make()
        item = schedule_delivery.deliver(sched, self.REPORT, run_at=SCHEDULED)
        InboxItems.objects.filter(pk=item.pk).update(is_deleted=True)
        self.assertIsNone(schedule_delivery.deliver(sched, self.REPORT, run_at=SCHEDULED))
        self.assertEqual(reports(self.user).count(), 1)

    def test_run_now_is_its_own_report_every_time(self):
        sched = self.make(due_at=utc(2030, 1, 1))
        schedules.run_now(self.ctx, sched.pk, now=NOW)
        # A second press inside the cooldown is refused (MIP-1151); a minute later it is a
        # report of its own.
        schedules.run_now(self.ctx, sched.pk, now=NOW + schedules.RUN_NOW_COOLDOWN)
        self.assertEqual(reports(self.user).count(), 2)

    def test_run_now_does_not_use_up_the_scheduled_run(self):
        sched = self.make(due_at=SCHEDULED)
        schedules.run_now(self.ctx, sched.pk)
        schedules.run_due(NOW)
        self.assertEqual(reports(self.user).count(), 2)

    def test_run_now_on_a_paused_schedule_is_not_swallowed(self):
        sched = self.make()
        schedules.set_enabled(self.ctx, sched.pk, False)
        schedules.run_now(self.ctx, sched.pk, now=NOW)
        # A second press inside the cooldown is refused (MIP-1151); a minute later it is a
        # report of its own.
        schedules.run_now(self.ctx, sched.pk, now=NOW + schedules.RUN_NOW_COOLDOWN)
        self.assertEqual(reports(self.user).count(), 2)

    def test_a_tick_that_died_mid_run_and_is_taken_over_files_one_item(self):
        sched = self.make(due_at=SCHEDULED)
        schedules.run_due(NOW)
        self.assertEqual(len(self.calls), 1)
        # The first tick died after delivering, before it recorded the outcome: the row is
        # still due and holds a claim that is now stale.
        MemberSchedule.objects.filter(pk=sched.pk).update(
            next_run_at=SCHEDULED, claimed_at=NOW - timedelta(minutes=20)
        )
        later = NOW + timedelta(minutes=5)
        summary = schedules.run_due(later)
        self.assertEqual(summary.ran, 1)
        self.assertEqual(len(self.calls), 2, "the job ran a second time")
        self.assertEqual(reports(self.user).count(), 1, "but the report was filed once")

    def test_a_repeat_sends_no_second_push(self):
        sched = self.make(deliver_push=True)
        with mock.patch(PUSH) as push, self.captureOnCommitCallbacks(execute=True):
            schedule_delivery.deliver(sched, self.REPORT, run_at=SCHEDULED)
            schedule_delivery.deliver(sched, self.REPORT, run_at=SCHEDULED)
        self.assertEqual(push.call_count, 1)


class EmptyReportTests(ScheduleBase):
    def test_a_report_with_no_items_is_still_delivered(self):
        sched = self.make()
        report = {"title": "Overdue", "summary": "Nothing is overdue.", "sections": []}
        item = schedule_delivery.deliver(sched, report, run_at=SCHEDULED)
        self.assertEqual(item.item_body["text"], "Nothing is overdue.")
        self.assertEqual(item.item_body["sections"], [])
        sched.refresh_from_db()
        self.assertEqual(sched.last_result, {"title": "Overdue", "items": 0})

    def test_a_report_with_no_summary_or_title_gets_a_default(self):
        sched = self.make(name="Monday catch-up")
        item = schedule_delivery.deliver(sched, {"title": "", "summary": "", "sections": []})
        self.assertEqual(item.item_body["title"], "Monday catch-up")
        self.assertEqual(item.item_body["text"], "Nothing to report.")

    def test_a_section_left_with_no_items_is_not_kept(self):
        body = clean_report(
            {"title": "t", "summary": "s", "sections": [{"heading": "h", "items": []}]}, "n"
        )
        self.assertEqual(body["sections"], [])


class PushTests(ScheduleBase):
    REPORT = {"title": "Overdue", "summary": "Two tasks.", "sections": []}

    def test_a_new_schedule_pushes_by_default(self):
        # MIP-1374: a report nobody is told about goes unread.
        sched = self.make()
        self.assertTrue(sched.deliver_push)
        with mock.patch(PUSH) as push, self.captureOnCommitCallbacks(execute=True):
            item = schedule_delivery.deliver(sched, self.REPORT)
        push.assert_called_once_with(item, title="Overdue", body="Two tasks.")

    def test_no_push_when_the_member_switched_it_off(self):
        sched = self.make(deliver_push=False)
        self.assertFalse(sched.deliver_push)
        with mock.patch(PUSH) as push, self.captureOnCommitCallbacks(execute=True):
            schedule_delivery.deliver(sched, self.REPORT)
        push.assert_not_called()

    def test_a_push_goes_out_when_the_schedule_asked(self):
        sched = self.make(deliver_push=True)
        with mock.patch(PUSH) as push, self.captureOnCommitCallbacks(execute=True):
            item = schedule_delivery.deliver(sched, self.REPORT)
        push.assert_called_once_with(item, title="Overdue", body="Two tasks.")

    def test_the_push_waits_for_the_commit(self):
        sched = self.make(deliver_push=True)
        with mock.patch(PUSH) as push, self.captureOnCommitCallbacks(execute=False) as callbacks:
            schedule_delivery.deliver(sched, self.REPORT)
            push.assert_not_called()
        self.assertEqual(len(callbacks), 1)

    def test_the_real_dispatcher_queues_a_card_with_the_headline_and_summary(self):
        from origin.services import webpush_dispatch

        sched = self.make(deliver_push=True)
        with (
            mock.patch.object(webpush_dispatch, "vapid_configured", return_value=True),
            mock.patch.object(webpush_dispatch, "_queue_push") as queue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            schedule_delivery.deliver(sched, self.REPORT)
        kwargs = queue.call_args.kwargs
        self.assertEqual(kwargs["title"], "Overdue")
        self.assertEqual(kwargs["body"], "Two tasks.")
        self.assertEqual(kwargs["category"], "inbox")
        self.assertEqual(kwargs["recipient_id"], self.user.id)
        self.assertEqual(kwargs["url"], "/workspace/inbox")

    def test_a_failing_push_does_not_fail_the_run_and_logs_a_warning(self):
        sched = self.make(deliver_push=True, due_at=SCHEDULED)
        with (
            mock.patch(PUSH, side_effect=RuntimeError("push gateway said: secret body")),
            self.assertLogs("origin.schedules", level="WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            summary = schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual((summary.ran, summary.failed), (1, 0))
        self.assertEqual(sched.last_status, "ok")
        self.assertEqual(sched.consecutive_failures, 0)
        self.assertEqual(reports(self.user).count(), 1, "the report is in the Inbox")
        levels = {r.levelno for r in logs.records}
        self.assertEqual(levels, {logging.WARNING})
        self.assertNotIn("secret body", "\n".join(logs.output))

    def test_a_push_that_always_fails_never_pauses_the_schedule(self):
        sched = self.make(deliver_push=True)
        with (
            mock.patch(PUSH, side_effect=RuntimeError("down")),
            self.captureOnCommitCallbacks(execute=True),
        ):
            for day in range(5):
                MemberSchedule.objects.filter(pk=sched.pk).update(
                    next_run_at=SCHEDULED + timedelta(days=day), claimed_at=None
                )
                schedules.run_due(NOW + timedelta(days=day))
        sched.refresh_from_db()
        self.assertTrue(sched.enabled)
        self.assertEqual(sched.consecutive_failures, 0)
        self.assertEqual(reports(self.user).count(), 5)

    def test_a_failing_item_write_is_still_a_failed_run(self):
        # The one thing that may fail a run: the member got nothing.
        sched = self.make(due_at=SCHEDULED)
        with (
            mock.patch.object(InboxItems.objects, "create", side_effect=RuntimeError("db")),
            self.assertLogs("origin.schedules", level="WARNING"),
        ):
            summary = schedules.run_due(NOW)
        sched.refresh_from_db()
        self.assertEqual((summary.ran, summary.failed), (0, 1))
        self.assertEqual(reports(self.user).count(), 0)


class SanitisingTests(ScheduleBase):
    def test_counts_are_capped(self):
        sections = [
            {"heading": f"S{n}", "items": [{"text": f"i{m}"} for m in range(100)]}
            for n in range(40)
        ]
        body = clean_report({"title": "t", "summary": "s", "sections": sections}, "n")
        self.assertEqual(len(body["sections"]), 12)
        self.assertTrue(all(len(s["items"]) == 30 for s in body["sections"]))

    def test_lengths_are_capped(self):
        report = {
            "title": "T" * 500,
            "summary": "S" * 900,
            "sections": [{"heading": "H" * 400, "items": [{"text": "x" * 1000}]}],
        }
        body = clean_report(report, "n")
        self.assertLessEqual(len(body["title"]), 200)
        self.assertLessEqual(len(body["text"]), 280)
        self.assertLessEqual(len(body["sections"][0]["heading"]), 120)
        self.assertLessEqual(len(body["sections"][0]["items"][0]["text"]), 240)

    def test_the_stored_item_is_bounded(self):
        sched = self.make()
        sections = [
            {
                "heading": "H" * 400,
                "items": [{"text": "x" * 1000, "link": "/workspace/" + "a" * 800}] * 100,
            }
        ] * 40
        item = schedule_delivery.deliver(
            sched, {"title": "t", "summary": "s", "sections": sections}
        )
        import json

        self.assertLess(len(json.dumps(item.item_body)), 100_000)
        sched.refresh_from_db()
        self.assertEqual(sched.last_result["items"], 12 * 30)

    def test_text_is_one_plain_line(self):
        body = clean_report(
            {
                "title": "a\nb‮title",
                "summary": "line1\r\nline2",
                "sections": [{"heading": "h", "items": [{"text": "  x\t\ty \x00 z  "}]}],
            },
            "n",
        )
        self.assertEqual(body["title"], "a b" + "title")  # newline is a space; the override is gone
        self.assertEqual(body["text"], "line1 line2")
        self.assertEqual(body["sections"][0]["items"][0]["text"], "x y z")

    def test_wrong_types_are_dropped_not_stored(self):
        body = clean_report(
            {
                "title": 5,
                "summary": ["x"],
                "sections": [
                    "nope",
                    {"heading": 3, "items": "nope"},
                    {
                        "heading": None,
                        "items": [None, 7, {"text": 9}, {"text": ""}, {"text": "ok", "link": 5}],
                    },
                    {"items": [{"text": "kept"}]},
                ],
            },
            "Fallback",
        )
        self.assertEqual(body["title"], "Fallback")
        self.assertEqual(body["text"], "Nothing to report.")
        self.assertEqual(
            body["sections"],
            [
                {"heading": "", "items": [{"text": "ok"}]},
                {"heading": "", "items": [{"text": "kept"}]},
            ],
        )

    def test_links_are_app_paths_or_https_only(self):
        good = [
            "/workspace/tasks/KDS-1",
            "/workspace/notes/1?x=1#y",
            "https://example.com/a?b=1",
            "https://sub.example.com",
        ]
        bad = [
            "",
            None,
            5,
            "/x",
            "/workspace",
            "/workspaces/x",
            "//evil.com/workspace/x",
            "http://example.com",
            "javascript:alert(1)",
            "JAVASCRIPT:alert(1)",
            "data:text/html,x",
            "https://",
            "https:///path",
            "https://user:pw@example.com",
            "https://example.com/a b",
            "https://example.com/\n",
            "/workspace/../admin",
            "/workspace/a/../../b",
            "/workspace/%2e%2e/admin",
            "/workspace/%2E%2E/admin",
            "/workspace/.%2e/admin",
            "/workspace/a%2f..%2fb",
            "/workspace/a%5cb",
            "/workspace/\\evil",
            "/workspace/a\tb",
            "/workspace/" + "a" * 600,
            "mailto:a@b.c",
            "ftp://example.com",
        ]
        for link in good:
            self.assertEqual(clean_link(link), link, link)
        for link in bad:
            self.assertEqual(clean_link(link), "", repr(link))

    def test_a_bad_link_drops_the_link_but_keeps_the_item(self):
        body = clean_report(
            {
                "title": "t",
                "summary": "s",
                "sections": [{"heading": "h", "items": [{"text": "x", "link": "javascript:1"}]}],
            },
            "n",
        )
        self.assertEqual(body["sections"][0]["items"], [{"text": "x"}])


class AccountTests(ScheduleBase):
    def setUp(self):
        super().setUp()
        self.mine = schedule_delivery.deliver(
            self.make(name="mine"),
            {
                "title": "Mine",
                "summary": "s",
                "sections": [{"heading": "h", "items": [{"text": "secret task"}]}],
            },
        )
        self.theirs = schedule_delivery.deliver(
            self.make(name="theirs", ctx=self.ctx2),
            {"title": "Theirs", "summary": "s", "sections": []},
        )

    def test_deleting_the_account_erases_their_reports_and_only_theirs(self):
        delete_account(self.user2)
        self.assertFalse(InboxItems.objects.filter(pk=self.theirs.pk).exists())
        self.assertTrue(InboxItems.objects.filter(pk=self.mine.pk).exists())

    def test_other_inbox_items_of_that_member_are_left_to_the_existing_rules(self):
        notice = InboxItems.objects.create(
            team=self.team, sender=self.user, receiver=self.user2, item_type=0, item_body={}
        )
        delete_account(self.user2)
        self.assertTrue(InboxItems.objects.filter(pk=notice.pk).exists())

    def test_the_export_includes_their_reports_and_not_anyone_elses(self):
        doc = build_export(self.user)
        self.assertEqual(doc["counts"]["schedule_reports"], 1)
        row = doc["schedule_reports"][0]
        self.assertEqual(row["title"], "Mine")
        self.assertEqual(row["sections"][0]["items"][0]["text"], "secret task")
        self.assertEqual(row["team_id"], str(self.team.team_id))
        self.assertNotIn("Theirs", str(doc["schedule_reports"]))

    def test_a_report_the_member_deleted_is_not_exported(self):
        InboxItems.objects.filter(pk=self.mine.pk).update(is_deleted=True)
        self.assertEqual(build_export(self.user)["counts"]["schedule_reports"], 0)


class TitleTests(ScheduleBase):
    def test_the_push_title_is_the_reports_headline(self):
        item = schedule_delivery.deliver(
            self.make(), {"title": "Overdue", "summary": "s", "sections": []}
        )
        self.assertEqual(_inbox_title(item, "Someone"), "Overdue")
        item.item_body = None
        self.assertEqual(_inbox_title(item, "Someone"), "Your Genos report")


class JobsRegistryUntouched(ScheduleBase):
    def test_registering_a_job_still_works_with_the_new_hook(self):
        register_job(Job("another", "A", "d", self._ok))
        self.assertIn("another", JOBS)
