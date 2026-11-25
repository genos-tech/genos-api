"""The built-in `template` tag of the official skills (MIP-2398, Ken's decision E1 of 2026-11-25).

What matters: every official skill carries `template` (the pack tag it had stays); `template` is NOT
a member's catalog tag, so a member can neither make it, rename one to it, nor put it on their own
skill, and nobody can delete or recolour it; its chip has ONE fixed colour for every reader (an
official skill's other chips are grey for a stranger, and `template` must not be); a COPY of an
official skill is the member's own and drops it; and the list a schedule picks a skill from carries
the tags and chips of every row, so the picker can show and filter by them.
"""

from __future__ import annotations

from origin.models.skills.skill_models import Skill, SkillTag
from origin.services.skills import tags as skill_tags
from origin.tests.test_skills_api import LIST, detail
from origin.tests.test_skills_official import SyncBase, official_skills, sync_command
from origin.tests.test_skills_tags import TagBase, tag_url


class TemplateTagBase(SyncBase, TagBase):
    pass


class OfficialSyncTests(TemplateTagBase):
    def test_every_official_skill_carries_template_and_keeps_its_pack_tag(self):
        sync_command("--apply")
        skills = list(official_skills())
        self.assertEqual(len(skills), self.catalog_count)
        for skill in skills:
            self.assertEqual(skill.tags[0], skill_tags.TEMPLATE_TAG, skill.slug)
            self.assertGreater(len(skill.tags), 1, f"{skill.slug} lost its pack tag")
            # What a reader sees is the published version's.
            published = skill.versions.get(version_no=skill.published_version_no)
            self.assertIn(skill_tags.TEMPLATE_TAG, published.tags, skill.slug)

    def test_template_is_never_a_catalog_row(self):
        sync_command("--apply")
        self.assertFalse(SkillTag.objects.filter(name=skill_tags.TEMPLATE_TAG).exists())

    def test_a_second_sync_changes_nothing(self):
        sync_command("--apply")
        out = sync_command("--apply")
        self.assertIn(f"0 created, 0 updated, {self.catalog_count} unchanged", out)


class TheChipTests(TemplateTagBase):
    def chip(self, chips):
        return next(c for c in chips if c["name"] == skill_tags.TEMPLATE_TAG)

    def test_the_chip_has_one_fixed_colour_whoever_reads_it(self):
        fixed = {"color": skill_tags.TEMPLATE_COLOR, "textColor": skill_tags.TEMPLATE_TEXT}
        for palette, coloured in (
            (None, False),
            (None, True),
            ({"template": ("#ff2323", "white")}, True),
            ({"template": ("#ff2323", "white")}, False),
        ):
            got = self.chip(skill_tags.chips(["template", "reports"], palette, coloured=coloured))
            self.assertEqual({k: got[k] for k in fixed}, fixed, (palette, coloured))

    def test_the_other_chips_are_as_they_were(self):
        chips = skill_tags.chips(
            ["template", "reports"], {"reports": ("#ff2323", "white")}, coloured=False
        )
        other = next(c for c in chips if c["name"] == "reports")
        self.assertEqual(other["color"], skill_tags.NEUTRAL_COLOR)
        chips = skill_tags.chips(
            ["template", "reports"], {"reports": ("#ff2323", "white")}, coloured=True
        )
        other = next(c for c in chips if c["name"] == "reports")
        self.assertEqual(other["color"], "#ff2323")


class ReservedNameTests(TemplateTagBase):
    def test_a_member_cannot_make_template_a_tag(self):
        res = self.post_tag(name="template")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("built-in", res.json()["error"])
        self.assertFalse(SkillTag.objects.filter(name="template").exists())

    def test_a_member_cannot_rename_a_tag_to_template(self):
        tag = self.make_tag("reports")
        self.as_user(self.user)
        res = self.client.patch(tag_url(tag["tagId"]), {"name": "template"}, format="json")
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(SkillTag.objects.get(pk=tag["tagId"]).name, "reports")

    def test_the_editor_cannot_put_template_on_a_skill(self):
        res = self.make(tags=["template"])
        self.assertEqual(res.status_code, 400, res.content)
        self.assertFalse(Skill.objects.filter(owner=self.user).exists())

    def test_the_editor_cannot_add_template_to_a_saved_skill(self):
        skill = self.make_ok(tags=[])
        self.as_user(self.user)
        res = self.client.patch(
            detail(skill["id"]), {"team_id": self.team_id, "tags": ["template"]}, format="json"
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(Skill.objects.get(pk=skill["id"]).tags, [])


class CopyTests(TemplateTagBase):
    def test_a_copy_of_an_official_skill_is_the_members_own_and_drops_template(self):
        sync_command("--apply")
        source = official_skills().order_by("slug").first()
        pack_tags = [t for t in source.tags if t != skill_tags.TEMPLATE_TAG]
        self.as_user(self.user)
        res = self.client.post(
            detail(str(source.id), "copy/"), {"team_id": self.team_id}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        copy = res.json()["skill"]
        self.assertNotIn("template", copy["tags"])
        self.assertEqual(copy["tags"], pack_tags)
        # Nor does it land in the copier's catalog.
        self.assertFalse(SkillTag.objects.filter(owner=self.user, name="template").exists())
        self.assertEqual(
            set(SkillTag.objects.filter(owner=self.user).values_list("name", flat=True)),
            set(pack_tags),
        )


class PickerPayloadTests(TemplateTagBase):
    def rows(self):
        self.as_user(self.user)
        res = self.client.get(f"{LIST}?team_id={self.team_id}&tab=runnable")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["skills"]

    def test_every_row_carries_its_tags_and_chips_and_an_official_one_the_template_chip(self):
        sync_command("--apply")
        self.make_tag("reports", color="#ff2323", textColor="white")
        self.make_ok(tags=["reports"], slug="mine-reports")
        rows = self.rows()
        mine = next(r for r in rows if r["slug"] == "mine-reports")
        self.assertEqual(mine["tags"], ["reports"])
        self.assertEqual(
            mine["tagChips"],
            [{"name": "reports", "color": "#ff2323", "textColor": "white"}],
        )
        official = [r for r in rows if r["isOfficial"]]
        self.assertTrue(official)
        for row in official:
            self.assertEqual(row["tags"][0], "template", row["slug"])
            chip = row["tagChips"][0]
            self.assertEqual(chip["name"], "template")
            self.assertEqual(chip["color"], skill_tags.TEMPLATE_COLOR)

    def test_a_row_with_no_tags_has_empty_lists_not_missing_keys(self):
        self.make_ok(slug="plain-one", tags=[])
        row = next(r for r in self.rows() if r["slug"] == "plain-one")
        self.assertEqual((row["tags"], row["tagChips"]), ([], []))
