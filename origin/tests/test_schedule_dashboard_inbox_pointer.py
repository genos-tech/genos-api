"""The Inbox item of a dashboard run is only a pointer to its page (MIP-2419).

Pinned: the item is the dashboard's name and one "Dashboard page" link, with no summary, numbers,
lists or written text; a push says "Dashboard page"; the cost and "no AI credits" facts stay on
the item; a chat's notice is what it was (title, the one-line summary, a chip); a dashboard run
that saved no page is filed as the report it is.
"""

from __future__ import annotations

from unittest import mock

from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.services import schedule_delivery, schedules
from origin.services.report_ai import llm
from origin.tests.test_dashboard_end_to_end import NARRATIVE, DashboardFlow
from origin.tests.test_schedule_dashboard_skill import DashBase

PUSH_ITEM = "origin.services.schedule_delivery.dispatch_push_for_inbox_item"


def pointer(title, note_id, section="my"):
    return {
        "version": 2,
        "title": title,
        "text": "Dashboard page",
        "sections": [
            {
                "heading": "",
                "items": [
                    {
                        "text": "Dashboard page",
                        "link": f"/workspace/notes/{section}/{note_id}",
                        "kind": "note",
                    }
                ],
            }
        ],
    }


def latest_item(user):
    return InboxItems.objects.filter(receiver=user, item_type=ITEM_TYPE_GENOS_REPORT).latest(
        "item_id"
    )


class InboxPointer(DashboardFlow):
    def inbox_dashboard(self, **params):
        return schedules.create(
            self.ctx,
            name="My board",
            job="ai_dashboard",
            params={
                "kind": "project",
                "project_id": self.project.project_id,
                "period": "month",
                **params,
            },
            frequency="daily",
            time_of_day="09:00",
        )

    def test_the_item_is_the_name_and_one_link_and_the_push_says_dashboard_page(self):
        sched = self.inbox_dashboard()
        with mock.patch(PUSH_ITEM) as push, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.run_it(sched), ("ok", ""))
        note = self.note_of(sched)
        item = latest_item(self.user)
        self.assertEqual(item.item_body, pointer(note.title, note.note_id))
        self.assertEqual(push.call_args.kwargs, {"title": note.title, "body": "Dashboard page"})
        self.assertNotIn("ai", item.item_optionals)
        sched.refresh_from_db()
        self.assertEqual(sched.last_result, {"title": note.title, "items": 1})

    def test_an_ai_run_keeps_its_cost_and_drops_the_written_summary(self):
        sched = self.inbox_dashboard(ai_sentences=True)
        self.assertEqual(self.run_it(sched), ("ok", ""))
        note = self.note_of(sched)
        self.assertIn(NARRATIVE, self.page_of(note), "the page still has the summary")
        item = latest_item(self.user)
        self.assertEqual(item.item_body, pointer(note.title, note.note_id))
        self.assertNotIn(NARRATIVE, str(item.item_body))
        self.assertIn("credits_milli", item.item_optionals["ai"])

    def test_a_run_with_no_credits_still_says_why_there_is_no_ai_part(self):
        sched = self.inbox_dashboard(ai_sentences=True)
        with mock.patch.object(llm, "credit_block", return_value=llm.SKIP_NO_CREDITS):
            self.assertEqual(self.run_it(sched), ("ok", ""))
        note = self.note_of(sched)
        item = latest_item(self.user)
        self.assertEqual(item.item_body, pointer(note.title, note.note_id))
        self.assertEqual(item.item_optionals["ai_skipped"], llm.SKIP_NO_CREDITS)

    def test_a_run_the_chat_refused_is_filed_as_the_pointer(self):
        sched = self.dashboard()  # points at the project chat; nobody has the note yet
        self.assertEqual(self.run_it(sched)[0], "ok")
        note = self.note_of(sched)
        self.assertEqual(latest_item(self.user).item_body, pointer(note.title, note.note_id))

    def test_the_chat_notice_still_carries_the_summary_line(self):
        sched = self.dashboard()
        self.run_it(sched)
        self.share_with_readers(self.note_of(sched))
        with mock.patch.object(
            schedule_delivery, "clean_report", wraps=schedule_delivery.clean_report
        ) as cleaned:
            self.assertEqual(self.run_it(type(sched).objects.get(pk=sched.pk)), ("ok", ""))
        report, fallback = cleaned.call_args.args
        whole = schedule_delivery.clean_report(report, fallback)
        note = self.note_of(sched)
        (message,) = self.posted()
        self.assertEqual(
            message.body,
            schedule_delivery.notice_blocks(whole, note.note_id, note_kind="shared"),
        )
        line = message.body[1]["content"][0]["text"]
        self.assertIn(NARRATIVE, line)
        self.assertNotEqual(line, "Dashboard page")


class SkillDashboardPointer(DashBase):
    def test_a_skill_dashboard_files_the_same_pointer(self):
        sched = self.dash(self.skill())
        self.assertEqual(schedules._execute(sched), ("ok", ""))
        note = self.notes().get()
        item = latest_item(self.user)
        self.assertEqual(item.item_body, pointer(note.title, note.note_id))
        self.assertIn("ai", item.item_optionals)

    def test_a_run_that_saved_no_page_keeps_its_own_words(self):
        sched = self.dash(self.skill())
        with mock.patch.object(llm, "credit_block", return_value=llm.SKIP_NO_CREDITS):
            self.assertEqual(schedules._execute(sched), ("ok", ""))
        self.assertFalse(PersonalNoteMaster.objects.filter(body_format="html").exists())
        body = latest_item(self.user).item_body
        self.assertNotEqual(body["text"], "Dashboard page")
        self.assertEqual(body["sections"], [])
