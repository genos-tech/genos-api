"""`html_notes.save_page`, the create/replace core, called WITHOUT MCP (MIP-2352).

The MCP tool `save_html_note` is a thin wrapper over this function (its own behaviour is pinned in
`test_mcp_save_html_note.py`); here the function is called with a `ToolContext` and nothing else, as
a dashboard or any other server-side caller will. Pinned: a plan without HTML notes stores nothing,
a create and a replace store the page, the plan's rate limit and the monthly note quota hold, a
member without write access is refused, and the `HTML_NOTES` switch belongs to the MCP wrapper only.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from origin.models.note.html_note_models import NoteHtmlRevision
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.services import html_note_limits, html_notes
from origin.services.collab_push import PushOutcome
from origin.tests.test_base import BaseAPITestCase

TIER_PATH = "origin.search_engine.quota.get_effective_tier"
PUSH = "origin.services.html_notes.push_note_body_now"
GOOD = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Sprint burndown</title>"
    "</head><body><h1>Sprint burndown</h1><p>Velocity dropped.</p></body></html>"
)
CORE = html_note_limits.HTML_NOTE_LIMITS["core"]


class SavePageBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        patcher = mock.patch(TIER_PATH, return_value="core")
        self.tier = patcher.start()
        self.addCleanup(patcher.stop)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def args(self, **extra):
        out = {"html": GOOD, "note_type": "personal", "title": "Burndown", "summary": "A chart."}
        out.update(extra)
        return out


class SavePageCreates(SavePageBase):
    def test_a_page_is_stored_as_a_new_note_with_its_first_revision(self):
        out = html_notes.save_page(self.args(), self.ctx)
        self.assertTrue(out["created"])
        self.assertEqual((out["note_type"], out["html_rev"]), ("personal", 1))
        note = PersonalNoteMaster.objects.get(note_id=out["note_id"])
        self.assertEqual(note.body_format, html_notes.BODY_FORMAT_HTML)
        self.assertEqual(
            NoteHtmlRevision.objects.filter(
                note_type=html_notes.NOTE_TYPE_PERSONAL, note_id=note.note_id
            ).count(),
            1,
        )

    def test_the_switch_is_not_checked_here(self):
        with override_settings(HTML_NOTES=False):
            out = html_notes.save_page(self.args(), self.ctx)
        self.assertTrue(out["created"])

    def test_a_free_plan_stores_nothing_and_gets_a_suggestion(self):
        self.tier.return_value = "free"
        out = html_notes.save_page(self.args(), self.ctx)
        self.assertNotIn("note_id", out)
        self.assertEqual(PersonalNoteMaster.objects.count(), 0)

    def test_the_hourly_rate_limit_refuses_with_what_to_do(self):
        with mock.patch.object(
            html_notes, "check_rate", side_effect=html_notes.HtmlNoteRefused("slow down")
        ):
            with self.assertRaisesMessage(ToolError, "slow down"):
                html_notes.save_page(self.args(), self.ctx)
        self.assertEqual(PersonalNoteMaster.objects.count(), 0)

    def test_the_monthly_note_quota_refuses_a_new_page(self):
        with mock.patch(
            "origin.services.html_notes.check_remaining_monthly", return_value=(False, 5, 5)
        ):
            with self.assertRaisesMessage(ToolError, "all 5 notes"):
                html_notes.save_page(self.args(), self.ctx)
        self.assertEqual(PersonalNoteMaster.objects.count(), 0)

    def test_a_page_over_the_plan_size_is_refused(self):
        big = GOOD + "<!--" + "x" * (CORE.max_bytes + 10) + "-->"
        with self.assertRaises(ToolError):
            html_notes.save_page(self.args(html=big), self.ctx)
        self.assertEqual(PersonalNoteMaster.objects.count(), 0)


class SavePageReplaces(SavePageBase):
    def test_a_replace_adds_a_revision_and_pushes_to_the_live_editor(self):
        made = html_notes.save_page(self.args(), self.ctx)
        with mock.patch(PUSH, return_value=PushOutcome.SENT) as push:
            out = html_notes.save_page(
                self.args(html=GOOD + "<!-- v2 -->", note_id=made["note_id"]), self.ctx
            )
        self.assertFalse(out["created"])
        self.assertEqual(out["html_rev"], 2)
        self.assertEqual(out["live_editor"], PushOutcome.SENT.value)
        push.assert_called_once()

    def test_a_failed_push_is_said_but_the_page_is_saved(self):
        made = html_notes.save_page(self.args(), self.ctx)
        with mock.patch(PUSH, return_value=PushOutcome.FAILED):
            out = html_notes.save_page(
                self.args(html=GOOD + "<!-- v2 -->", note_id=made["note_id"]), self.ctx
            )
        self.assertIn("live editor could not be updated", out["__summary__"])

    def test_someone_without_write_access_cannot_replace(self):
        made = html_notes.save_page(self.args(), self.ctx)
        other = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaises((ToolError, ToolForbidden)):
            html_notes.save_page(self.args(note_id=made["note_id"]), other)
        self.assertEqual(NoteHtmlRevision.objects.filter(note_id=made["note_id"]).count(), 1)
