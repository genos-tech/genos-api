"""The official Genos skills (MIP-2048): the Skills v2 runbooks as public skills under a Genos system
account, written by `manage.py sync_official_skills`.

What matters: the committed catalog is valid and only talks to tools the in-app agent has; the dry run
(the default) writes nothing, not even the account; an apply makes one public official skill per
runbook; a second run changes nothing; a new release cuts a new version and keeps stars and installs;
nobody else's skill is touched (a member's skill of the same name, an operator's takedown, a real
account on the official address); the text of each version is the immutable one; and nothing is
visible, or runnable by name, while `GENOS_SKILLS_PUBLIC` is off.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from origin.models.common.team_models import TeamMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.skills.skill_models import Skill, SkillInstall, SkillStar, SkillVersion
from origin.search_engine.agent.tools import base as tool_base
from origin.services import blind_index
from origin.services.skills import official, run, service, text
from origin.services.skills.official_slugs import OFFICIAL_SLUGS
from origin.tests.test_skills_api import SkillBase
from origin.views.public.mcp import tools as mcp_tools

User = get_user_model()

TRIAGE = "triage"


def sync_command(*args: str) -> str:
    out = StringIO()
    call_command("sync_official_skills", *args, stdout=out)
    return out.getvalue()


def official_skills():
    return Skill.objects.filter(is_official=True)


class CatalogTests(SkillBase):
    def test_the_catalog_is_valid_and_matches_the_reserved_slugs(self):
        specs = official.load_catalog()
        self.assertEqual({s.slug for s in specs}, set(OFFICIAL_SLUGS))
        self.assertGreaterEqual(len(specs), 30)
        manifest = json.loads((official.CATALOG_DIR / "manifest.json").read_text())
        self.assertEqual(manifest["release"], specs[0].release)
        # A runbook left out says why.
        self.assertTrue(all(manifest["excluded"].values()))
        self.assertFalse(set(manifest["excluded"]) & set(OFFICIAL_SLUGS))

    def test_every_text_closes_with_a_link_to_its_release(self):
        for spec in official.load_catalog():
            last = spec.body_markdown.rstrip().split("\n")[-1]
            self.assertTrue(last.startswith("Source: ["), spec.slug)
            self.assertIn(f"/blob/{spec.release}/plugins/", last, spec.slug)

    def test_a_text_only_names_tools_the_in_app_agent_has(self):
        """A connector-only name (`get_my_briefing`) or one the connector renames (`get_task`) in a
        text would send the in-app agent to a tool it does not have."""
        # A flag-gated tool counts (MIP-2485): the skill that needs one is published after the flag
        # is on (see the flag's comment in settings).
        in_app = set(tool_base.REGISTRY) | set(tool_base.FLAGGED_REGISTRY)
        connector_names = {t.name for t in mcp_tools.CATALOG} | set(tool_base.MCP_ONLY_REGISTRY)
        for spec in official.load_catalog():
            for name in set(re.findall(r"`([a-z]+(?:_[a-z0-9]+)+)`", spec.body_markdown)):
                if name in connector_names:
                    self.assertIn(name, in_app, f"{spec.slug} calls `{name}`, which is not in-app")

    def test_the_briefing_runbooks_run_in_app_from_the_in_app_reads(self):
        """MIP-2203: wrap-up, weekly review, weekly priorities and study week were built on the
        connector-only briefing; each now has a hand-written in-app text. Every tool and parameter it
        names exists in the in-app agent, and the one left out says why."""
        converted = {"wrapup", "weekly-review", "weekly-priorities", "study-week"}
        slugs = {s.slug: s for s in official.load_catalog()}
        self.assertTrue(converted <= set(slugs))
        in_app = set(tool_base.REGISTRY)
        params = {
            p
            for t in tool_base.REGISTRY.values()
            for p in (t.parameters_schema or {}).get("properties", {})
        }
        fields = {"effective_status", "last_progress_at"}
        for slug in converted:
            body = slugs[slug].body_markdown
            for name in set(re.findall(r"`([a-z]+(?:_[a-z0-9]+)+)`", body)):
                self.assertTrue(name in in_app or name in params or name in fields, (slug, name))
            self.assertNotIn("get_my_briefing", body, slug)
        manifest = json.loads((official.CATALOG_DIR / "manifest.json").read_text())
        self.assertFalse(converted & set(manifest["excluded"]))
        self.assertIn("catch-me-up", manifest["excluded"]["catchup"])

    def test_a_text_does_not_talk_to_claude_code_or_the_connector(self):
        banned = re.compile(
            r"/genos|MCP|connector|briefing|CLAUDE\.md|\bgh [a-z]|\bgit [a-z]", re.I
        )
        for spec in official.load_catalog():
            body = spec.body_markdown.split("\nSource: [")[0]
            self.assertIsNone(banned.search(body), spec.slug)

    def test_a_member_cannot_take_an_official_name(self):
        with self.assertRaises(text.SkillTextError):
            text.clean_slug(TRIAGE)
        self.assertEqual(text.clean_slug(TRIAGE, official=True), TRIAGE)
        # `official=True` is not a way round the other reserved names.
        with self.assertRaises(text.SkillTextError):
            text.clean_slug("help", official=True)
        res = self.make(slug=TRIAGE)
        self.assertEqual(res.status_code, 400, res.content)


class SyncBase(SkillBase):
    def setUp(self):
        super().setUp()
        self.catalog_count = len(official.load_catalog())

    def catalog_for(self, release: str, *, change: dict[str, str] | None = None) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copytree(official.CATALOG_DIR, tmp / "c")
        base = tmp / "c"
        manifest = json.loads((base / "manifest.json").read_text())
        old = manifest["release"]
        manifest["release"] = release
        (base / "manifest.json").write_text(json.dumps(manifest))
        for path in base.glob("*.md"):
            body = path.read_text().replace(f"Genos skills {old}", f"Genos skills {release}")
            body = body.replace(f"/blob/{old}/", f"/blob/{release}/")
            if change and path.stem in change:
                body = body.replace("You get:", change[path.stem], 1)
            path.write_text(body)
        return base

    def use(self, directory: Path):
        patch = mock.patch.object(official, "CATALOG_DIR", directory)
        patch.start()
        self.addCleanup(patch.stop)


class DryRunTests(SyncBase):
    def test_the_default_writes_nothing_not_even_the_account(self):
        before = (
            User.objects.count(),
            TeamMaster.objects.count(),
            Skill.objects.count(),
            PersonalNoteMaster.objects.count(),
            PersonalNoteFolder.objects.count(),
        )
        out = sync_command()
        self.assertIn("dry run, nothing written", out)
        self.assertIn(f"{self.catalog_count} to create, 0 to update", out)
        self.assertIn("Run again with --apply", out)
        after = (
            User.objects.count(),
            TeamMaster.objects.count(),
            Skill.objects.count(),
            PersonalNoteMaster.objects.count(),
            PersonalNoteFolder.objects.count(),
        )
        self.assertEqual(before, after)

    def test_the_service_default_is_a_dry_run_too(self):
        report = official.sync()
        self.assertFalse(report.applied)
        self.assertFalse(report.account_exists)
        self.assertEqual(report.count(official.CREATE), self.catalog_count)
        self.assertEqual(Skill.objects.count(), 0)

    def test_a_dry_run_after_an_apply_reports_without_writing(self):
        sync_command("--apply")
        versions = SkillVersion.objects.count()
        out = sync_command()
        self.assertIn(f"0 to create, 0 to update, {self.catalog_count} unchanged", out)
        self.assertEqual(SkillVersion.objects.count(), versions)

    def test_the_output_names_no_account_or_team(self):
        out = sync_command("--apply")
        for word in (
            official.OFFICIAL_EMAIL,
            official.OFFICIAL_TEAM_NAME,
            official.OFFICIAL_USERNAME,
        ):
            self.assertNotIn(word, out)


class ApplyTests(SyncBase):
    def test_one_public_official_skill_per_runbook(self):
        out = sync_command("--apply")
        self.assertIn(f"{self.catalog_count} created, 0 updated", out)
        skills = list(official_skills())
        self.assertEqual({s.slug for s in skills}, set(OFFICIAL_SLUGS))
        owner = User.objects.get(blind_index.USER_EMAIL.matches(official.OFFICIAL_EMAIL))
        team = TeamMaster.objects.get(owner=owner)
        self.assertEqual(team.team_name, official.OFFICIAL_TEAM_NAME)
        self.assertTrue(owner.is_system_user)
        self.assertFalse(owner.has_usable_password())
        self.assertEqual(team.owner_id, owner.pk)
        for skill in skills:
            self.assertEqual(skill.visibility, "public")
            self.assertEqual(skill.source, "official")
            self.assertEqual(skill.status, "active")
            self.assertEqual((skill.current_version_no, skill.published_version_no), (1, 1))
            self.assertEqual(skill.public_from_version_no, 1)
            self.assertEqual((skill.owner_id, skill.team_id), (owner.pk, team.team_id))
            self.assertTrue(skill.hub_text)  # the directory columns are written
            self.assertTrue(skill.search_vector)

    def test_a_skill_is_a_note_in_the_skill_folder_and_an_immutable_version(self):
        sync_command("--apply", "--only", TRIAGE)
        skill = official_skills().get()
        note = PersonalNoteMaster.objects.get(note_id=skill.note_id)
        folder = PersonalNoteFolder.objects.get(folder_id=note.folder_id)
        self.assertEqual(folder.scope, PersonalNoteFolder.SCOPE_SKILL)
        version = skill.versions.get()
        self.assertIn("list_my_inbox", version.body_markdown)
        self.assertIn("Source: [Genos skills", version.body_markdown)
        self.assertEqual(version.changelog, official.load_catalog()[0].changelog)
        self.assertEqual(version.created_by_id, skill.owner_id)
        version.title = "changed"
        with self.assertRaises(ValueError):
            version.save()

    def test_the_tool_level_follows_what_the_text_can_write(self):
        sync_command("--apply")
        self.assertEqual(official_skills().get(slug="content-week").tool_level, "read")
        self.assertEqual(official_skills().get(slug=TRIAGE).tool_level, "act")

    def test_only_limits_the_sync_and_refuses_an_unknown_name(self):
        sync_command("--apply", "--only", TRIAGE, "--only", "handoff")
        self.assertEqual(
            sorted(official_skills().values_list("slug", flat=True)), ["handoff", TRIAGE]
        )
        with self.assertRaises(CommandError):
            sync_command("--apply", "--only", "no-such-skill")
        self.assertEqual(official_skills().count(), 2)

    def test_a_flagged_official_text_is_reported_and_still_not_held(self):
        out = sync_command("--apply")
        self.assertFalse(Skill.objects.filter(is_official=True).exclude(status="active").exists())
        self.assertIn("official skills (applied)", out)


class IdempotencyTests(SyncBase):
    def test_a_second_run_changes_nothing(self):
        sync_command("--apply")
        ids = dict(official_skills().values_list("slug", "id"))
        counts = (
            Skill.objects.count(),
            SkillVersion.objects.count(),
            PersonalNoteMaster.objects.count(),
            User.objects.count(),
            TeamMaster.objects.count(),
        )
        stamp = dict(official_skills().values_list("slug", "updated_at"))
        out = sync_command("--apply")
        self.assertIn(f"0 created, 0 updated, {self.catalog_count} unchanged", out)
        self.assertEqual(dict(official_skills().values_list("slug", "id")), ids)
        self.assertEqual(
            counts,
            (
                Skill.objects.count(),
                SkillVersion.objects.count(),
                PersonalNoteMaster.objects.count(),
                User.objects.count(),
                TeamMaster.objects.count(),
            ),
        )
        self.assertEqual(dict(official_skills().values_list("slug", "updated_at")), stamp)

    def test_a_run_that_stopped_half_way_finishes_the_rest(self):
        sync_command("--apply", "--only", TRIAGE)
        out = sync_command("--apply")
        self.assertIn(f"{self.catalog_count - 1} created, 0 updated, 1 unchanged", out)
        self.assertEqual(official_skills().count(), self.catalog_count)


class ReleaseUpdateTests(SyncBase):
    """A newer plugin release changes each text's closing "Source" line, so it is a new text."""

    def test_a_new_release_cuts_version_two_and_keeps_stars_and_installs(self):
        committed = official.load_catalog()[0].release
        sync_command("--apply")
        skill = official_skills().get(slug=TRIAGE)
        SkillStar.objects.create(user=self.user, skill=skill)
        SkillInstall.objects.update_or_create(
            user=self.user, skill=skill, defaults={"version_no": 1}
        )  # `triage` is a default, so the first sync may already have made the row
        Skill.objects.filter(pk=skill.pk).update(star_count=3, install_count=2, run_count_30d=5)
        old_note = skill.note_id

        self.use(self.catalog_for("v9.9.9"))
        out = sync_command()
        self.assertIn(f"0 to create, {self.catalog_count} to update", out)
        out = sync_command("--apply")
        self.assertIn(f"0 created, {self.catalog_count} updated", out)

        skill.refresh_from_db()
        self.assertEqual((skill.current_version_no, skill.published_version_no), (2, 2))
        self.assertEqual(
            (skill.star_count, skill.install_count, skill.run_count_30d, skill.note_id),
            (3, 2, 5, old_note),
        )
        mine = SkillInstall.objects.get(user=self.user, skill=skill)
        self.assertEqual(mine.version_no, 1)  # pinned: they accept
        new = skill.versions.get(version_no=2)
        self.assertEqual(new.changelog, "Genos skills v9.9.9")
        self.assertIn("v9.9.9", new.body_markdown)
        self.assertIn(committed, skill.versions.get(version_no=1).body_markdown)  # old text stays
        self.assertEqual(skill.versions.count(), 2)
        self.assertEqual(Skill.objects.count(), self.catalog_count)

        out = sync_command("--apply")  # and that is idempotent too
        self.assertIn(f"0 created, 0 updated, {self.catalog_count} unchanged", out)

    def test_a_changed_text_updates_only_that_skill(self):
        sync_command("--apply")
        self.use(
            self.catalog_for(official.load_catalog()[0].release, change={TRIAGE: "You get (new):"})
        )
        out = sync_command("--apply")
        self.assertIn("1 updated", out)
        self.assertEqual(official_skills().get(slug=TRIAGE).published_version_no, 2)
        self.assertEqual(
            official_skills().exclude(slug=TRIAGE).filter(published_version_no=1).count(),
            self.catalog_count - 1,
        )
        self.assertIn(
            "You get (new):",
            official_skills().get(slug=TRIAGE).versions.get(version_no=2).body_markdown,
        )

    def test_a_skill_dropped_from_the_catalog_is_reported_and_left(self):
        sync_command("--apply")
        base = self.catalog_for(official.load_catalog()[0].release)
        manifest = json.loads((base / "manifest.json").read_text())
        manifest["skills"] = [s for s in manifest["skills"] if s["slug"] != "handoff"]
        (base / "manifest.json").write_text(json.dumps(manifest))
        self.use(base)
        with mock.patch.object(text, "OFFICIAL_SLUGS", OFFICIAL_SLUGS - {"handoff"}):
            out = sync_command("--apply")
        self.assertIn("orphan    handoff", out)
        self.assertEqual(official_skills().get(slug="handoff").published_version_no, 1)

    def test_a_catalog_that_differs_from_the_reserved_names_is_refused(self):
        base = self.catalog_for(official.load_catalog()[0].release)
        manifest = json.loads((base / "manifest.json").read_text())
        manifest["skills"] = manifest["skills"][1:]
        (base / "manifest.json").write_text(json.dumps(manifest))
        self.use(base)
        with self.assertRaises(CommandError):
            sync_command("--apply")
        self.assertEqual(Skill.objects.count(), 0)


class OnlyOfficialTests(SyncBase):
    def test_a_members_public_skill_of_the_same_name_is_skipped_and_reported(self):
        """MIP-2210: public names are unique, so the member who published first keeps the name and
        the official skill of that name waits. Nothing of the member's is touched, nothing errors."""
        mine = Skill.objects.create(
            team=self.team,
            owner=self.user,
            slug=TRIAGE,
            title="My triage",
            visibility="public",
            published_version_no=1,
            current_version_no=1,
        )
        out = sync_command("--apply")
        mine.refresh_from_db()
        self.assertEqual((mine.title, mine.is_official, mine.source), ("My triage", False, "user"))
        self.assertEqual(mine.versions.count(), 0)
        self.assertEqual(official_skills().filter(slug=TRIAGE).count(), 0)
        self.assertEqual(official_skills().count(), self.catalog_count - 1)
        self.assertIn("skipped   triage", out)
        self.assertIn("1 skipped", out)
        # The dry run says the same, and the next run keeps skipping without an error.
        self.assertIn("skipped   triage", sync_command())
        sync_command("--apply")
        self.assertEqual(official_skills().filter(slug=TRIAGE).count(), 0)

    def test_a_members_private_skill_of_the_same_name_is_not_touched(self):
        mine = Skill.objects.create(
            team=self.team,
            owner=self.user,
            slug=TRIAGE,
            title="My triage",
            visibility="private",
            published_version_no=1,
            current_version_no=1,
        )
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="My triage", body=[]
        )
        Skill.objects.filter(pk=mine.pk).update(note_id=note.note_id)
        SkillVersion.objects.create(
            skill=mine,
            version_no=1,
            title="My triage",
            slug=TRIAGE,
            tool_level="read",
            body_markdown="MINE",
            content_hash="x",
        )
        sync_command("--apply")
        mine.refresh_from_db()
        self.assertEqual((mine.title, mine.is_official, mine.source), ("My triage", False, "user"))
        self.assertEqual(mine.versions.count(), 1)
        self.assertEqual(mine.versions.get().body_markdown, "MINE")
        self.assertEqual(official_skills().filter(slug=TRIAGE).count(), 1)
        self.assertEqual(Skill.objects.filter(slug=TRIAGE).count(), 2)

    def test_a_member_who_already_has_an_official_name_can_still_save_that_skill(self):
        """Reserving the names does not lock an existing skill: it is only refused for a NEW name."""
        mine = service.create(
            self.user,
            self.team.team_id,
            service.Fields(title="My notes", slug="plain-name", body_markdown="# Mine\n\n1. Read."),
        )
        Skill.objects.filter(pk=mine.pk).update(slug="handoff")  # made before the name was reserved
        service.update(self.user, mine.pk, service.Fields(slug="handoff", title="Renamed title"))
        mine.refresh_from_db()
        self.assertEqual((mine.slug, mine.title), ("handoff", "Renamed title"))
        with self.assertRaises(service.SkillError):
            service.update(self.user, mine.pk, service.Fields(slug="triage"))

    def test_an_account_that_is_not_ours_on_the_official_address_is_refused(self):
        User.objects.create_user(
            username="someone", email=official.OFFICIAL_EMAIL, password="x-pass-123"
        )
        with self.assertRaises(CommandError):
            sync_command("--apply")
        with self.assertRaises(CommandError):
            sync_command()
        self.assertEqual(Skill.objects.count(), 0)
        self.assertEqual(
            TeamMaster.objects.filter(team_name=official.OFFICIAL_TEAM_NAME).count(), 0
        )

    def test_a_member_taking_the_team_name_does_not_block_the_sync(self):
        TeamMaster.objects.create(
            team_name=official.OFFICIAL_TEAM_NAME, team_email="squat@example.com", owner=self.user
        )
        sync_command("--apply")
        owner = User.objects.get(blind_index.USER_EMAIL.matches(official.OFFICIAL_EMAIL))
        team = TeamMaster.objects.get(owner=owner)
        self.assertNotEqual(team.team_id, self.team.team_id)
        self.assertEqual(official_skills().count(), self.catalog_count)
        out = sync_command("--apply")
        self.assertIn(f"0 created, 0 updated, {self.catalog_count} unchanged", out)

    def test_a_takedown_that_lands_after_the_dry_look_is_not_overwritten(self):
        sync_command("--apply")
        skill = official_skills().get(slug=TRIAGE)
        stale = Skill.objects.get(pk=skill.pk)  # what the classify step saw: active
        Skill.objects.filter(pk=skill.pk).update(status="taken_down")
        spec = next(s for s in official.load_catalog() if s.slug == TRIAGE)
        user, team = official.find_account()
        changed = official.Spec(
            **{**spec.__dict__, "body_markdown": spec.body_markdown + "\n- new"}
        )
        official._write(changed, user, team, stale)
        self.assertEqual(skill.versions.count(), 1)

    def test_a_moderation_decision_is_never_undone(self):
        sync_command("--apply")
        skill = official_skills().get(slug=TRIAGE)
        Skill.objects.filter(pk=skill.pk).update(status="taken_down", moderation_reason="why")
        # Even with a changed text, the sync leaves it.
        base = self.catalog_for("v9.9.9")
        with mock.patch.object(official, "CATALOG_DIR", base):
            out = sync_command("--apply")
        skill.refresh_from_db()
        self.assertEqual((skill.status, skill.published_version_no), ("taken_down", 1))
        self.assertIn("skipped   triage", out)
        self.assertIn(f"{self.catalog_count - 1} updated, 0 unchanged, 1 skipped", out)

    def test_a_member_cannot_edit_or_delete_an_official_skill(self):
        sync_command("--apply")
        skill = official_skills().get(slug=TRIAGE)
        self.as_user(self.user)
        self.assertEqual(
            self.client.patch(
                f"/api/v2/skills/{skill.id}/", {"title": "x"}, format="json"
            ).status_code,
            403,
        )
        self.assertEqual(self.client.delete(f"/api/v2/skills/{skill.id}/").status_code, 403)
        skill.refresh_from_db()
        self.assertNotEqual(skill.title, "x")


class VisibilityTests(SyncBase):
    """Official skills are public skills: nothing is visible, or runs by name, while the switch is off."""

    def test_nothing_is_readable_while_the_public_switch_is_off(self):
        with override_settings(GENOS_SKILLS=True, GENOS_SKILLS_PUBLIC=False):
            sync_command("--apply")  # the sync itself runs
            skill = official_skills().get(slug=TRIAGE)
            self.assertFalse(service.can_read(skill, self.user.pk))
            self.assertFalse(service.can_read(skill, self.outsider.pk))
            self.assertFalse(
                Skill.objects.filter(service.visible_to(self.user.pk), is_official=True).exists()
            )
            with self.assertRaises(service.SkillNotFound):
                run.resolve(self.user, self.team.team_id, {"name": TRIAGE})
            self.as_user(self.user)
            self.assertEqual(self.client.get("/api/v2/skills/hub/").status_code, 404)

    def test_with_the_switch_on_an_official_skill_is_readable_and_runs_by_name(self):
        sync_command("--apply")
        skill = official_skills().get(slug=TRIAGE)
        self.assertTrue(service.can_read(skill, self.user.pk))
        self.assertTrue(service.can_read(skill, self.outsider.pk))  # any team, no install needed
        got = run.resolve(self.user, self.team.team_id, {"name": f"/{TRIAGE}"})
        self.assertEqual((got.skill_id, got.author), (str(skill.id), run.AUTHOR_OFFICIAL))
        self.assertIn("list_my_inbox", got.body_markdown)
        self.as_user(self.user)
        res = self.client.get("/api/v2/skills/hub/?official=1&limit=48")
        self.assertEqual(res.status_code, 200, res.content)
        rows = res.json()["skills"]
        self.assertEqual(len(rows), self.catalog_count)
        self.assertTrue(all(r["isOfficial"] for r in rows))
        self.assertIsNone(rows[0].get("author"))  # no personal byline, just the badge
