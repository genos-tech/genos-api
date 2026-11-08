"""Demo workspace v2: the visitor-facing presentation of the demo (MIP-1237).

What v2 promises, and why each is pinned:

  * **v1 is untouched.** Pinned by `test_demo_v1_golden`; here we only check that
    asking for nothing and asking for "v1" are the same call.
  * **It reads as a team.** A human team name and no `demo-<slug>` on project
    names (the slug is also why task ids read `QDC-1`); a named visitor.
  * **Every task is filed.** Tags and an effort estimate on all of them. v1 left
    both empty, so the table's Tag and Effort columns were blank.
  * **The calendar is today's.** "Q2" in the story moves with the date, in both
    languages, and never in v1.
  * **It cleans up like v1.** The logout / cron sweep finds the team by its id,
    so a v2 workspace must disappear the same way.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.services import demo_seeder, demo_v2
from origin.services.demo_seeder import create_demo_environment, delete_demo_environment

User = get_user_model()


def _user(name):
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", password="x", is_demo=True
    )


class DemoV2ContentTests(TestCase):
    """Pure content checks: no seeding."""

    def test_unknown_versions_fall_back_to_v1(self):
        for value in (None, "", "v3", 2, ["v2"], "V1 "):
            self.assertEqual(demo_v2.normalize_version(value), "v1", value)
        self.assertEqual(demo_v2.normalize_version(" V2 "), "v2")

    def test_every_task_is_filed_and_every_meta_key_is_a_task(self):
        keys = set()
        for lang in ("en", "ja"):
            adapted = demo_v2.adapt_content(demo_seeder._content(lang))
            for blueprint in adapted.PROJECT_BLUEPRINTS:
                for spec in blueprint["tasks"]:
                    keys.add(spec["key"])
                    self.assertTrue(spec["tags_json"], spec["key"])
                    self.assertIn(spec["effort_level"], demo_v2.EFFORT_CODES, spec["key"])
                    # The tag objects carry what the task table paints.
                    for tag in spec["tags_json"]:
                        self.assertEqual(set(tag), {"tagName", "tagColor", "tagTextColor"})
        self.assertEqual(keys, set(demo_v2.TASK_META))

    def test_adapting_never_mutates_the_shared_module(self):
        en = demo_seeder._content("en")
        before = [spec.get("tags_json") for b in en.PROJECT_BLUEPRINTS for spec in b["tasks"]]
        demo_v2.adapt_content(en, today=date(2026, 11, 8))
        after = [spec.get("tags_json") for b in en.PROJECT_BLUEPRINTS for spec in b["tasks"]]
        self.assertEqual(before, after)
        self.assertEqual(en.PROJECT_BLUEPRINTS[1]["name"], "Q2 Roadmap")

    def test_quarters_move_with_the_date_and_keep_their_distance(self):
        # Q2 (the story's "now") becomes the current quarter; Q3 the next.
        self.assertEqual(
            demo_v2.requarter("Q2 plan, Q3 bets, Q4 later", 2), "Q4 plan, Q1 bets, Q2 later"
        )
        self.assertEqual(demo_v2.quarter_shift(date(2026, 11, 8)), 2)
        self.assertEqual(demo_v2.quarter_shift(date(2026, 5, 1)), 0)
        # Words that merely contain a Q are left alone.
        self.assertEqual(demo_v2.requarter("FAQ2 and q3_okrs", 2), "FAQ2 and q3_okrs")

    def test_a_november_demo_is_in_q4_in_both_languages(self):
        # The story's Q2 is "now"; every other quarter keeps its distance.
        for lang, name, milestone in (
            ("en", "Q4 Roadmap", "Q4 Discovery Sprint"),
            ("ja", "Q4 ロードマップ", "Q4 ディスカバリースプリント"),
        ):
            adapted = demo_v2.adapt_content(demo_seeder._content(lang), today=date(2026, 11, 8))
            roadmap = adapted.PROJECT_BLUEPRINTS[1]
            self.assertEqual(roadmap["name"], name)
            self.assertEqual(roadmap["milestone"]["title"], milestone)
            self.assertEqual(adapted.TODO_CONTENT["cat_q2"], name)

    def test_team_name_takes_the_next_free_number(self):
        base = demo_v2.TEAM_NAMES["en"]
        self.assertEqual(demo_v2.team_name_for("en", set()), base)
        self.assertEqual(demo_v2.team_name_for("en", {base}), f"{base} 2")
        self.assertEqual(demo_v2.team_name_for("en", {base, f"{base} 2"}), f"{base} 3")


class DemoV2SeedingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.en_user = _user("v2_en")
        cls.en_team = create_demo_environment(cls.en_user, short="v2en", lang="en", version="v2")
        cls.ja_user = _user("v2_ja")
        cls.ja_team = create_demo_environment(cls.ja_user, short="v2ja", lang="ja", version="v2")

    def test_the_team_has_a_human_name_not_a_slug(self):
        self.assertEqual(self.en_team["team_name"], demo_v2.TEAM_NAMES["en"])
        self.assertNotIn("v2en", self.en_team["team_name"])
        # A second visitor in the same language gets the next number.
        second = create_demo_environment(_user("v2_en_2"), short="v2en2", lang="en", version="v2")
        self.assertEqual(second["team_name"], f"{demo_v2.TEAM_NAMES['en']} 2")

    def test_project_names_and_task_codes_are_clean(self):
        for team in (self.en_team, self.ja_team):
            projects = ProjectMaster.objects.filter(team_id=team["team_id"])
            self.assertEqual(projects.count(), 2)
            for project in projects:
                self.assertNotIn("demo-", project.project_name)
                self.assertNotEqual(project.code, "QDC")

    def test_every_task_has_tags_and_effort(self):
        tasks = TaskMaster.objects.filter(team_id=self.en_team["team_id"])
        filed = [t for t in tasks if t.tags and t.effort_level and t.effort_level_code]
        # Every blueprint task (16); milestone backing tasks are not blueprint tasks.
        self.assertEqual(len(filed), 16)

    def test_japanese_workspace_stays_japanese(self):
        names = list(
            ProjectMaster.objects.filter(team_id=self.ja_team["team_id"]).values_list(
                "project_name", flat=True
            )
        )
        self.assertTrue(any("ロードマップ" in n for n in names), names)

    def test_v2_workspaces_are_swept_like_v1(self):
        user = _user("v2_sweep")
        env = create_demo_environment(user, short="v2sweep", lang="en", version="v2")
        delete_demo_environment(user)
        self.assertFalse(TeamMaster.objects.filter(team_id=env["team_id"]).exists())
        self.assertFalse(TaskMaster.objects.filter(team_id=env["team_id"]).exists())

    def test_default_and_v1_are_the_same_call(self):
        a = create_demo_environment(_user("v1_a"), short="same1", lang="en")
        b = create_demo_environment(_user("v1_b"), short="same2", lang="en", version="v1")
        self.assertTrue(a["team_name"].startswith("Demo Team "))
        self.assertTrue(b["team_name"].startswith("Demo Team "))


class DemoV2SignInTests(TestCase):
    """The endpoint is the only caller that asks for v2."""

    url = "/api/v2/user/demo/"

    def setUp(self):
        cache.clear()  # the endpoint allows 10 signins an hour per address

    def _post(self, lang):
        return self.client.post(self.url, {"lang": lang}, content_type="application/json")

    def test_a_visitor_is_a_named_person_in_a_team_not_a_slug(self):
        for lang in ("en", "ja"):
            with self.subTest(lang=lang):
                cache.clear()
                res = self._post(lang)
                self.assertEqual(res.status_code, 201, res.content)
                body = res.json()
                self.assertEqual(body["username"], demo_v2.VISITOR_NAMES[lang])
                self.assertTrue(body["team_name"].startswith(demo_v2.TEAM_NAMES[lang]))
                self.assertNotIn("demo", body["team_name"].lower())
                self.assertEqual(User.objects.get(pk=body["user_id"]).username, body["username"])
