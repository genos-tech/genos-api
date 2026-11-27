"""The `dashboard` tag of a skill that can make a dashboard (MIP-2418).

The dashboard form lists only skills carrying it; the server never refuses a skill for lacking it.
What matters here: exactly the chosen official skills carry it, and a catalog rebuild keeps it; an
earlier sync gets it as a new version; it is an ordinary tag (a member makes it and puts it on their
own skill, and a copy of an official skill keeps it); and the list a schedule picks from says so.
"""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path
from unittest import mock

from django.conf import settings

from origin.models.skills.skill_models import SkillTag
from origin.services.skills import official
from origin.services.skills.official_slugs import OFFICIAL_SLUGS
from origin.tests.test_skills_api import LIST, detail
from origin.tests.test_skills_official import SyncBase, official_skills, sync_command
from origin.tests.test_skills_tags import TagBase

DASHBOARD = "dashboard"


def build_script():
    path = Path(settings.BASE_DIR) / "scripts" / "build_official_skills.py"
    spec = importlib.util.spec_from_file_location("build_official_skills", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DashboardTagTests(SyncBase, TagBase):
    def chosen(self) -> set[str]:
        return set(build_script().DASHBOARD_SKILLS)

    def rows(self):
        self.as_user(self.user)
        res = self.client.get(f"{LIST}?team_id={self.team_id}&tab=runnable")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["skills"]

    def test_the_committed_catalog_tags_exactly_what_the_build_script_names(self):
        script = build_script()
        self.assertEqual(script.DASHBOARD_TAG, DASHBOARD)
        self.assertLessEqual(self.chosen(), set(OFFICIAL_SLUGS))
        tagged = {s.slug for s in official.load_catalog() if DASHBOARD in s.tags}
        self.assertEqual(tagged, self.chosen())

    def test_a_synced_official_skill_publishes_the_tag_and_the_picker_sees_it(self):
        sync_command("--apply")
        listed = {r["slug"] for r in self.rows() if r["isOfficial"] and DASHBOARD in r["tags"]}
        self.assertEqual(listed, self.chosen())

    def test_a_catalog_synced_before_the_tag_gets_it_as_a_new_version(self):
        untagged = [
            replace(s, tags=[t for t in s.tags if t != DASHBOARD]) for s in official.load_catalog()
        ]
        with mock.patch.object(official, "load_catalog", return_value=untagged):
            sync_command("--apply")
        count = len(self.chosen())
        out = sync_command("--apply")
        self.assertIn(f"0 created, {count} updated, {self.catalog_count - count} unchanged", out)
        for skill in official_skills().filter(slug__in=self.chosen()):
            published = skill.versions.get(version_no=skill.published_version_no)
            self.assertEqual(skill.published_version_no, 2, skill.slug)
            self.assertIn(DASHBOARD, published.tags, skill.slug)

    def test_a_copy_of_an_official_skill_keeps_it(self):
        sync_command("--apply")
        source = official_skills().get(slug="status-report")
        self.as_user(self.user)
        res = self.client.post(
            detail(str(source.id), "copy/"), {"team_id": self.team_id}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        tags = res.json()["skill"]["tags"]
        self.assertIn(DASHBOARD, tags)
        self.assertNotIn("template", tags)
        self.assertTrue(SkillTag.objects.filter(owner=self.user, name=DASHBOARD).exists())

    def test_a_member_puts_it_on_their_own_skill_and_the_picker_sees_it(self):
        self.make_tag(DASHBOARD)
        self.make_ok(slug="my-numbers", tags=[DASHBOARD])
        row = next(r for r in self.rows() if r["slug"] == "my-numbers")
        self.assertEqual(row["tags"], [DASHBOARD])
