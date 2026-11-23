"""HTML notes by plan (MIP-2216): Core and up only, with the limits in ONE table.

What is pinned:

- the table: Free has no row, Core is 512 KB / 100 notes / 30 saves an hour, Pro, Max and Enterprise
  are 1 MB / 500 / 60, and the derived ceiling is the largest page any plan has;
- Free: `save_html_note` stores NOTHING (no note, no page, no revision, no save used) and answers with
  a suggestion that is not an error, for a create, a replace and an oversize page alike;
- each limit per tier: the size (at the limit saves, one byte over is refused with the numbers and the
  fix, and the higher tier takes the same page), the count of HTML notes (counted per member, per
  HTML note only, a replace is not a new note), the saves per hour;
- a refused save uses no save from the hour; a failed plan lookup reads as Core, never as an error;
- a downgrade deletes nothing and reads still work (MCP `get_note` here, the REST read in
  `test_html_notes_api`), only new saves are held to the new plan.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache

from origin.models.note.html_note_models import NoteHtmlRevision
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.services import html_note_limits, html_notes
from origin.tests.test_mcp_save_html_note import (
    CORE,
    GOOD,
    PRO,
    SAVE,
    TIER_PATH,
    HtmlBase,
)


def page_of(size: int) -> str:
    """A valid page of exactly `size` bytes."""
    filler = "x" * (size - len(GOOD.encode()) - 7)
    page = GOOD.replace("</body>", f"<!--{filler}--></body>")
    assert len(page.encode()) == size
    return page


def own_html_notes(team, user, count: int) -> None:
    """`count` HTML notes owned by `user`, made straight in the table (the count is what is tested)."""
    PersonalNoteMaster.objects.bulk_create(
        PersonalNoteMaster(
            team=team,
            owner=user,
            title=f"Page {i}",
            body=[],
            body_format=html_notes.BODY_FORMAT_HTML,
        )
        for i in range(count)
    )


class TheTable(HtmlBase):
    def test_the_rows(self):
        table = html_note_limits.HTML_NOTE_LIMITS
        self.assertIsNone(table["free"])
        self.assertEqual(
            (CORE.max_bytes, CORE.max_notes, CORE.saves_per_hour), (512 * 1024, 100, 30)
        )
        for tier in ("pro", "max", "enterprise"):
            row = table[tier]
            self.assertEqual(
                (row.max_bytes, row.max_notes, row.saves_per_hour), (1024 * 1024, 500, 60), tier
            )

    def test_every_tier_the_plans_know_has_a_row(self):
        from origin.search_engine import quota

        self.assertEqual(set(html_note_limits.HTML_NOTE_LIMITS), set(quota._TIER_RANK))  # noqa: SLF001

    def test_the_ceiling_is_the_largest_row(self):
        self.assertEqual(html_note_limits.MAX_PAGE_BYTES, PRO.max_bytes)
        self.assertEqual(html_notes.MAX_HTML_BYTES, PRO.max_bytes)

    def test_an_unknown_tier_reads_as_not_on_the_plan(self):
        self.assertIsNone(html_note_limits.limits_for_tier("platinum"))

    def test_a_failed_plan_lookup_reads_as_core(self):
        with mock.patch(TIER_PATH, side_effect=RuntimeError("down")):
            self.assertEqual(html_note_limits.limits_for("u"), CORE)
        self.tier.side_effect = RuntimeError("down")
        made = self.save()  # the member is never blocked by our failure
        self.assertTrue(made["created"])


class FreeStoresNothing(HtmlBase):
    def setUp(self):
        super().setUp()
        self.tier.return_value = "free"

    def assert_nothing_stored(self):
        self.assertEqual(PersonalNoteMaster.objects.count(), 0)
        self.assertEqual(NoteHtmlRevision.objects.count(), 0)

    def test_a_create_is_a_suggestion_not_an_error(self):
        out = self.ok(SAVE, html=GOOD, note_type="personal", title="t", summary="s")
        self.assertIs(out["saved"], False)
        self.assertIs(out["created"], False)
        self.assertEqual(out["plan_required"], "core")
        self.assertEqual(out["your_plan"], "free")
        self.assert_nothing_stored()

    def test_the_reply_says_it_is_a_core_feature_and_what_to_do(self):
        out = self.ok(SAVE, html=GOOD, note_type="personal", title="t", summary="s")
        self.assertIn("Core plan feature", out["message"])
        self.assertIn("upgrade", out["message"])
        self.assertIn("Plan & Usage", out["message"])
        self.assertIn("create_note", out["next_step"])
        self.assertIn("Nothing was saved", out["next_step"])

    def test_a_replace_stores_nothing_either(self):
        self.tier.return_value = "core"
        note = self.html_note()
        revisions = NoteHtmlRevision.objects.count()
        self.tier.return_value = "free"
        out = self.ok(
            SAVE, html=GOOD + "<!--new-->", note_id=note.note_id, note_type="personal", summary="s"
        )
        self.assertIs(out["saved"], False)
        self.assertEqual(NoteHtmlRevision.objects.count(), revisions)

    def test_an_oversize_page_gets_the_plan_answer_not_a_size_error(self):
        out = self.ok(SAVE, html="x" * (PRO.max_bytes + 1), note_type="personal", title="t")
        self.assertEqual(out["plan_required"], "core")
        self.assert_nothing_stored()

    def test_it_never_reaches_the_scan_or_uses_a_save(self):
        with mock.patch("origin.services.html_notes.scan_html") as scan:
            for _ in range(CORE.saves_per_hour + 5):
                self.ok(SAVE, html=GOOD, note_type="personal", title="t", summary="s")
        scan.assert_not_called()
        self.tier.return_value = "core"
        for _ in range(CORE.saves_per_hour):
            self.save()  # the Free calls used none of the hour

    def test_upgrading_makes_the_same_call_work(self):
        self.ok(SAVE, html=GOOD, note_type="personal", title="t", summary="s")
        self.tier.return_value = "core"
        made = self.save()
        self.assertTrue(made["created"])


class SizePerTier(HtmlBase):
    def test_core_saves_exactly_512_kb_and_refuses_one_byte_more(self):
        made = self.save(page_of(CORE.max_bytes))
        self.assertEqual(made["size_bytes"], CORE.max_bytes)
        text = self.refused(SAVE, html=page_of(CORE.max_bytes + 1), note_type="personal", title="t")
        self.assertIn("512 KB", text)
        self.assertIn(f"{CORE.max_bytes:,}", text)
        self.assertIn("Nothing was saved", text)
        self.assertIn("upgrade", text)
        self.assertEqual(PersonalNoteMaster.objects.count(), 1)

    def test_the_same_page_that_core_refuses_pro_takes(self):
        big = page_of(CORE.max_bytes + 1)
        self.refused(SAVE, html=big, note_type="personal", title="t")
        self.tier.return_value = "pro"
        made = self.save(big)
        self.assertEqual(made["size_bytes"], CORE.max_bytes + 1)

    def test_pro_and_up_save_exactly_1_mb_and_refuse_one_byte_more(self):
        for tier in ("pro", "max", "enterprise"):
            self.tier.return_value = tier
            made = self.save(page_of(PRO.max_bytes))
            self.assertEqual(made["size_bytes"], PRO.max_bytes, tier)
            text = self.refused(
                SAVE, html=page_of(PRO.max_bytes + 1), note_type="personal", title="t"
            )
            self.assertIn("1024 KB", text)
            self.assertIn(f"{PRO.max_bytes:,}", text)

    def test_a_replace_is_held_to_the_size_too(self):
        note = self.html_note()
        text = self.refused(
            SAVE,
            html=page_of(CORE.max_bytes + 1),
            note_id=note.note_id,
            note_type="personal",
            summary="s",
        )
        self.assertIn("512 KB", text)
        self.assertEqual(NoteHtmlRevision.objects.count(), 1)

    def test_an_oversize_page_never_reaches_the_scan(self):
        with mock.patch("origin.services.html_notes.scan_html") as scan:
            self.refused(SAVE, html=page_of(CORE.max_bytes + 1), note_type="personal", title="t")
        scan.assert_not_called()


class CountPerTier(HtmlBase):
    def test_core_may_own_100_and_the_101st_is_refused_with_the_fix(self):
        own_html_notes(self.team, self.user, CORE.max_notes - 1)
        self.assertTrue(self.save()["created"])  # the 100th
        text = self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        self.assertIn("100", text)
        self.assertIn("Delete", text)
        self.assertIn("note_id", text)
        self.assertIn("Nothing was saved", text)
        self.assertEqual(html_note_limits.count_owned(self.user.id), CORE.max_notes)

    def test_pro_and_up_may_own_500(self):
        own_html_notes(self.team, self.user, CORE.max_notes)
        self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        self.tier.return_value = "pro"
        self.assertTrue(self.save()["created"])  # 101st: Core said no, Pro says yes
        own_html_notes(self.team, self.user, PRO.max_notes - CORE.max_notes - 1)
        text = self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        self.assertIn("500", text)

    def test_a_replace_is_not_a_new_note_so_it_works_at_the_limit(self):
        own_html_notes(self.team, self.user, CORE.max_notes - 1)
        note = self.html_note()
        self.refused(SAVE, html=GOOD, note_type="personal", title="t")  # full
        out = self.ok(
            SAVE, html=GOOD + "<!--v2-->", note_id=note.note_id, note_type="personal", summary="s"
        )
        self.assertFalse(out["created"])
        self.assertEqual(out["html_rev"], 2)

    def test_ordinary_notes_and_other_members_notes_do_not_count(self):
        PersonalNoteMaster.objects.bulk_create(
            PersonalNoteMaster(team=self.team, owner=self.user, title=f"Plain {i}", body=[])
            for i in range(CORE.max_notes)
        )
        own_html_notes(self.team, self.user2, CORE.max_notes)
        self.assertEqual(html_note_limits.count_owned(self.user.id), 0)
        self.assertTrue(self.save()["created"])

    def test_a_refusal_uses_no_save_from_the_hour(self):
        own_html_notes(self.team, self.user, CORE.max_notes)
        for _ in range(5):
            self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        PersonalNoteMaster.objects.filter(owner=self.user).delete()
        for _ in range(CORE.saves_per_hour):
            self.save()


class RatePerTier(HtmlBase):
    def test_core_gets_30_an_hour(self):
        for _ in range(CORE.saves_per_hour):
            self.save()
        text = self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        self.assertIn("30", text)
        self.assertIn("minutes", text)
        self.assertIn("Nothing was saved", text)

    def test_pro_and_up_get_60_an_hour(self):
        for tier in ("pro", "max", "enterprise"):
            cache.clear()
            self.tier.return_value = tier
            for _ in range(PRO.saves_per_hour):
                self.save()
            text = self.refused(SAVE, html=GOOD, note_type="personal", title="t")
            self.assertIn("60", text, tier)

    def test_the_hour_counts_replaces_too(self):
        note = self.html_note()
        for i in range(CORE.saves_per_hour - 1):
            self.ok(
                SAVE,
                html=GOOD + f"<!--{i}-->",
                note_id=note.note_id,
                note_type="personal",
                summary="s",
            )
        self.refused(SAVE, html=GOOD, note_id=note.note_id, note_type="personal", summary="s")

    def test_a_page_with_no_title_anywhere_uses_no_save_from_the_hour(self):
        untitled = "<!doctype html><html><body><p>no title</p></body></html>"
        for _ in range(CORE.saves_per_hour + 5):
            text = self.refused(SAVE, html=untitled, note_type="personal")
            self.assertIn("needs a title", text)
        for _ in range(CORE.saves_per_hour):
            self.save()  # the whole hour is still there

    def test_a_caller_past_the_hour_never_reaches_the_scan(self):
        for _ in range(CORE.saves_per_hour):
            self.save()
        with mock.patch("origin.services.html_notes.scan_html") as scan:
            self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        scan.assert_not_called()


class DowngradeKeepsPages(HtmlBase):
    def test_pages_stay_readable_and_nothing_is_deleted_after_a_downgrade(self):
        self.tier.return_value = "pro"
        note = self.html_note(page_of(CORE.max_bytes + 10))  # over Core's size, fine on Pro
        self.tier.return_value = "free"
        out = self.ok("get_note", note_type="personal", note_id=note.note_id, include_html=True)
        self.assertEqual(out["body_format"], "html")
        self.assertEqual(len(out["html"].encode()), CORE.max_bytes + 10)
        self.assertEqual(NoteHtmlRevision.objects.count(), 1)
        self.assertEqual(PersonalNoteMaster.objects.count(), 1)

    def test_a_downgraded_member_over_the_new_count_keeps_them_and_cannot_add(self):
        self.tier.return_value = "pro"
        own_html_notes(self.team, self.user, CORE.max_notes + 20)
        self.tier.return_value = "core"
        self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        self.assertEqual(html_note_limits.count_owned(self.user.id), CORE.max_notes + 20)
