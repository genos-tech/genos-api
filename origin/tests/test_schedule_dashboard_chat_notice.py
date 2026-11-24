"""A dashboard schedule that points at a chat (MIP-2355).

Pinned: the chat gets a SHORT notice (title, one summary line, a `hashNote` chip to the living
note) and none of the report's items; it is refused, in words and before anything is posted, when
a reader of the chat holds no role on the note, and the same run falls back to nothing in the chat
(the refusal is a failed run); a reader with a role gets the notice; the AI summary of a
dashboard needs the operator switch `REPORT_AI_DASHBOARDS` on top of the member's opt-in.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache

from origin.models.common.schedule_models import MemberSchedule
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.services import schedule_delivery
from origin.services.collab_push import PushOutcome
from origin.services.schedule_jobs import ai_dashboard
from origin.tests.test_schedule_chat_delivery import ChatBase
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_VIEWER

TIER_PATH = "origin.search_engine.quota.get_effective_tier"
PUSH = "origin.services.html_notes.push_note_body_now"


class DashboardChatBase(ChatBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        for patcher in (
            mock.patch(TIER_PATH, return_value="core"),
            mock.patch(PUSH, return_value=PushOutcome.SKIPPED),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def dashboard(self, channel):
        return self.save(
            channel,
            job="ai_dashboard",
            name="Project board",
            params={"kind": "project", "project_id": self.project.project_id, "period": "month"},
        )

    def note_of(self, sched):
        sched.refresh_from_db()
        return PersonalNoteMaster.objects.get(note_id=sched.output["note_id"])

    def share(self, note, user):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=user,
            note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_VIEWER,
        )


class TestTheNotice(DashboardChatBase):
    def test_a_chat_with_a_reader_who_cannot_open_the_note_gets_nothing(self):
        sched = self.dashboard(self.pm)
        status, error = self.run_it(sched)
        self.assertEqual(status, "failed")
        self.assertEqual(error, schedule_delivery.NOTE_NOT_SHARED)
        self.assertFalse(self.posted().exists())

    def test_the_refusal_names_nobody(self):
        sched = self.dashboard(self.pm)
        _status, error = self.run_it(sched)
        self.assertNotIn(self.user2.username, error)
        self.assertNotIn(self.user2.email, error)

    def test_once_every_reader_has_a_role_the_notice_is_one_message_with_a_note_chip(self):
        sched = self.dashboard(self.pm)
        self.run_it(sched)  # the first run saves the note and is refused
        note = self.note_of(sched)
        for reader in schedule_readers(self.pm):
            if str(reader) != str(self.user.id):
                self.share(note, type(self.user).objects.get(pk=reader))
        status, error = self.run_it(MemberSchedule.objects.get(pk=sched.pk))
        self.assertEqual((status, error), ("ok", ""))
        (message,) = self.posted()
        kinds = [block["type"] for block in message.body]
        self.assertEqual(kinds, ["heading", "paragraph", "paragraph"])
        chip = message.body[2]["content"][0]
        self.assertEqual(chip["type"], "hashNote")
        self.assertEqual(chip["props"]["noteId"], str(note.note_id))
        self.assertEqual(chip["props"]["noteKind"], "shared")
        self.assertNotIn("bulletListItem", kinds)
        self.assertIsNone(message.sender_id)

    def test_a_chat_only_the_owner_reads_gets_the_notice_with_no_sharing(self):
        alone = self.gm(self.user)
        sched = self.save(
            alone,
            job="ai_dashboard",
            name="Mine",
            params={"kind": "mine", "period": "month"},
        )
        status, error = self.run_it(sched)
        self.assertEqual((status, error), ("ok", ""))
        (message,) = self.posted(alone)
        chip = message.body[2]["content"][0]
        self.assertEqual(chip["props"]["noteKind"], "my")


def schedule_readers(channel):
    from origin.services import schedule_audience  # noqa: PLC0415

    return schedule_audience.readers(channel)


class TestTheOperatorSwitch(DashboardChatBase):
    def run_it_with(self, env: dict[str, str]):
        with mock.patch.dict("os.environ", env):
            with mock.patch("origin.services.report_ai.narrative.narrative_for") as model:
                report = self.run_job("ai_dashboard", kind="mine", ai_sentences=True)
        return report, model

    def test_the_summary_needs_the_dashboard_switch_even_when_the_member_opted_in(self):
        report, model = self.run_it_with(
            {"REPORT_AI_SENTENCES": "true", "REPORT_AI_DASHBOARDS": "false"}
        )
        model.assert_not_called()
        self.assertEqual(report["ai_skipped"], "operator_off")
        self.assertNotIn("narrative", report["dashboard"])

    def test_with_the_switch_on_the_member_opt_in_reaches_the_model(self):
        with mock.patch.dict(
            "os.environ", {"REPORT_AI_SENTENCES": "true", "REPORT_AI_DASHBOARDS": "true"}
        ):
            with mock.patch("origin.services.report_ai.narrative.narrative_for") as model:
                model.return_value = mock.Mock(text="Calm week.", request_id="r1", skipped="")
                report = self.run_job("ai_dashboard", kind="mine", ai_sentences=True)
        model.assert_called_once()
        self.assertEqual(report["dashboard"]["narrative"], "Calm week.")

    def test_a_plain_dashboard_never_looks_at_the_switch(self):
        report, model = self.run_it_with({"REPORT_AI_DASHBOARDS": "false"})
        report, model = self.run_it_with({})
        model.assert_not_called()

    def test_it_is_off_unless_the_operator_turned_it_on(self):
        for value in ("true", "1", "ON"):
            with mock.patch.dict("os.environ", {"REPORT_AI_DASHBOARDS": value}):
                self.assertTrue(ai_dashboard.dashboards_ai_enabled())
        for value in ("", "false", "0"):
            with mock.patch.dict("os.environ", {"REPORT_AI_DASHBOARDS": value}):
                self.assertFalse(ai_dashboard.dashboards_ai_enabled())
