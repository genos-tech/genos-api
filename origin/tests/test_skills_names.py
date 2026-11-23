"""A skill's name is unique where it is read from (MIP-2210).

Before: one rule, unique per owner within a team, whatever the visibility, answered with a 409.
Now: private = unique among the owner's skills; team = also unique in the team; public = also unique
among all public skills; a clash is a 400 with a field error; and a live check answers the editor.
"""

from __future__ import annotations

import importlib
from unittest import mock

from django.apps import apps
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import override_settings

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.skills.skill_models import Skill
from origin.services.skills import service
from origin.tests.test_skills_api import LIST, SkillBase, detail

CHECK = LIST + "name-check/"


class NameRuleBase(SkillBase):
    def check(self, user=None, **params):
        self.as_user(user or self.user)
        params.setdefault("team_id", self.team_id)
        return self.client.get(CHECK, params)

    def assert_name_taken(self, res):
        self.assertEqual(res.status_code, 400, res.content)
        body = res.json()
        self.assertEqual(body["code"], "name_taken")
        self.assertEqual(body["fieldErrors"], {"slug": [body["error"]]})
        return body["error"]


class PrivateNameTests(NameRuleBase):
    def test_a_private_name_is_unique_among_the_owners_skills(self):
        self.make_ok(slug="mine")
        self.assert_name_taken(self.make(slug="mine"))
        self.assertEqual(Skill.objects.count(), 1)

    def test_private_skills_of_two_people_may_share_a_name(self):
        self.make_ok(slug="mine")
        self.make_ok(user=self.user2, slug="mine")
        self.assertEqual(Skill.objects.filter(slug="mine").count(), 2)

    def test_a_private_name_may_equal_a_team_or_public_name_of_somebody_else(self):
        self.make_ok(user=self.user2, slug="shared", visibility="team")
        self.make_ok(user=self.user2, slug="loud", visibility="public")
        self.make_ok(slug="shared")
        self.make_ok(slug="loud")

    def test_a_rename_into_the_owners_other_name_is_refused(self):
        self.make_ok(slug="first")
        other = self.make_ok(slug="second")
        self.as_user(self.user)
        res = self.client.patch(detail(other["id"]), {"slug": "first"}, format="json")
        self.assert_name_taken(res)
        self.assertEqual(Skill.objects.get(pk=other["id"]).slug, "second")


class TeamNameTests(NameRuleBase):
    def test_a_team_skills_name_is_unique_in_the_team(self):
        self.make_ok(slug="standup", visibility="team")
        message = self.assert_name_taken(
            self.make(user=self.user2, slug="standup", visibility="team")
        )
        self.assertIn("team", message)
        self.assertEqual(Skill.objects.filter(slug="standup").count(), 1)
        # Nothing half-made: the refused create left no body note either.
        self.assertEqual(Skill.objects.count(), 1)

    def test_sharing_a_private_skill_with_a_taken_team_name_is_refused(self):
        self.make_ok(slug="standup", visibility="team")
        mine = self.make_ok(user=self.user2, slug="standup")  # private: fine
        self.as_user(self.user2)
        res = self.client.patch(detail(mine["id"]), {"visibility": "team"}, format="json")
        self.assert_name_taken(res)
        row = Skill.objects.get(pk=mine["id"])
        self.assertEqual((row.visibility, row.current_version_no), ("private", 0))

    def test_renaming_a_team_skill_into_a_taken_team_name_is_refused(self):
        self.make_ok(slug="standup", visibility="team")
        mine = self.make_ok(user=self.user2, slug="retro", visibility="team")
        self.as_user(self.user2)
        res = self.client.patch(detail(mine["id"]), {"slug": "standup"}, format="json")
        self.assert_name_taken(res)

    def test_a_rename_and_a_share_in_one_call_are_checked_for_where_it_goes(self):
        self.make_ok(slug="standup", visibility="team")
        mine = self.make_ok(user=self.user2, slug="retro")
        self.as_user(self.user2)
        res = self.client.patch(
            detail(mine["id"]), {"slug": "standup", "visibility": "team"}, format="json"
        )
        self.assert_name_taken(res)
        self.assertEqual(Skill.objects.get(pk=mine["id"]).slug, "retro")

    def test_going_private_frees_the_team_name(self):
        first = self.make_ok(slug="standup", visibility="team")
        self.as_user(self.user)
        self.client.patch(detail(first["id"]), {"visibility": "private"}, format="json")
        self.make_ok(user=self.user2, slug="standup", visibility="team")

    def test_a_team_name_is_not_shared_with_another_team(self):
        self.make_ok(slug="standup", visibility="team")
        second = TeamMaster.objects.create(
            team_name="Second", team_email="second@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=second, attendee=self.user2)
        self.as_user(self.user2)
        res = self.client.post(
            LIST,
            {
                "team_id": str(second.team_id),
                "title": "Standup",
                "slug": "standup",
                "visibility": "team",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)

    def test_an_unnamed_team_skill_gets_the_next_free_name(self):
        self.make_ok(slug="daily-standup", visibility="team")
        res = self.make(user=self.user2, slug="", title="Daily standup", visibility="team")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["skill"]["slug"], "daily-standup-2")

    def test_the_handover_keeps_names_unique_in_the_team(self):
        owner_skill = self.make_ok(slug="standup")  # the team owner's own private skill
        leaver = self.make_ok(user=self.user2, slug="standup", visibility="team")
        moved = service.hand_over_team_skills(self.team.team_id, self.user2.pk)
        self.assertEqual(moved, 1)
        row = Skill.objects.get(pk=leaver["id"])
        self.assertEqual(row.owner_id, self.team.owner_id)
        self.assertEqual(row.slug, "standup-2")  # the heir already had `standup`
        self.assertEqual(Skill.objects.get(pk=owner_skill["id"]).slug, "standup")


class PublicNameTests(NameRuleBase):
    def test_a_public_name_is_unique_across_all_public_skills(self):
        self.make_ok(slug="planner", visibility="public")
        message = self.assert_name_taken(
            self.make(user=self.outsider, slug="planner", visibility="public")
        )
        self.assertIn("public", message)

    def test_publishing_a_private_skill_with_a_taken_public_name_is_refused(self):
        self.make_ok(slug="planner", visibility="public")
        mine = self.make_ok(user=self.outsider, slug="planner")  # private elsewhere: fine
        self.as_user(self.outsider)
        res = self.client.patch(detail(mine["id"]), {"visibility": "public"}, format="json")
        self.assert_name_taken(res)
        self.assertEqual(Skill.objects.get(pk=mine["id"]).visibility, "private")

    def test_a_team_skill_may_go_public_only_with_a_free_public_name(self):
        self.make_ok(slug="planner", visibility="public")
        mine = self.make_ok(user=self.user2, slug="planner", visibility="team")
        self.as_user(self.user2)
        res = self.client.patch(detail(mine["id"]), {"visibility": "public"}, format="json")
        self.assert_name_taken(res)
        self.assertEqual(Skill.objects.get(pk=mine["id"]).visibility, "team")
        # A new name makes it work, in one call.
        res = self.client.patch(
            detail(mine["id"]), {"slug": "planner-2", "visibility": "public"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)

    def test_renaming_while_public_is_checked_against_the_public_names(self):
        self.make_ok(slug="planner", visibility="public")
        mine = self.make_ok(user=self.outsider, slug="notes", visibility="public")
        self.as_user(self.outsider)
        res = self.client.patch(detail(mine["id"]), {"slug": "planner"}, format="json")
        self.assert_name_taken(res)
        res = self.client.patch(detail(mine["id"]), {"slug": "notes-pro"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)

    def test_renaming_a_private_skill_to_a_public_name_is_allowed(self):
        self.make_ok(slug="planner", visibility="public")
        mine = self.make_ok(user=self.outsider, slug="notes")
        self.as_user(self.outsider)
        res = self.client.patch(detail(mine["id"]), {"slug": "planner"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)

    def test_an_unnamed_public_skill_gets_the_next_free_name(self):
        self.make_ok(slug="daily-standup", visibility="public")
        res = self.make(user=self.outsider, slug="", title="Daily standup", visibility="public")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["skill"]["slug"], "daily-standup-2")


class DatabaseNetTests(NameRuleBase):
    def test_the_database_refuses_two_team_skills_with_one_name_in_a_team(self):
        first = self.make_ok(slug="standup", visibility="team")
        row = Skill.objects.get(pk=first["id"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Skill.objects.create(
                team=row.team, owner=self.user2, slug="standup", title="Dup", visibility="team"
            )

    def test_the_database_refuses_two_public_skills_with_one_name(self):
        first = self.make_ok(slug="planner", visibility="public")
        row = Skill.objects.get(pk=first["id"])
        with self.assertRaises(IntegrityError), transaction.atomic():
            Skill.objects.create(
                team=row.team, owner=self.user2, slug="planner", title="Dup", visibility="public"
            )

    def test_a_save_that_loses_a_race_is_the_same_400(self):
        mine = self.make_ok(user=self.user2, slug="standup")
        self.make_ok(slug="standup", visibility="team")  # somebody takes the team name meanwhile
        self.as_user(self.user2)
        # The check had passed (nobody had the name yet): only the unique index is left to refuse.
        with mock.patch.object(service, "require_name_free", return_value=None):
            res = self.client.patch(detail(mine["id"]), {"visibility": "team"}, format="json")
        self.assert_name_taken(res)
        self.assertEqual(Skill.objects.get(pk=mine["id"]).visibility, "private")


class NameCheckEndpointTests(NameRuleBase):
    def test_a_free_name_is_available(self):
        res = self.check(slug="Fresh-Name")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), {"available": True, "slug": "fresh-name", "message": ""})

    def test_the_callers_own_name_is_taken_unless_it_is_the_skill_being_edited(self):
        mine = self.make_ok(slug="mine")
        body = self.check(slug="mine").json()
        self.assertFalse(body["available"])
        self.assertIn("mine", body["message"])
        self.assertTrue(self.check(slug="mine", skill_id=mine["id"]).json()["available"])

    def test_the_answer_depends_on_where_the_skill_is_shared(self):
        self.make_ok(user=self.user2, slug="shared", visibility="team")
        self.make_ok(user=self.user2, slug="loud", visibility="public")
        for slug, visibility, available in (
            ("shared", "private", True),
            ("shared", "team", False),
            ("shared", "public", True),
            ("loud", "private", True),
            ("loud", "team", True),
            ("loud", "public", False),
        ):
            body = self.check(slug=slug, visibility=visibility).json()
            self.assertEqual(body["available"], available, (slug, visibility, body))

    def test_a_private_skill_of_somebody_else_never_shows(self):
        self.make_ok(user=self.user2, slug="secret")
        for visibility in ("private", "team", "public"):
            self.assertTrue(self.check(slug="secret", visibility=visibility).json()["available"])

    def test_someone_elses_skill_id_does_not_hide_a_clash(self):
        theirs = self.make_ok(user=self.user2, slug="theirs", visibility="team")
        body = self.check(slug="theirs", visibility="team", skill_id=theirs["id"]).json()
        self.assertFalse(body["available"])

    def test_a_badly_formed_name_is_not_available_with_the_rule(self):
        for bad in ("a", "Has Space", "-lead", "help", ""):
            body = self.check(slug=bad).json()
            self.assertFalse(body["available"], bad)
            self.assertTrue(body["message"], bad)

    def test_an_unknown_visibility_or_a_bad_id_is_a_400(self):
        self.assertEqual(self.check(slug="ok-name", visibility="everyone").status_code, 400)
        self.assertEqual(self.check(slug="ok-name", skill_id="nope").status_code, 400)
        self.assertEqual(self.client.get(CHECK).status_code, 400)  # no team_id

    def test_a_non_member_gets_a_404(self):
        res = self.check(user=self.outsider, slug="ok-name")
        self.assertEqual(res.status_code, 404)

    def test_it_is_a_404_while_the_switch_is_off(self):
        with override_settings(GENOS_SKILLS=False):
            self.assertEqual(self.check(slug="ok-name").status_code, 404)


class MigrationTests(NameRuleBase):
    """The data step renames the clashes before the indexes exist: the oldest keeps its name."""

    def run_step(self):
        module = importlib.import_module("origin.migrations.0275_skill_unique_names")
        module.rename_clashes(apps, None)

    def drop_indexes(self):
        # Make the clash possible, as in a database from before the rule.
        from django.db import connection

        with connection.cursor() as cur:
            cur.execute("DROP INDEX IF EXISTS skill_slug_unique_in_team")
            cur.execute("DROP INDEX IF EXISTS skill_slug_unique_public")

    def test_clashing_team_and_public_names_are_renamed_oldest_first(self):
        self.drop_indexes()
        make = lambda owner, slug, visibility: Skill.objects.create(  # noqa: E731
            team=self.team, owner=owner, slug=slug, title=slug, visibility=visibility
        )
        old = make(self.user, "standup", "team")
        newer = make(self.user2, "standup", "team")
        taken = make(self.user2, "standup-2", "private")  # `-2` is used by the new one's owner
        pub_old = make(self.user, "planner", "public")
        pub_new = make(self.user2, "planner", "public")
        solo = make(self.user, "solo", "team")
        self.run_step()
        slug = lambda s: Skill.objects.get(pk=s.pk).slug  # noqa: E731
        self.assertEqual(slug(old), "standup")
        self.assertEqual(slug(newer), "standup-3")  # `standup-2` is the owner's other skill
        self.assertEqual(slug(taken), "standup-2")
        self.assertEqual(slug(pub_old), "planner")
        self.assertEqual(slug(pub_new), "planner-2")
        self.assertEqual(slug(solo), "solo")

    def test_a_long_name_keeps_the_suffix_within_the_limit(self):
        self.drop_indexes()
        long = "a" * 40
        Skill.objects.create(
            team=self.team, owner=self.user, slug=long, title="x", visibility="team"
        )
        newer = Skill.objects.create(
            team=self.team, owner=self.user2, slug=long, title="y", visibility="team"
        )
        self.run_step()
        self.assertEqual(Skill.objects.get(pk=newer.pk).slug, "a" * 38 + "-2")

    def tearDown(self):
        cache.clear()
        super().tearDown()
