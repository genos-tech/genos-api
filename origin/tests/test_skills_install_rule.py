"""A skill can be picked or run only when it is the member's own or one they installed (MIP-2589).

Covered here: the default installs (new accounts, the first official sync, an uninstall that stays),
installing and uninstalling a TEAM skill (and losing it with the team), the REST answers of a run and
of the lists, and the data migration that keeps what already worked. The picker, the by-name and by-id
lookups, the schedule and MCP refusals are in the tests of each of those paths.
"""

from __future__ import annotations

import importlib

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import override_settings

from origin.models.common.schedule_models import MemberSchedule
from origin.models.skills.skill_models import Skill, SkillInstall, SkillRunLog
from origin.search_engine.models import AgentRun
from origin.services import team_membership
from origin.services.skills import defaults, run, service
from origin.tests.test_skills_api import LIST, detail
from origin.tests.test_skills_official import official_skills, sync_command
from origin.tests.test_skills_run import RunBase

User = get_user_model()

migration = importlib.import_module("origin.migrations.0307_skill_installs_before_install_rule")


def installs(user) -> set[str]:
    return set(SkillInstall.objects.filter(user=user).values_list("skill__slug", flat=True))


@override_settings(GENOS_SKILLS=True, GENOS_SKILLS_PUBLIC=True)
class DefaultInstallTests(RunBase):
    def test_the_first_sync_installs_the_defaults_for_the_accounts_that_exist(self):
        sync_command("--apply")
        for user in (self.user, self.user2, self.outsider):
            self.assertEqual(installs(user), set(defaults.DEFAULT_SKILL_SLUGS), user.username)
        # The Hub's count is of people who chose to install.
        self.assertEqual({s.install_count for s in official_skills()}, {0})
        self.assertFalse(SkillInstall.objects.filter(counted=True).exists())

    def test_a_new_account_gets_them_and_a_system_account_does_not(self):
        sync_command("--apply")
        new = User.objects.create_user(username="new", email="new@example.com", password="x-pass-1")
        self.assertEqual(installs(new), set(defaults.DEFAULT_SKILL_SLUGS))
        system = User.objects.create_user(
            username="bot", email="bot@example.com", password=None, is_system_user=True
        )
        self.assertEqual(installs(system), set())

    def test_an_account_made_before_the_sync_has_none_and_nothing_breaks(self):
        new = User.objects.create_user(username="new", email="new@example.com", password="x-pass-1")
        self.assertEqual(installs(new), set())

    def test_the_defaults_are_offered_and_run_and_no_other_official_skill_is(self):
        sync_command("--apply")
        rows = run.runnable(self.user, self.team.team_id)
        self.assertEqual(
            {(r["group"], r["slug"]) for r in rows},
            {("installed", slug) for slug in defaults.DEFAULT_SKILL_SLUGS},
        )
        self.assertEqual(self.resolve({"name": "triage"}).author, run.AUTHOR_OFFICIAL)
        with self.assertRaises(run.InstallRequired) as caught:
            self.resolve({"name": "status-report"})
        self.assertEqual(caught.exception.extra["where"], "hub")

    def test_a_default_can_be_uninstalled_and_stays_uninstalled(self):
        sync_command("--apply")
        triage = official_skills().get(slug="triage")
        service.uninstall(self.user, triage.pk)
        sync_command("--apply")  # nothing is created again
        self.assertNotIn("triage", installs(self.user))
        triage.refresh_from_db()
        self.assertEqual(triage.install_count, 0)
        with self.assertRaises(run.InstallRequired):
            self.resolve({"name": "triage"})
        # Installing it again is an ordinary, counted install.
        service.install(self.user, triage.pk)
        triage.refresh_from_db()
        self.assertEqual(triage.install_count, 1)
        service.uninstall(self.user, triage.pk)
        triage.refresh_from_db()
        self.assertEqual(triage.install_count, 0)


@override_settings(GENOS_SKILLS=True, GENOS_SKILLS_PUBLIC=True)
class TeamInstallTests(RunBase):
    def theirs(self, **over):
        return self.skill(self.user2, "team", install=False, **over)

    def test_installing_makes_it_pickable_and_uninstalling_takes_it_away(self):
        skill = self.theirs()
        self.assertEqual(run.runnable(self.user, self.team.team_id), [])
        self.as_user(self.user)
        self.assertEqual(self.client.post(detail(str(skill.id), "install/")).status_code, 201)
        (row,) = run.runnable(self.user, self.team.team_id)
        self.assertEqual(
            (row["group"], row["id"], row["ownerName"]), ("team", str(skill.id), "otheruser")
        )
        self.assertEqual(self.client.delete(detail(str(skill.id), "install/")).status_code, 204)
        self.assertEqual(run.runnable(self.user, self.team.team_id), [])

    def test_leaving_the_team_ends_what_was_installed_from_it(self):
        skill = self.skill(self.user, "team", slug="from-the-team")
        service.install(self.user2, skill.id)
        team_membership.remove_team_member(self.team.team_id, self.user2.pk)
        self.assertEqual(list(service.list_for(self.user2, None, "installed")), [])
        with self.assertRaises(service.SkillError):
            run.resolve(self.user2, self.team.team_id, {"id": str(skill.id)})

    def test_another_teams_member_cannot_install_it(self):
        skill = self.theirs()
        with self.assertRaises(service.SkillNotFound):
            service.install(self.outsider, skill.id)

    def test_a_private_or_hidden_skill_cannot_be_installed(self):
        private = self.skill(self.user2, "private", slug="private-one")
        with self.assertRaises(service.SkillError):
            service.install(self.user, private.id)
        hidden = self.theirs(slug="hidden-one")
        Skill.objects.filter(pk=hidden.pk).update(status="hidden_by_team")
        with self.assertRaises(service.SkillError):
            service.install(self.user, hidden.id)

    def test_it_stays_on_the_installed_version_until_the_update_is_taken(self):
        skill = self.theirs(body_markdown="STEP: one")
        service.install(self.user, skill.id)
        service.update(self.user2, skill.id, service.Fields(body_markdown="STEP: two"))
        service.publish(self.user2, skill.id)
        self.assertIn("STEP: one", self.resolve({"id": str(skill.id)}).body_markdown)
        diff = service.update_diff(self.user, skill.id)
        self.assertEqual((diff["fromVersionNo"], diff["toVersionNo"]), (1, 2))
        self.assertFalse(diff["needsConfirmation"])  # a teammate is not an outside author
        service.accept_update(self.user, skill.id)
        self.assertIn("STEP: two", self.resolve({"id": str(skill.id)}).body_markdown)

    def test_the_picker_says_whether_there_is_a_hub(self):
        self.as_user(self.user)
        url = f"{LIST}?team_id={self.team_id}&tab=runnable"
        self.assertTrue(self.client.get(url).json()["publicEnabled"])
        with override_settings(GENOS_SKILLS_PUBLIC=False):
            self.assertFalse(self.client.get(url).json()["publicEnabled"])

    def test_the_team_tab_says_which_rows_are_installed(self):
        have = self.theirs(slug="have")
        self.theirs(slug="have-not")
        service.install(self.user, have.id)
        self.as_user(self.user)
        res = self.client.get(f"{LIST}?team_id={self.team_id}&tab=team")
        rows = {r["slug"]: r for r in res.json()["skills"]}
        self.assertEqual(rows["have"]["installedVersionNo"], 1)
        self.assertNotIn("installedVersionNo", rows["have-not"])

    def test_the_installed_tab_lists_this_teams_and_public_installs(self):
        mine_here = self.theirs(slug="here")
        public = self.public_skill(slug="public-one")
        service.install(self.user, mine_here.id)
        self.as_user(self.user)
        res = self.client.get(f"{LIST}?team_id={self.team_id}&tab=installed")
        self.assertEqual({r["slug"] for r in res.json()["skills"]}, {"here", "public-one"})
        rows = service.list_for(self.user, self.other_team.team_id, "installed")
        self.assertEqual([r.pk for r in rows], [public.pk])


@override_settings(GENOS_SKILLS=True, GENOS_SKILLS_PUBLIC=True)
class AnswerTests(RunBase):
    def test_an_ask_by_name_says_where_to_install_before_anything_is_spent(self):
        from origin.tests.test_skills_run import AskTests  # noqa: PLC0415

        skill = self.skill(self.user2, "team", install=False)
        resp, got = AskTests.ask(self, {"name": "report"})
        self.assertEqual(resp.status_code, 403, getattr(resp, "content", b""))
        body = resp.json()
        self.assertEqual(
            (body["code"], body["where"], body["slug"], body["skillId"]),
            ("install_required", "team", "report", str(skill.id)),
        )
        self.assertNotIn("kwargs", got)
        self.assertFalse(AgentRun.objects.exists())

    def test_a_typed_word_that_is_no_skill_is_an_ordinary_question(self):
        from origin.tests.test_skills_run import AskTests  # noqa: PLC0415

        self.skill(self.user2, "team", install=False)
        resp, got = AskTests.ask(self, {"name": "tmp", "optional": True}, query="/tmp is full")
        self.assertEqual(resp.status_code, 200, getattr(resp, "content", b""))
        self.assertNotIn("SKILL RUN", got["kwargs"].get("system_extra") or "")
        # ... and a typed name that IS a skill to install is still refused.
        resp, got = AskTests.ask(self, {"name": "report", "optional": True})
        self.assertEqual((resp.status_code, resp.json()["code"]), (403, "install_required"))
        # Without `optional` (an id or a picked name) a missing skill stays a 404.
        resp, _ = AskTests.ask(self, {"name": "tmp"})
        self.assertEqual(resp.status_code, 404)


@override_settings(GENOS_SKILLS=True, GENOS_SKILLS_PUBLIC=True)
class KeepWhatWorkedMigrationTests(RunBase):
    """`0307`: run with the current models, which is what it runs against right after `0306`."""

    def migrate(self):
        SkillInstall.objects.all().delete()
        migration.forwards(apps, None)

    def used_by_schedule(self, skill, user=None, job="run_skill"):
        MemberSchedule.objects.create(
            user=user or self.user,
            team=self.team,
            job=job,
            params={"skill_id": str(skill.id)},
            frequency="daily",
            time_of_day="08:00",
        )

    def official(self, slug):
        skill = self.skill(self.outsider, "public", slug=slug)
        Skill.objects.filter(pk=skill.pk).update(is_official=True)
        return skill

    def used_by_run(self, skill, user=None):
        AgentRun.objects.create(
            team_id=str(self.team.team_id),
            user_id=str((user or self.user).pk),
            query="/x",
            skill_ref={"id": str(skill.id), "version_no": 1, "slug": skill.slug},
        )

    def test_official_and_team_skills_a_schedule_or_a_past_run_uses_are_installed(self):
        by_schedule = self.official("by-schedule")
        by_dashboard = self.official("by-dashboard")
        by_run = self.skill(self.user2, "team", install=False, slug="by-run")
        by_log = self.official("by-log")
        self.official("never-used")
        self.used_by_schedule(by_schedule)
        self.used_by_schedule(by_dashboard, job="ai_dashboard")
        self.used_by_run(by_run)
        SkillRunLog.objects.create(skill=by_log, user=self.user, version_no=1)
        self.migrate()
        self.assertEqual(installs(self.user), {"by-schedule", "by-dashboard", "by-run", "by-log"})
        self.assertEqual(installs(self.user2), set())
        self.assertFalse(SkillInstall.objects.filter(counted=True).exists())
        before = SkillInstall.objects.count()
        migration.forwards(apps, None)  # running it again changes nothing
        self.assertEqual(SkillInstall.objects.count(), before)

    def test_what_may_not_be_kept_is_not_installed(self):
        public = self.skill(self.outsider, "public", slug="uninstalled-public")  # removed by them
        mine = self.skill(self.user, "team", slug="mine")
        other_team = self.skill(self.outsider, "team", slug="other-team")
        gone = self.official("taken-down")
        Skill.objects.filter(pk=gone.pk).update(status="taken_down")
        private = self.skill(self.user2, "private", slug="private")
        for skill in (public, mine, other_team, gone, private):
            self.used_by_run(skill)
        self.used_by_run(public, user=self.outsider)  # its own author
        AgentRun.objects.create(
            team_id=str(self.team.team_id),
            user_id="not-a-uuid",
            query="x",
            skill_ref={"id": "nonsense"},
        )
        self.migrate()
        self.assertEqual(SkillInstall.objects.count(), 0)

    def test_every_existing_account_gets_the_defaults(self):
        sync_command("--apply")
        self.migrate()
        self.assertEqual(installs(self.user), set(defaults.DEFAULT_SKILL_SLUGS))
        official_owner = official_skills().first().owner
        self.assertEqual(installs(official_owner), set())

    def test_the_migration_names_the_same_defaults_as_the_code(self):
        self.assertEqual(migration.DEFAULT_SKILL_SLUGS, defaults.DEFAULT_SKILL_SLUGS)
