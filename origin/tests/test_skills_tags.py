"""The skill tag catalog (MIP-2211): pre-defined chips, picked in the editor, managed in Manage tags.

What matters: the catalog is per owner and team and only its owner changes it (a tag that is not
yours is the same 404 as a missing one); the editor's writes are strict (a name that is not in the
owner's catalog is refused, listing the names that are); a rename or a delete rewrites the owner's
DRAFT tags and never a published version or the Hub's columns; a co-editor picks from the OWNER's
list; a stranger reading a public skill gets grey chips (a personal palette would link an author's
skills together); a copy or a hand-over brings its tags (and their colours) along; and the data
migration turns the free-text tags that existed into catalog rows.
"""

from __future__ import annotations

import importlib

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_models import TeamMembers
from origin.models.skills.skill_models import Skill, SkillTag, SkillVersion
from origin.services.skills import service
from origin.services.skills import tags as skill_tags
from origin.tests.test_skills_api import LIST, SkillBase, detail

User = get_user_model()
TAGS = LIST + "tags/"


def tag_url(tag_id):
    return f"{TAGS}{tag_id}/"


class TagBase(SkillBase):
    def post_tag(self, user=None, **body):
        self.as_user(user or self.user)
        body.setdefault("team_id", self.team_id)
        return self.client.post(TAGS, body, format="json")

    def make_tag(self, name, user=None, **body):
        res = self.post_tag(user, name=name, **body)
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()

    def names(self, user=None, **params):
        self.as_user(user or self.user)
        params.setdefault("team_id", self.team_id)
        res = self.client.get(TAGS, params)
        self.assertEqual(res.status_code, 200, res.content)
        return [t["name"] for t in res.json()["tags"]]


class CatalogCrudTests(TagBase):
    def test_a_member_makes_lists_recolours_renames_and_deletes_a_tag(self):
        tag = self.make_tag("reports", color="#1dc200", textColor="white")
        self.assertEqual(
            (tag["name"], tag["color"], tag["textColor"]), ("reports", "#1dc200", "white")
        )
        self.assertEqual(self.names(), ["reports"])

        self.as_user(self.user)
        res = self.client.patch(
            tag_url(tag["tagId"]), {"color": "#0044c2", "textColor": "white"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["color"], "#0044c2")
        res = self.client.patch(tag_url(tag["tagId"]), {"name": "weekly"}, format="json")
        self.assertEqual(res.json()["name"], "weekly")
        self.assertEqual(self.names(), ["weekly"])

        self.assertEqual(self.client.delete(tag_url(tag["tagId"])).status_code, 204)
        self.assertEqual(self.names(), [])

    def test_a_colour_alone_gets_a_readable_text_colour(self):
        light = self.make_tag("light", color="#ffd93b")
        dark = self.make_tag("dark", color="#0044c2")
        self.assertEqual((light["textColor"], dark["textColor"]), ("black", "white"))

    def test_a_tag_made_by_name_alone_gets_a_palette_colour(self):
        tag = self.make_tag("plain")
        self.assertIn(tag["color"], skill_tags.PALETTE)

    def test_a_duplicate_name_is_a_409_on_create_and_on_rename(self):
        first = self.make_tag("reports")
        second = self.make_tag("weekly")
        self.assertEqual(self.post_tag(name="reports").status_code, 409)
        self.as_user(self.user)
        res = self.client.patch(tag_url(second["tagId"]), {"name": "reports"}, format="json")
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(SkillTag.objects.get(pk=first["tagId"]).name, "reports")
        self.assertEqual(SkillTag.objects.count(), 2)

    def test_the_names_follow_the_skill_tag_rules_and_the_colours_are_checked(self):
        for bad in ("Bad Tag", "x" * 25, "", "-a", "UPPER only!"):
            self.assertEqual(self.post_tag(name=bad).status_code, 400, bad)
        self.assertEqual(self.post_tag(name="ok", color="red").status_code, 400)
        self.assertEqual(
            self.post_tag(name="ok", color="#ff0000", textColor="pink").status_code, 400
        )
        self.assertEqual(SkillTag.objects.count(), 0)

    def test_a_name_is_lower_cased(self):
        self.assertEqual(self.make_tag("Reports")["name"], "reports")

    def test_the_order_of_the_list_is_the_order_they_were_made(self):
        for name in ("c-tag", "a-tag", "b-tag"):
            self.make_tag(name)
        self.assertEqual(self.names(), ["c-tag", "a-tag", "b-tag"])


class CatalogAclTests(TagBase):
    def test_the_catalog_is_per_owner_two_members_may_use_the_same_name(self):
        self.make_tag("reports")
        self.make_tag("reports", user=self.user2)
        self.assertEqual(SkillTag.objects.filter(name="reports").count(), 2)
        self.assertEqual(self.names(), ["reports"])
        self.assertEqual(self.names(user=self.user2), ["reports"])

    def test_someone_elses_tag_is_the_same_404_as_a_missing_one_for_patch_and_delete(self):
        mine = self.make_tag("reports")
        self.as_user(self.user2)
        self.assertEqual(self.client.patch(tag_url(mine["tagId"]), {"name": "x"}).status_code, 404)
        self.assertEqual(self.client.delete(tag_url(mine["tagId"])).status_code, 404)
        self.assertEqual(self.client.delete(tag_url(999999)).status_code, 404)
        self.assertEqual(SkillTag.objects.get(pk=mine["tagId"]).name, "reports")

    def test_a_catalog_is_per_team(self):
        self.make_tag("reports")
        self.assertEqual(self.post_tag(self.outsider, name="x").status_code, 404)  # not a member
        self.assertEqual(self.post_tag(name="x", team_id="not-a-uuid").status_code, 400)
        self.as_user(self.outsider)
        res = self.client.get(TAGS, {"team_id": self.team_id})
        self.assertEqual(res.status_code, 404)

    def test_a_team_or_skill_id_is_required(self):
        self.as_user(self.user)
        self.assertEqual(self.client.get(TAGS).status_code, 400)

    def test_anonymous_is_refused(self):
        self.client.credentials()
        self.assertIn(self.client.get(TAGS, {"team_id": self.team_id}).status_code, (401, 403))

    def test_the_whole_api_is_a_404_while_the_switch_is_off(self):
        from django.test import override_settings

        with override_settings(GENOS_SKILLS=False):
            self.as_user(self.user)
            self.assertEqual(self.client.get(TAGS, {"team_id": self.team_id}).status_code, 404)
            self.assertEqual(self.client.post(TAGS, {"name": "a"}).status_code, 404)


class StrictEditorTests(TagBase):
    def test_a_create_with_a_tag_that_is_not_in_the_catalog_is_refused_with_the_names_that_are(
        self,
    ):
        self.make_tag("reports")
        self.as_user(self.user)
        res = self.client.post(  # not `make`: it would put "nope" in the catalog first
            LIST,
            {"team_id": self.team_id, "title": "T", "slug": "strict", "tags": ["reports", "nope"]},
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("nope", res.json()["error"])
        self.assertEqual(res.json()["allowed"], ["reports"])
        self.assertEqual(res.json()["missing"], ["nope"])
        self.assertFalse(Skill.objects.filter(slug="strict").exists())

    def test_a_create_with_catalog_tags_works_and_carries_the_chips(self):
        self.make_tag("reports", color="#1dc200", textColor="white")
        skill = self.make_ok(tags=["reports"], slug="chips")
        self.assertEqual(skill["tags"], ["reports"])
        self.assertEqual(
            skill["tagChips"], [{"name": "reports", "color": "#1dc200", "textColor": "white"}]
        )

    def test_a_patch_with_an_unknown_tag_is_refused_and_changes_nothing(self):
        skill = self.make_ok(tags=["reports"])
        self.as_user(self.user)
        res = self.client.patch(
            detail(skill["id"]), {"tags": ["reports", "free-text"]}, format="json"
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).tags, ["reports"])

    def test_a_patch_that_keeps_a_tag_the_catalog_lost_still_saves(self):
        skill = self.make_ok(tags=["reports"])
        SkillTag.objects.all().delete()  # an old row, or data from before the catalog
        self.as_user(self.user)
        res = self.client.patch(
            detail(skill["id"]), {"tags": ["reports"], "description": "d"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)

    def test_the_tag_rules_still_apply_before_the_catalog(self):
        skill = self.make_ok()
        self.as_user(self.user)
        for tags in (["a", "b", "c", "d", "e", "f"], ["Bad Tag"], "reports"):
            res = self.client.patch(detail(skill["id"]), {"tags": tags}, format="json")
            self.assertEqual(res.status_code, 400, tags)

    def test_removing_all_tags_works(self):
        skill = self.make_ok(tags=["reports"])
        self.as_user(self.user)
        res = self.client.patch(detail(skill["id"]), {"tags": []}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["skill"]["tagChips"], [])


class RenameAndDeleteTests(TagBase):
    def test_a_rename_rewrites_the_owners_draft_tags_in_order_and_nobody_elses(self):
        tag = self.make_tag("reports")
        self.make_tag("weekly")
        mine = self.make_ok(tags=["weekly", "reports"], slug="mine")
        theirs = self.make_ok(user=self.user2, tags=["reports"], slug="theirs")
        self.as_user(self.user)
        res = self.client.patch(tag_url(tag["tagId"]), {"name": "status"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(Skill.objects.get(pk=mine["id"]).tags, ["weekly", "status"])
        self.assertEqual(Skill.objects.get(pk=theirs["id"]).tags, ["reports"])

    def test_a_delete_takes_the_name_off_the_owners_skills(self):
        tag = self.make_tag("reports")
        skill = self.make_ok(tags=["reports"], slug="a-skill")
        self.as_user(self.user)
        self.assertEqual(self.client.delete(tag_url(tag["tagId"])).status_code, 204)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).tags, [])

    def test_a_published_version_and_the_hub_keep_the_old_name_until_the_next_publish(self):
        tag = self.make_tag("reports", color="#1dc200", textColor="white")
        skill = self.make_ok(tags=["reports"], visibility="public", slug="loud")
        self.as_user(self.user)
        self.client.patch(tag_url(tag["tagId"]), {"name": "status"}, format="json")
        row = Skill.objects.get(pk=skill["id"])
        self.assertEqual(row.tags, ["status"])
        self.assertEqual(row.hub_tags, ["reports"])
        self.assertEqual(SkillVersion.objects.get(skill=row, version_no=1).tags, ["reports"])
        # The owner is told the draft differs, and the directory still filters on the published name.
        res = self.client.get(detail(skill["id"]))
        self.assertTrue(res.json()["skill"]["hasUnpublishedChanges"])
        hub = self.client.get(LIST + "hub/", {"tag": "reports"})
        self.assertEqual([s["id"] for s in hub.json()["skills"]], [skill["id"]])
        # Publishing carries the new name to readers.
        self.assertEqual(self.client.post(detail(skill["id"], "publish/")).status_code, 201)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).hub_tags, ["status"])


class ChipVisibilityTests(TagBase):
    def test_a_name_that_left_the_catalog_reads_as_a_grey_chip(self):
        tag = self.make_tag("reports", color="#1dc200", textColor="white")
        skill = self.make_ok(tags=["reports"], visibility="team", slug="team-one")
        self.as_user(self.user)
        self.client.delete(tag_url(tag["tagId"]))
        # A teammate still reads the published version, which still names the deleted tag.
        self.as_user(self.user2)
        res = self.client.get(detail(skill["id"]))
        self.assertEqual(
            res.json()["skill"]["tagChips"],
            [{"name": "reports", "color": "#808080", "textColor": "white"}],
        )

    def test_a_teammate_reading_a_team_skill_sees_the_owners_colours(self):
        self.make_tag("reports", user=self.user2, color="#ff2323", textColor="white")
        skill = self.make_ok(
            user=self.user2, tags=["reports"], visibility="team", slug="from-user2"
        )
        self.as_user(self.user)
        res = self.client.get(detail(skill["id"]))
        self.assertEqual(res.json()["skill"]["tagChips"][0]["color"], "#ff2323")
        listed = self.client.get(LIST, {"team_id": self.team_id, "tab": "team"}).json()["skills"]
        self.assertEqual(listed[0]["tagChips"][0]["color"], "#ff2323")

    def test_a_stranger_reading_a_public_skill_gets_grey_chips_the_owner_gets_colours(self):
        self.make_tag(
            "reports",
            user=self.outsider,
            team_id=str(self.other_team.team_id),
            color="#ff2323",
            textColor="white",
        )
        skill = self.make_ok(
            user=self.outsider, tags=["reports"], visibility="public", slug="open-one"
        )
        self.as_user(self.user)
        stranger = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertEqual(stranger["tags"], ["reports"])
        self.assertEqual(stranger["tagChips"][0]["color"], "#808080")
        in_hub = self.client.get(LIST + "hub/").json()["skills"][0]
        self.assertEqual(in_hub["tagChips"][0]["color"], "#808080")
        self.as_user(self.outsider)
        own = self.client.get(detail(skill["id"])).json()["skill"]
        self.assertEqual(own["tagChips"][0]["color"], "#ff2323")

    def test_a_list_asks_for_the_colours_once_however_many_rows(self):
        self.make_tag("reports")
        for i in range(6):
            self.make_ok(tags=["reports"], slug=f"s-{i}")
        self.as_user(self.user)
        with CaptureQueriesContext(connection) as few:
            self.client.get(LIST, {"team_id": self.team_id, "tab": "mine"})
        for i in range(6, 12):
            self.make_ok(tags=["reports"], slug=f"s-{i}")
        self.as_user(self.user)
        with CaptureQueriesContext(connection) as more:
            res = self.client.get(LIST, {"team_id": self.team_id, "tab": "mine"})
        self.assertEqual(len(res.json()["skills"]), 12)
        self.assertEqual(len(more), len(few))


class CoEditorTests(TagBase):
    def setUp(self):
        super().setUp()
        self.editor = User.objects.create_user(
            username="editor", email="editor@example.com", password="x-pass-123"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.editor)
        self.make_tag("owned", user=self.user2)
        self.make_tag("editors-own", user=self.editor)
        self.skill = self.make_ok(
            user=self.user2, tags=["owned"], visibility="team", slug="shared-one"
        )
        self.as_user(self.user2)
        res = self.client.post(
            detail(self.skill["id"], "editors/"), {"userId": str(self.editor.pk)}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)

    def test_a_co_editor_gets_the_owners_catalog_read_only(self):
        self.as_user(self.editor)
        res = self.client.get(TAGS, {"skill_id": self.skill["id"]})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([t["name"] for t in res.json()["tags"]], ["owned"])
        self.assertFalse(res.json()["editable"])
        self.as_user(self.user2)
        self.assertTrue(self.client.get(TAGS, {"skill_id": self.skill["id"]}).json()["editable"])

    def test_a_co_editor_picks_from_the_owners_list_not_their_own(self):
        self.as_user(self.editor)
        res = self.client.patch(detail(self.skill["id"]), {"tags": ["editors-own"]}, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        res = self.client.patch(detail(self.skill["id"]), {"tags": ["owned"]}, format="json")
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_reader_and_a_stranger_cannot_read_the_owners_catalog(self):
        reader = User.objects.create_user(
            username="reader", email="reader@example.com", password="x-pass-123"
        )
        TeamMembers.objects.create(team=self.team, attendee=reader)
        for who in (reader, self.outsider):
            self.as_user(who)
            res = self.client.get(TAGS, {"skill_id": self.skill["id"]})
            self.assertIn(res.status_code, (403, 404), who.username)

    def test_a_co_editor_cannot_change_the_owners_catalog(self):
        tag = SkillTag.objects.get(owner=self.user2, name="owned")
        self.as_user(self.editor)
        self.assertEqual(self.client.delete(tag_url(tag.tag_id)).status_code, 404)


class NonEditorPathTests(TagBase):
    def test_a_copy_brings_the_tags_and_their_colours_into_the_copiers_catalog(self):
        self.make_tag("reports", user=self.user2, color="#ff2323", textColor="white")
        skill = self.make_ok(user=self.user2, tags=["reports"], visibility="team", slug="to-copy")
        self.as_user(self.user)
        res = self.client.post(
            detail(skill["id"], "copy/"), {"team_id": self.team_id}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        mine = SkillTag.objects.get(owner=self.user, name="reports")
        self.assertEqual(mine.color, "#ff2323")
        self.assertEqual(res.json()["skill"]["tags"], ["reports"])

    def test_copying_a_public_skill_does_not_hand_the_author_their_palette(self):
        self.make_tag(
            "reports",
            user=self.outsider,
            team_id=str(self.other_team.team_id),
            color="#ff2323",
            textColor="white",
        )
        skill = self.make_ok(
            user=self.outsider, tags=["reports"], visibility="public", slug="open-copy"
        )
        self.as_user(self.user)
        res = self.client.post(
            detail(skill["id"], "copy/"), {"team_id": self.team_id}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        mine = SkillTag.objects.get(owner=self.user, name="reports")
        self.assertNotEqual(mine.color, "#ff2323")
        self.assertEqual(mine.color, skill_tags.default_color_for("reports")[0])
        self.assertNotIn(
            "#ff2323",
            [t["color"] for t in self.client.get(TAGS, {"team_id": self.team_id}).json()["tags"]],
        )

    def test_a_member_who_left_the_team_cannot_rename_or_delete_their_tags_there(self):
        tag = self.make_tag("reports", user=self.user2)
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).delete()
        self.as_user(self.user2)
        self.assertEqual(
            self.client.patch(tag_url(tag["tagId"]), {"name": "x-tag"}, format="json").status_code,
            404,
        )
        self.assertEqual(self.client.delete(tag_url(tag["tagId"])).status_code, 404)
        self.assertEqual(SkillTag.objects.get(pk=tag["tagId"]).name, "reports")

    def test_an_enormous_sort_order_is_clamped_not_a_500(self):
        tag = self.make_tag("reports")
        self.as_user(self.user)
        res = self.client.patch(tag_url(tag["tagId"]), {"sort_order": 10**12}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(SkillTag.objects.get(pk=tag["tagId"]).sort_order, skill_tags.SORT_LIMIT)

    def test_the_hand_over_gives_the_heir_the_tags_in_the_leavers_colours(self):
        self.make_tag("reports", user=self.user2, color="#8e23ff", textColor="white")
        skill = self.make_ok(user=self.user2, tags=["reports"], visibility="team", slug="handover")
        self.assertEqual(service.hand_over_team_skills(self.team.team_id, self.user2.pk), 1)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).owner_id, self.user.pk)
        heir_tag = SkillTag.objects.get(owner=self.user, name="reports")
        self.assertEqual(heir_tag.color, "#8e23ff")

    def test_service_create_ensures_missing_names_unless_strict(self):
        service.create(
            self.user, self.team.team_id, service.Fields(title="One", tags=["fresh-tag"])
        )
        self.assertEqual(SkillTag.objects.get(owner=self.user).name, "fresh-tag")
        with self.assertRaises(service.SkillError):
            service.create(
                self.user,
                self.team.team_id,
                service.Fields(title="Two", tags=["another-one"]),
                tags_mode="strict",
            )

    def test_ensure_is_safe_to_repeat(self):
        skill_tags.ensure(self.team.team_id, self.user.pk, ["a-tag", "b-tag"])
        skill_tags.ensure(self.team.team_id, self.user.pk, ["a-tag", "b-tag", "c-tag"])
        self.assertEqual(SkillTag.objects.filter(owner=self.user).count(), 3)

    def test_a_deleted_account_takes_its_catalog_with_it(self):
        self.make_tag("reports", user=self.user2)
        self.user2.delete()
        self.assertFalse(SkillTag.objects.filter(name="reports").exists())


class BackfillMigrationTests(TagBase):
    def migration(self):
        return importlib.import_module("origin.migrations.0277_skill_tags")

    def test_every_free_text_tag_of_a_skill_becomes_a_catalog_row_once(self):
        a = self.make_ok(tags=[], slug="a-skill")
        b = self.make_ok(tags=[], slug="b-skill")
        c = self.make_ok(user=self.user2, tags=[], slug="c-skill")
        Skill.objects.filter(pk=a["id"]).update(tags=["reports", "weekly"])
        Skill.objects.filter(pk=b["id"]).update(tags=["weekly", "planning"])
        Skill.objects.filter(pk=c["id"]).update(tags=["reports"])
        SkillTag.objects.all().delete()

        self.migration().backfill_catalog(apps, None)

        self.assertEqual(
            list(
                SkillTag.objects.filter(owner=self.user)
                .order_by("sort_order")
                .values_list("name", flat=True)
            ),
            ["reports", "weekly", "planning"],
        )
        self.assertEqual(
            list(SkillTag.objects.filter(owner=self.user2).values_list("name", flat=True)),
            ["reports"],
        )
        # Skills are untouched, and the editor accepts them as they are.
        self.assertEqual(Skill.objects.get(pk=a["id"]).tags, ["reports", "weekly"])
        self.as_user(self.user)
        res = self.client.patch(detail(a["id"]), {"tags": ["reports", "weekly"]}, format="json")
        self.assertEqual(res.status_code, 200, res.content)

    def test_running_it_twice_changes_nothing_and_the_colour_is_stable(self):
        a = self.make_ok(tags=[], slug="a-skill")
        Skill.objects.filter(pk=a["id"]).update(tags=["reports"])
        SkillTag.objects.all().delete()
        self.migration().backfill_catalog(apps, None)
        first = SkillTag.objects.get(owner=self.user, name="reports")
        self.migration().backfill_catalog(apps, None)
        self.assertEqual(SkillTag.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(first.color, skill_tags.default_color_for("reports")[0])
        self.assertIn(first.text_color, ("black", "white"))
