"""The proactive digest tick (`agent_digest`, UX tier model §8).

The contracts pinned here:

  * selection is TIER × LOCAL-HOUR × cadence: max=daily, pro=weekly on
    the user's local Monday, free/core never; the hourly tick only
    fires for users whose own clock says --at-hour;
  * IDEMPOTENT: a second run inside the window sends nothing;
  * the agent run is READ-ONLY (every write tool undeclared) and its
    failure sends nothing and stamps nothing (next matching tick
    retries), while a truthful NOTHING_TO_REPORT stamps without
    sending;
  * delivery is an item_type-6 Inbox row (system-authored, item_body +
    item_optionals both populated — the historical POST drop trap) +
    a web push;
  * surface="digest" is non-billable by construction.
"""

from datetime import datetime
from datetime import timezone as dt_timezone
from io import StringIO
from unittest import mock

from django.conf import settings as dj_settings
from django.core.management import call_command

from origin.models.common.inbox_models import InboxItems
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.management.commands import agent_digest as digest_mod

from .test_base import BaseAPITestCase

# Monday 2026-08-03 08:00 in Asia/Tokyo == Sunday 2026-08-02 23:00 UTC.
_MONDAY_8AM_JST = datetime(2026, 8, 2, 23, 0, tzinfo=dt_timezone.utc)
# Tuesday 2026-08-04 08:00 JST.
_TUESDAY_8AM_JST = datetime(2026, 8, 3, 23, 0, tzinfo=dt_timezone.utc)


def _fake_run_agent(text="- Task X slipped.\n- Ping Bob.\nNext: review X."):
    def fake(query, ctx, emit, **kwargs):
        emit({"type": "answer_delta", "text": text})
        emit({"type": "done"})
        return None

    return fake


class DigestTickTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.team.plan = "max"  # daily cadence under the flipped table
        self.team.save(update_fields=["plan"])
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])

    def _tick(self, now=_MONDAY_8AM_JST, run_agent=None, **opts):
        out = StringIO()
        with (
            mock.patch.object(digest_mod.timezone, "now", return_value=now),
            mock.patch.object(
                digest_mod, "run_agent", side_effect=run_agent or _fake_run_agent()
            ) as ra,
            mock.patch.object(digest_mod, "dispatch_push_for_inbox_item") as push,
        ):
            call_command("agent_digest", stdout=out, **opts)
        return ra, push, out.getvalue()

    def _items(self):
        return list(InboxItems.objects.filter(item_type=digest_mod.ITEM_TYPE_DIGEST))

    def test_daily_digest_delivers_item_push_and_stamp(self):
        ra, push, _ = self._tick(at_hour=8)
        items = self._items()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(str(item.receiver_id), str(self.user.id))
        self.assertIsNone(item.sender_id)
        self.assertIn("Task X", item.item_body["text"])
        self.assertEqual(item.item_optionals["cadence"], "daily")
        push.assert_called_once()
        self.user.refresh_from_db()
        self.assertEqual(self.user.digest_last_sent_at, _MONDAY_8AM_JST)
        # The run is read-only: every write tool undeclared.
        writes = {t.name for t in REGISTRY.values() if t.requires_approval}
        self.assertTrue(
            writes <= ra.call_args.kwargs["disabled_tools"],
            "a digest run must not be able to pause on a write approval",
        )

    def test_second_tick_in_the_window_is_idempotent(self):
        self._tick(at_hour=8)
        ra, push, _ = self._tick(at_hour=8)
        self.assertEqual(len(self._items()), 1)
        ra.assert_not_called()
        push.assert_not_called()

    def test_wrong_local_hour_sends_nothing(self):
        ra, _, _ = self._tick(at_hour=9)
        self.assertEqual(self._items(), [])
        ra.assert_not_called()

    def test_weekly_fires_only_on_the_local_monday(self):
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        ra, _, _ = self._tick(now=_TUESDAY_8AM_JST, at_hour=8)
        ra.assert_not_called()
        self._tick(now=_MONDAY_8AM_JST, at_hour=8)
        self.assertEqual(len(self._items()), 1)
        self.assertEqual(self._items()[0].item_optionals["cadence"], "weekly")

    def test_tiers_without_a_cadence_never_fire(self):
        for plan in ("free", "core"):
            self.team.plan = plan
            self.team.save(update_fields=["plan"])
            ra, _, _ = self._tick(at_hour=8)
            ra.assert_not_called()
        self.assertEqual(self._items(), [])

    def test_opt_out_is_respected(self):
        self.user.digest_enabled = False
        self.user.save(update_fields=["digest_enabled"])
        ra, _, _ = self._tick(at_hour=8)
        ra.assert_not_called()

    def test_nothing_to_report_stamps_without_sending(self):
        self._tick(at_hour=8, run_agent=_fake_run_agent("NOTHING_TO_REPORT"))
        self.assertEqual(self._items(), [])
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.digest_last_sent_at)

    def test_a_failed_run_sends_nothing_and_does_not_stamp(self):
        def broken(query, ctx, emit, **kwargs):
            raise RuntimeError("provider down")

        _, push, out = self._tick(at_hour=8, run_agent=broken)
        self.assertEqual(self._items(), [])
        push.assert_not_called()
        self.user.refresh_from_db()
        self.assertIsNone(self.user.digest_last_sent_at, "no stamp -> the next tick retries")
        self.assertIn("failed=1", out)

    def test_dry_run_touches_nothing(self):
        ra, push, out = self._tick(at_hour=8, dry_run=True)
        self.assertEqual(self._items(), [])
        ra.assert_not_called()
        push.assert_not_called()
        self.user.refresh_from_db()
        self.assertIsNone(self.user.digest_last_sent_at)
        self.assertIn("[dry-run]", out)

    def test_user_id_override_skips_the_clock(self):
        # Support/testing lever: --user-id sends regardless of local hour.
        self._tick(at_hour=3, user_id=str(self.user.id))
        self.assertEqual(len(self._items()), 1)

    def test_digest_surface_is_not_billable(self):
        self.assertNotIn(
            "digest",
            dj_settings.CREDIT_POLICY.billable_surfaces,
            "an unlisted surface bills 0 by construction — the digest "
            "must never consume the user's credits",
        )


class DigestEditorialContentTests(BaseAPITestCase):
    """The content contract (`_build_prompt` / `_split_title`).

    The failure this guards against is not a crash — it's two mornings
    in a row that say the same thing in the same order. So: the writer
    must SEE its last editions, it must be able to headline an edition,
    and a conversational body must survive the sentinel check that was
    written for terse bullets.
    """

    def setUp(self):
        super().setUp()
        self.team.plan = "max"
        self.team.save(update_fields=["plan"])
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])

    def _tick(self, now=_MONDAY_8AM_JST, run_agent=None, **opts):
        out = StringIO()
        with (
            mock.patch.object(digest_mod.timezone, "now", return_value=now),
            mock.patch.object(
                digest_mod, "run_agent", side_effect=run_agent or _fake_run_agent()
            ) as ra,
            mock.patch.object(digest_mod, "dispatch_push_for_inbox_item") as push,
        ):
            call_command("agent_digest", stdout=out, **opts)
        return ra, push, out.getvalue()

    def _prompt(self, ra):
        return ra.call_args.args[0]

    # --- the writer's memory -------------------------------------------------

    def test_first_edition_says_so_and_carries_no_history(self):
        ra, _, _ = self._tick(at_hour=8)
        prompt = self._prompt(ra)
        self.assertIn("first edition", prompt)
        self.assertNotIn("What you already told them", prompt)

    def test_previous_editions_reach_the_prompt(self):
        # Yesterday's digest, stored the way the tick stores one.
        InboxItems.objects.create(
            team=self.team,
            sender=None,
            receiver=self.user,
            item_type=digest_mod.ITEM_TYPE_DIGEST,
            item_body={"title": "Two things slipped", "text": "- The API milestone slipped."},
            item_optionals={"cadence": "daily"},
            request_status="",
        )
        ra, _, _ = self._tick(at_hour=8)
        prompt = self._prompt(ra)
        self.assertIn("What you already told them", prompt)
        self.assertIn("The API milestone slipped.", prompt)
        # ...and the instruction that makes the history useful rather
        # than a template to copy.
        self.assertIn("Do NOT restate", prompt)

    def test_only_the_last_two_editions_are_recalled(self):
        for n in range(4):
            InboxItems.objects.create(
                team=self.team,
                sender=None,
                receiver=self.user,
                item_type=digest_mod.ITEM_TYPE_DIGEST,
                item_body={"title": "t", "text": f"edition-{n}"},
                item_optionals={"cadence": "daily"},
                request_status="",
            )
        ra, _, _ = self._tick(at_hour=8)
        prompt = self._prompt(ra)
        self.assertIn("edition-3", prompt)
        self.assertIn("edition-2", prompt)
        self.assertNotIn("edition-1", prompt)
        self.assertNotIn("edition-0", prompt)

    def test_another_users_digests_are_not_recalled(self):
        InboxItems.objects.create(
            team=self.team,
            sender=None,
            receiver=self.user2,
            item_type=digest_mod.ITEM_TYPE_DIGEST,
            item_body={"title": "t", "text": "someone-elses-edition"},
            item_optionals={"cadence": "daily"},
            request_status="",
        )
        ra, _, _ = self._tick(at_hour=8, user_id=str(self.user.id))
        self.assertNotIn("someone-elses-edition", self._prompt(ra))

    def test_the_house_bullet_rules_are_overridden_for_this_surface(self):
        # agent/prompts.py mandates "prefer 3-5 bullets over a paragraph"
        # and bans opening lines — the format the screenshots showed. A
        # briefing has to countermand that at the SYSTEM level or the
        # per-run voice instructions just lose.
        ra, _, _ = self._tick(at_hour=8)
        extra = ra.call_args.kwargs["system_extra"]
        self.assertIn("SURFACE OVERRIDE", extra)
        self.assertIn("does NOT apply", extra)

    def test_prompt_carries_the_local_day_and_cadence(self):
        ra, _, _ = self._tick(at_hour=8)
        prompt = self._prompt(ra)
        # Monday 2026-08-03 in Asia/Tokyo — the USER's day, not UTC's.
        self.assertIn("Monday, August 3", prompt)
        self.assertIn("daily briefing", prompt)

    def test_prompt_asks_for_the_users_language(self):
        self.user.language = "ja"
        self.user.save(update_fields=["language"])
        ra, _, _ = self._tick(at_hour=8)
        self.assertIn("Write in Japanese", self._prompt(ra))

    def test_unknown_language_falls_back_to_english(self):
        self.user.language = "kl"
        self.user.save(update_fields=["language"])
        ra, _, _ = self._tick(at_hour=8)
        self.assertIn("Write in English", self._prompt(ra))

    # --- the headline --------------------------------------------------------

    def test_title_line_becomes_the_item_title_and_the_push_title(self):
        _, push, _ = self._tick(
            at_hour=8,
            run_agent=_fake_run_agent("TITLE: Three things are stuck\n\nMorning. Two slipped."),
        )
        item = InboxItems.objects.get(item_type=digest_mod.ITEM_TYPE_DIGEST)
        self.assertEqual(item.item_body["title"], "Three things are stuck")
        # ...and it is GONE from the body, or the bubble prints it twice.
        self.assertNotIn("TITLE:", item.item_body["text"])
        self.assertTrue(item.item_body["text"].startswith("Morning."))
        self.assertEqual(push.call_args.kwargs["title"], "Three things are stuck")

    def test_output_without_a_title_keeps_the_default_and_its_first_line(self):
        # The regression that matters: a run that ignores the convention
        # loses its headline, never its opening sentence.
        _, push, _ = self._tick(at_hour=8, run_agent=_fake_run_agent("Morning. Two slipped."))
        item = InboxItems.objects.get(item_type=digest_mod.ITEM_TYPE_DIGEST)
        self.assertEqual(item.item_body["title"], digest_mod.DEFAULT_TITLE)
        self.assertEqual(item.item_body["text"], "Morning. Two slipped.")
        self.assertEqual(push.call_args.kwargs["title"], digest_mod.DEFAULT_TITLE)

    def test_a_bolded_or_headed_title_line_is_still_parsed(self):
        for raw in ("**TITLE: Stuck on review**", "## TITLE: Stuck on review"):
            InboxItems.objects.all().delete()
            self.user.digest_last_sent_at = None
            self.user.save(update_fields=["digest_last_sent_at"])
            self._tick(at_hour=8, run_agent=_fake_run_agent(f"{raw}\n\nBody."))
            item = InboxItems.objects.get(item_type=digest_mod.ITEM_TYPE_DIGEST)
            self.assertEqual(item.item_body["title"], "Stuck on review", raw)

    def test_a_headline_is_forced_to_plain_text(self):
        # Both consumers render the headline LITERALLY — a bare
        # <Typography> in the bubble and the web-push title — and unlike
        # the body it never passes through rewrite_citation_md. "no
        # markdown, no links" in the brief is an instruction, not a
        # guarantee, so the markdown has to be taken out here.
        self._tick(
            at_hour=8,
            run_agent=_fake_run_agent(
                "TITLE: **[KDS-439](task:15)** is stuck and [task:99] slipped\n\nBody."
            ),
        )
        item = InboxItems.objects.get(item_type=digest_mod.ITEM_TYPE_DIGEST)
        title = item.item_body["title"]
        self.assertEqual(title, "KDS-439 is stuck and slipped")
        for junk in ("[", "]", "(", ")", "*", "task:"):
            self.assertNotIn(junk, title)

    def test_an_overlong_headline_is_truncated(self):
        self._tick(at_hour=8, run_agent=_fake_run_agent(f"TITLE: {'x' * 200}\n\nBody."))
        item = InboxItems.objects.get(item_type=digest_mod.ITEM_TYPE_DIGEST)
        self.assertLessEqual(len(item.item_body["title"]), digest_mod._TITLE_MAX)

    def test_a_title_with_no_body_sends_nothing(self):
        # An edition that is all headline has nothing to say; stamping
        # without sending is the NOTHING_TO_REPORT rule.
        _, push, _ = self._tick(at_hour=8, run_agent=_fake_run_agent("TITLE: All quiet"))
        self.assertEqual(list(InboxItems.objects.filter(item_type=digest_mod.ITEM_TYPE_DIGEST)), [])
        push.assert_not_called()
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.digest_last_sent_at)

    # --- the sentinel --------------------------------------------------------

    def test_a_digest_that_mentions_the_sentinel_survives(self):
        # The old substring check would have deleted this edition. A
        # conversational digest can absolutely say the words.
        body = "TITLE: Quiet week\n\nThe API side has **nothing to report**, which is the story."
        self._tick(at_hour=8, run_agent=_fake_run_agent(body))
        item = InboxItems.objects.get(item_type=digest_mod.ITEM_TYPE_DIGEST)
        self.assertIn("nothing to report", item.item_body["text"])

    def test_the_bare_sentinel_still_suppresses(self):
        for raw in ("NOTHING_TO_REPORT", "  nothing_to_report  ", "**NOTHING_TO_REPORT**"):
            self.user.digest_last_sent_at = None
            self.user.save(update_fields=["digest_last_sent_at"])
            self._tick(at_hour=8, run_agent=_fake_run_agent(raw))
            self.assertEqual(
                list(InboxItems.objects.filter(item_type=digest_mod.ITEM_TYPE_DIGEST)), [], raw
            )

    # --- --preview -----------------------------------------------------------

    def test_preview_prints_and_persists_nothing(self):
        ra, push, out = self._tick(
            at_hour=8,
            preview=True,
            user_id=str(self.user.id),
            run_agent=_fake_run_agent("TITLE: Three things are stuck\n\nMorning."),
        )
        ra.assert_called_once()  # it really generates — that's the point
        self.assertIn("TITLE: Three things are stuck", out)
        self.assertIn("Morning.", out)
        self.assertEqual(list(InboxItems.objects.filter(item_type=digest_mod.ITEM_TYPE_DIGEST)), [])
        push.assert_not_called()
        self.user.refresh_from_db()
        self.assertIsNone(self.user.digest_last_sent_at)

    def test_preview_ignores_the_once_per_period_guard(self):
        self._tick(at_hour=8)  # a real send stamps
        ra, _, _ = self._tick(at_hour=8, preview=True, user_id=str(self.user.id))
        ra.assert_called_once()


class DigestPreferenceEndpointTests(BaseAPITestCase):
    URL = "/api/v2/user/preferences/digest/"

    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_round_trip(self):
        self.assertTrue(self.client.get(self.URL).data["digest_enabled"])
        resp = self.client.patch(self.URL, {"digest_enabled": False}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.digest_enabled)

    def test_non_boolean_is_rejected(self):
        resp = self.client.patch(self.URL, {"digest_enabled": "yes"}, format="json")
        self.assertEqual(resp.status_code, 400)


class DigestCitationRewriteTests(BaseAPITestCase):
    """The stored digest body must carry REAL /workspace hrefs, not the
    agent's citation tokens — the Inbox has no sources map to resolve
    them, so `[KDS-7](task:15)` in `item_body.text` renders as a dead
    link (or raw markdown) in the bubble."""

    def setUp(self):
        super().setUp()
        from origin.models.project.prj_models import ProjectMaster
        from origin.models.task.task_models import TaskMaster

        self.team.plan = "max"
        self.team.save(update_fields=["plan"])
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="Fix login", status="Open"
        )

    def test_rewriter_resolves_degrades_and_keeps_http(self):
        from origin.search_engine.agent.tools.entity_links import rewrite_citation_md

        text = (
            f"- [WRD-1](task:{self.task.task_id}) is overdue.\n"
            "- [gone](task:999999) was deleted.\n"
            "- See [MDN](https://developer.mozilla.org)."
        )
        out = rewrite_citation_md(text, team_id=str(self.team.team_id))
        self.assertIn(
            f"[WRD-1](/workspace/tasks/project/{self.project.project_id}/task/{self.task.task_id})",
            out,
        )
        # Unresolvable token degrades to prose — never a dead link.
        self.assertIn("- gone was deleted.", out)
        self.assertNotIn("task:999999", out)
        # Real URLs pass through untouched.
        self.assertIn("[MDN](https://developer.mozilla.org)", out)

    def test_foreign_team_token_degrades(self):
        from origin.models.common.team_models import TeamMaster
        from origin.search_engine.agent.tools.entity_links import rewrite_citation_md

        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        out = rewrite_citation_md(
            f"[peek](task:{self.task.task_id})", team_id=str(other_team.team_id)
        )
        self.assertEqual(out, "peek")

    def test_tick_stores_rewritten_body(self):
        out = StringIO()
        with (
            mock.patch.object(digest_mod.timezone, "now", return_value=_MONDAY_8AM_JST),
            mock.patch.object(
                digest_mod,
                "run_agent",
                side_effect=_fake_run_agent(f"- [WRD-1](task:{self.task.task_id}) slipped."),
            ),
            mock.patch.object(digest_mod, "dispatch_push_for_inbox_item"),
        ):
            call_command("agent_digest", at_hour=8, stdout=out)
        item = InboxItems.objects.get(item_type=digest_mod.ITEM_TYPE_DIGEST)
        self.assertIn(
            f"/workspace/tasks/project/{self.project.project_id}/task/{self.task.task_id}",
            item.item_body["text"],
        )
        self.assertNotIn(f"(task:{self.task.task_id})", item.item_body["text"])


class BareCitationTokenTests(BaseAPITestCase):
    """The agent emits BOTH `[label](task:12)` and bare `[task:12]`; a
    digest must link the same way a note body does for either form."""

    def setUp(self):
        super().setUp()
        from origin.models.project.prj_models import ProjectMaster
        from origin.models.task.task_models import TaskMaster

        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        # display_id is a derived property ("<project.code>-<number>"),
        # not a column — set the number it reads from.
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Fix login",
            status="Open",
            project_task_number=4,
        )

    def test_bare_token_becomes_a_labelled_link(self):
        from origin.search_engine.agent.tools.entity_links import rewrite_citation_md

        out = rewrite_citation_md(
            f"Start with [task:{self.task.task_id}] today.",
            team_id=str(self.team.team_id),
        )
        # Label comes from the entity itself (display_id), like the note path.
        self.assertIn(
            f"[WRD-4](/workspace/tasks/project/{self.project.project_id}/task/{self.task.task_id})",
            out,
        )

    def test_bare_project_token_resolves(self):
        from origin.search_engine.agent.tools.entity_links import rewrite_citation_md

        out = rewrite_citation_md(
            f"[project:{self.project.project_id}] is stale.",
            team_id=str(self.team.team_id),
        )
        self.assertIn(
            f"[Website Redesign](/workspace/tasks/project/{self.project.project_id})", out
        )

    def test_unresolvable_bare_token_is_left_literal(self):
        from origin.search_engine.agent.tools.entity_links import rewrite_citation_md

        out = rewrite_citation_md("See [task:999999].", team_id=str(self.team.team_id))
        self.assertEqual(out, "See [task:999999].")

    def test_rewriting_is_idempotent(self):
        from origin.search_engine.agent.tools.entity_links import rewrite_citation_md

        once = rewrite_citation_md(
            f"[WRD-4](task:{self.task.task_id}) and [task:{self.task.task_id}].",
            team_id=str(self.team.team_id),
        )
        twice = rewrite_citation_md(once, team_id=str(self.team.team_id))
        self.assertEqual(once, twice)
        # And the already-rewritten label is not re-consumed as a token.
        self.assertNotIn("task:", once)


class BackfillDigestLinksTests(BaseAPITestCase):
    """The one-shot repair for digests stored before the link fix."""

    def setUp(self):
        super().setUp()
        from origin.models.project.prj_models import ProjectMaster
        from origin.models.task.task_models import TaskMaster

        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="Fix login", status="Open"
        )
        self.item = InboxItems.objects.create(
            team=self.team,
            sender=None,
            receiver=self.user,
            item_type=digest_mod.ITEM_TYPE_DIGEST,
            item_body={
                "title": "Your Genos digest",
                "text": f"- [KDS-1](task:{self.task.task_id}) is overdue.",
            },
            item_optionals={"cadence": "daily"},
            request_status="",
        )
        self.href = f"/workspace/tasks/project/{self.project.project_id}/task/{self.task.task_id}"

    def _run(self, **opts):
        out = StringIO()
        call_command("backfill_digest_links", stdout=out, **opts)
        return out.getvalue()

    def test_backfill_rewrites_stored_tokens(self):
        report = self._run()
        self.assertIn("changed=1", report)
        self.item.refresh_from_db()
        self.assertIn(f"[KDS-1]({self.href})", self.item.item_body["text"])
        self.assertNotIn("task:", self.item.item_body["text"])
        # Sibling keys survive the rewrite.
        self.assertEqual(self.item.item_body["title"], "Your Genos digest")

    def test_dry_run_writes_nothing(self):
        report = self._run(dry_run=True)
        self.assertIn("changed=1", report)
        self.item.refresh_from_db()
        self.assertIn(f"(task:{self.task.task_id})", self.item.item_body["text"])

    def test_second_run_is_a_noop(self):
        self._run()
        report = self._run()
        self.assertIn("changed=0", report)

    def test_non_digest_items_are_untouched(self):
        other = InboxItems.objects.create(
            team=self.team,
            sender=self.user2,
            receiver=self.user,
            item_type=1,
            item_body=[{"type": "paragraph", "content": []}],
        )
        self._run()
        other.refresh_from_db()
        self.assertEqual(other.item_body, [{"type": "paragraph", "content": []}])
