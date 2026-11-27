"""HTML notes by plan (MIP-2216): Core and up only, with the limits in ONE table.

What is pinned:

- the table: Free has no row, Core is 512 KB / 200 MB of storage / 30 saves an hour, Pro, Max and
  Enterprise are 1 MB / 1 GB / 60, and the derived ceiling is the largest page any plan has;
- Free: `save_html_note` stores NOTHING (no note, no page, no revision, no save used) and answers with
  a suggestion that is not an error, for a create, a replace and an oversize page alike;
- each limit per tier: the size (at the limit saves, one byte over is refused with the numbers and the
  fix, and the higher tier takes the same page), the saves per hour;
- there is no count of HTML notes (MIP-2453); storage is the bytes of every kept revision of the
  HTML notes the member owns, a save that would pass the quota stores nothing, and a save that adds
  nothing (the revision it drops is as big) is taken even at or past the quota;
- a refused save uses no save from the hour; a failed plan lookup reads as Core, never as an error;
- a downgrade deletes nothing and reads still work (MCP `get_note` here, the REST read in
  `test_html_notes_api`), only new saves are held to the new plan.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache

from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.html_note_models import NoteHtmlRevision
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.services import html_note_limits, html_notes
from origin.tests.test_mcp_save_html_note import (
    CORE,
    EDITOR,
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
    """`count` HTML notes owned by `user`, made straight in the table, with no page."""
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


def kept(note_id: int, size: int, *, rev: int = 1, code: int = 1) -> None:
    """A kept revision of `size` bytes, as the quota counts it: the `size` column, no real page."""
    NoteHtmlRevision.objects.create(
        note_type=code, note_id=note_id, rev=rev, html="", sha256="0" * 64, size=size
    )


def stored(team, user, size: int, *, model=PersonalNoteMaster, code: int = 1, **fields) -> int:
    """An HTML note of `user` whose one kept revision is `size` bytes. Returns its id."""
    note = model.objects.create(
        team=team,
        owner=user,
        title="Big",
        body=[],
        body_format=html_notes.BODY_FORMAT_HTML,
        **fields,
    )
    kept(note.note_id, size, code=code)
    return note.note_id


class TheTable(HtmlBase):
    def test_the_rows(self):
        table = html_note_limits.HTML_NOTE_LIMITS
        self.assertIsNone(table["free"])
        self.assertEqual(
            (CORE.max_bytes, CORE.max_storage_bytes, CORE.saves_per_hour),
            (512 * 1024, 200 * 1024 * 1024, 30),
        )
        for tier in ("pro", "max", "enterprise"):
            row = table[tier]
            self.assertEqual(
                (row.max_bytes, row.max_storage_bytes, row.saves_per_hour),
                (1024 * 1024, 1024 * 1024 * 1024, 60),
                tier,
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


class NoCountLimit(HtmlBase):
    def test_a_core_member_creates_the_101st_and_the_501st_html_note(self):
        own_html_notes(self.team, self.user, 100)
        self.assertTrue(self.save()["created"])
        own_html_notes(self.team, self.user, 399)
        self.assertTrue(self.save()["created"])


class StorageUsed(HtmlBase):
    def test_it_sums_every_kept_revision_of_all_three_families_in_one_query(self):
        personal = stored(self.team, self.user, 10)
        kept(personal, 20, rev=2)
        stored(self.team, self.user, 300, model=TaskNoteMaster, code=2)
        stored(
            self.team, self.user, 4000, model=ChatNoteMaster, code=3, chat_type=1, is_thread=False
        )
        with self.assertNumQueries(1):
            self.assertEqual(html_note_limits.storage_used(self.user.id), 4330)

    def test_other_members_pages_and_ordinary_notes_do_not_count(self):
        stored(self.team, self.user2, 999)
        PersonalNoteMaster.objects.create(team=self.team, owner=self.user, title="p", body=[])
        self.assertEqual(html_note_limits.storage_used(self.user.id), 0)

    def test_the_family_code_is_part_of_the_match(self):
        # A task note's revisions must not be counted for a personal note with the same number.
        note_id = stored(self.team, self.user, 10)
        kept(note_id, 5000, code=2)
        self.assertEqual(html_note_limits.storage_used(self.user.id), 10)

    def test_deleting_a_note_frees_its_storage_at_once(self):
        note_id = stored(self.team, self.user, 700)
        PersonalNoteMaster.objects.get(note_id=note_id).delete()
        self.assertEqual(html_note_limits.storage_used(self.user.id), 0)

    def test_the_numbers_read_as_a_person_writes_them(self):
        text = html_note_limits.size_text
        self.assertEqual(text(CORE.max_storage_bytes), "200 MB")
        self.assertEqual(text(PRO.max_storage_bytes), "1 GB")
        self.assertEqual(text(1536 * 1024), "1.5 MB")
        self.assertEqual(text(1), "1 KB")


class StorageQuota(HtmlBase):
    size = len(GOOD.encode())

    def fill(self, left: int, user=None, row=CORE) -> None:
        """Give `user` a stored page that leaves `left` bytes of `row`'s quota."""
        stored(self.team, user or self.user, row.max_storage_bytes - left)

    def replace(self, note_id: int, html: str = GOOD):
        return self.run_tool(SAVE, html=html, note_id=note_id, note_type="personal", summary="s")

    def at_the_cap(self) -> int:
        """An HTML note of mine holding `KEEP_REVISIONS` revisions of exactly `GOOD`'s size."""
        note = self.html_note()
        for rev in range(2, html_notes.KEEP_REVISIONS + 1):
            kept(note.note_id, self.size, rev=rev)
        return note.note_id

    def test_a_create_that_just_fits_is_taken_and_one_byte_more_is_refused(self):
        self.fill(left=self.size)
        self.assertTrue(self.save()["created"])
        notes, revisions = PersonalNoteMaster.objects.count(), NoteHtmlRevision.objects.count()
        text = self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        for part in ("200 MB of your 200 MB", "Delete", "note_id", "higher plan", "not saved"):
            self.assertIn(part, text)
        self.assertEqual(PersonalNoteMaster.objects.count(), notes)
        self.assertEqual(NoteHtmlRevision.objects.count(), revisions)

    def test_pro_has_1_gb_and_is_not_told_to_upgrade(self):
        self.fill(left=0)
        self.tier.return_value = "pro"
        self.assertTrue(self.save()["created"])
        stored(self.team, self.user, PRO.max_storage_bytes - CORE.max_storage_bytes)
        text = self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        self.assertIn("of your 1 GB", text)
        self.assertNotIn("higher plan", text)

    def test_a_replace_under_20_versions_adds_a_version_and_is_held_to_the_quota(self):
        note = self.html_note()
        self.fill(left=self.size + 10)  # 10 bytes left once the page it has is counted
        text, is_error = self.replace(note.note_id)
        self.assertTrue(is_error, text)
        self.assertIn("make this page smaller", text)
        self.assertEqual(self.page_count(note.note_id), 1)

    def test_a_same_size_replace_at_20_versions_is_taken_at_the_quota_edge(self):
        note_id = self.at_the_cap()
        self.fill(left=html_notes.KEEP_REVISIONS * self.size)  # the 20 versions fill the rest
        self.assertEqual(html_note_limits.storage_used(self.user.id), CORE.max_storage_bytes)
        text, is_error = self.replace(note_id)
        self.assertFalse(is_error, text)
        self.assertEqual(self.page_count(note_id), html_notes.KEEP_REVISIONS)
        self.assertEqual(html_note_limits.storage_used(self.user.id), CORE.max_storage_bytes)

    def test_at_20_versions_a_bigger_page_is_charged_only_for_the_difference(self):
        note_id = self.at_the_cap()
        free = CORE.max_storage_bytes - html_note_limits.storage_used(self.user.id)
        stored(self.team, self.user, free - 10)  # 10 bytes left
        text, is_error = self.replace(note_id, GOOD + "x" * 11)
        self.assertTrue(is_error, text)
        text, is_error = self.replace(note_id, GOOD + "x" * 10)
        self.assertFalse(is_error, text)
        self.assertEqual(html_note_limits.storage_used(self.user.id), CORE.max_storage_bytes)

    def test_a_member_over_the_quota_keeps_everything_and_may_still_not_grow(self):
        note_id = self.at_the_cap()
        stored(self.team, self.user, CORE.max_storage_bytes)  # over, e.g. after a downgrade
        used = html_note_limits.storage_used(self.user.id)
        self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        self.assertEqual(html_note_limits.storage_used(self.user.id), used)
        text, is_error = self.replace(note_id)  # frees as much as it adds
        self.assertFalse(is_error, text)

    def test_an_editor_replace_is_held_to_the_owners_quota_and_told_no_numbers(self):
        note = self.html_note()
        PersonalNoteMaster.objects.filter(note_id=note.note_id).update(owner=self.user2)
        NotePermissionMaster.objects.filter(note_id=note.note_id, note_type=1).update(
            role_id=EDITOR
        )
        self.fill(left=0, user=self.user2)
        text, is_error = self.replace(note.note_id)
        self.assertTrue(is_error, text)
        self.assertIn("owner", text)
        self.assertNotIn("MB", text)
        self.assertEqual(self.page_count(note.note_id), 1)

    def test_an_editor_cannot_probe_the_owners_storage_for_free(self):
        note = self.html_note()
        PersonalNoteMaster.objects.filter(note_id=note.note_id).update(owner=self.user2)
        NotePermissionMaster.objects.filter(note_id=note.note_id, note_type=1).update(
            role_id=EDITOR
        )
        self.fill(left=0, user=self.user2)
        for _ in range(CORE.saves_per_hour - 1):  # `html_note` used one of the hour
            self.assertIn("owner", self.replace(note.note_id)[0])
        self.assertIn("this hour", self.replace(note.note_id)[0])

    def test_over_the_quota_a_replace_is_not_told_to_make_the_page_smaller(self):
        note = self.html_note()
        self.fill(left=0)
        text, is_error = self.replace(note.note_id)
        self.assertTrue(is_error, text)
        self.assertNotIn("smaller", text)
        self.assertIn("Delete", text)

    def test_an_editors_own_full_storage_does_not_stop_a_replace_of_anothers_page(self):
        note = self.html_note()
        PersonalNoteMaster.objects.filter(note_id=note.note_id).update(owner=self.user2)
        NotePermissionMaster.objects.filter(note_id=note.note_id, note_type=1).update(
            role_id=EDITOR
        )
        self.fill(left=0)
        text, is_error = self.replace(note.note_id)
        self.assertFalse(is_error, text)

    def test_a_refusal_uses_no_save_from_the_hour_and_never_reaches_the_scan(self):
        self.fill(left=0)
        with mock.patch("origin.services.html_notes.scan_html") as scan:
            for _ in range(5):
                self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        scan.assert_not_called()
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

    def test_a_downgraded_member_over_the_new_quota_keeps_them_and_cannot_add(self):
        self.tier.return_value = "pro"
        stored(self.team, self.user, CORE.max_storage_bytes + 20)
        self.tier.return_value = "core"
        self.refused(SAVE, html=GOOD, note_type="personal", title="t")
        self.assertEqual(html_note_limits.storage_used(self.user.id), CORE.max_storage_bytes + 20)
