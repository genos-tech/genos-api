"""Tests for team-scoped PROJECT labels (`/api/v2/project/label/`).

These label whole projects so a team can organize a long project list.
Not to be confused with `ProjectTags` (`/api/v2/project/tag/`), which are
per-project tags applied to the TASKS inside one project.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectLabelAssignment,
    ProjectMaster,
)

User = get_user_model()

_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "project-label-tests",
    }
}


@override_settings(CACHES=_LOCMEM_CACHE)
class TestProjectLabels(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="labelowner", email="labelowner@test.com", password="pw"
        )
        self.member = User.objects.create_user(
            username="labelmember", email="labelmember@test.com", password="pw"
        )
        self.team = TeamMaster.objects.create(
            team_name="Label Team", team_email="label@test.com", owner=self.owner
        )
        TeamMembers.objects.create(team=self.team, attendee=self.owner)
        TeamMembers.objects.create(team=self.team, attendee=self.member)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Labelled Project",
            owner=self.owner,
            project_system_user=self.owner,
        )
        self.other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other Project",
            owner=self.owner,
            project_system_user=self.owner,
        )
        self._auth(self.owner)

    def _auth(self, user):
        refresh = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")

    def _create_label(self, name="Client Work", color="#7c3aed"):
        return self.client.post(
            "/api/v2/project/label/",
            {
                "project_id": self.project.project_id,
                "name": name,
                "color": color,
                "text_color": "#ffffff",
            },
            format="json",
        )

    def _assign(self, label_ids, project=None):
        return self.client.put(
            "/api/v2/project/label/assign/",
            {
                "project_id": (project or self.project).project_id,
                "label_ids": label_ids,
            },
            format="json",
        )

    # ── Catalog CRUD ───────────────────────────────────────────────

    def test_create_label(self):
        res = self._create_label()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["name"], "Client Work")
        self.assertEqual(res.data["projectCount"], 0)
        self.assertIn("labelId", res.data)

    def test_create_label_rejects_case_variant_duplicate(self):
        self._create_label(name="Client")
        res = self._create_label(name="client")
        self.assertEqual(res.status_code, 409)

    def test_create_label_rejects_empty_name(self):
        res = self._create_label(name="   ")
        self.assertEqual(res.status_code, 400)

    def test_list_labels_includes_project_count(self):
        label_id = self._create_label().data["labelId"]
        self._assign([label_id])
        res = self.client.get(f"/api/v2/project/label/?team_id={self.team.team_id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]["projectCount"], 1)

    def test_rename_label_applies_everywhere(self):
        """The point of the normalized model: one UPDATE, every project sees it."""
        label_id = self._create_label().data["labelId"]
        self._assign([label_id])
        self._assign([label_id], project=self.other_project)

        res = self.client.put(
            "/api/v2/project/label/",
            {
                "project_id": self.project.project_id,
                "label_id": label_id,
                "name": "Renamed",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["name"], "Renamed")

        profile = self.client.get(
            f"/api/v2/project/profile/?team_id={self.team.team_id}"
            f"&project_id={self.other_project.project_id}"
        )
        self.assertEqual([lb["name"] for lb in profile.data["projectLabels"]], ["Renamed"])

    def test_rename_label_rejects_collision(self):
        first = self._create_label(name="Alpha").data["labelId"]
        self._create_label(name="Beta")
        res = self.client.put(
            "/api/v2/project/label/",
            {"project_id": self.project.project_id, "label_id": first, "name": "Beta"},
            format="json",
        )
        self.assertEqual(res.status_code, 409)

    # ── Colours (MIP-955) ──────────────────────────────────────────

    def _post_label(self, **body):
        body.setdefault("project_id", self.project.project_id)
        body.setdefault("name", "Tinted")
        return self.client.post("/api/v2/project/label/", body, format="json")

    def _put_label(self, label_id, **body):
        body.setdefault("project_id", self.project.project_id)
        return self.client.put(
            "/api/v2/project/label/", {"label_id": label_id, **body}, format="json"
        )

    def test_create_refuses_a_colour_that_cannot_fit_the_column(self):
        """The column is 10 characters: a longer value used to reach Postgres and 500."""
        res = self._post_label(color="this-is-too-long")
        self.assertEqual(res.status_code, 400, getattr(res, "data", None))
        self.assertEqual(res.data, {"error": "color must be a hex colour like #3b82f6."})
        res = self._post_label(text_color="this-is-too-long")
        self.assertEqual(res.status_code, 400, getattr(res, "data", None))
        self.assertIn("text_color", res.data["error"])
        self.assertFalse(ProjectLabel.objects.filter(name="Tinted").exists())

    def test_create_refuses_a_value_the_picker_cannot_produce(self):
        for body in (
            {"color": "red"},
            {"color": "#fff"},
            {"color": "#12345g"},
            {"color": "#3b82f6aa"},
            {"color": 5},
            {"color": ["#3b82f6"]},
            {"text_color": "purple"},
            {"text_color": 5},
            {"text_color": "#fffffff"},
        ):
            with self.subTest(body=body):
                res = self._post_label(**body)
                self.assertEqual(res.status_code, 400, getattr(res, "data", None))
                self.assertIn("error", res.data)
        self.assertFalse(ProjectLabel.objects.filter(name="Tinted").exists())

    def test_create_takes_what_the_picker_sends(self):
        """`ColorPickerMenu` sends a lowercase `#rrggbb` and `black` / `white`; the
        API's own default text colour is `#ffffff`."""
        for i, (color, text) in enumerate(
            [
                ("#ffd93b", "black"),
                ("#8e23ff", "white"),
                ("#7c3aed", "#ffffff"),
                ("#D3D3D3", "BLACK"),
            ]
        ):
            with self.subTest(color=color, text=text):
                res = self._post_label(name=f"Picked {i}", color=color, text_color=text)
                self.assertEqual(res.status_code, 201, getattr(res, "data", None))
                self.assertEqual(res.data["color"], color.lower())
                self.assertEqual(res.data["textColor"], text.lower())

    def test_create_without_colours_takes_the_defaults(self):
        for i, body in enumerate(({}, {"color": None, "text_color": ""})):
            with self.subTest(body=body):
                res = self._post_label(name=f"Plain {i}", **body)
                self.assertEqual(res.status_code, 201, getattr(res, "data", None))
                self.assertEqual((res.data["color"], res.data["textColor"]), ("#7c3aed", "#ffffff"))

    def test_recolour_refuses_a_colour_that_cannot_fit_and_changes_nothing(self):
        label_id = self._create_label(name="Keep").data["labelId"]
        for body in (
            {"color": "this-is-too-long"},
            {"text_color": "this-is-too-long"},
            {"color": "red"},
            {"name": "Renamed", "color": "#12345"},
        ):
            with self.subTest(body=body):
                res = self._put_label(label_id, **body)
                self.assertEqual(res.status_code, 400, getattr(res, "data", None))
                self.assertIn("error", res.data)
        label = ProjectLabel.objects.get(label_id=label_id)
        self.assertEqual(
            (label.name, label.color, label.text_color), ("Keep", "#7c3aed", "#ffffff")
        )

    def test_recolour_takes_what_the_picker_sends(self):
        label_id = self._create_label(name="Picked").data["labelId"]
        res = self._put_label(label_id, name="Picked too", color="#ffd93b", text_color="black")
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        self.assertEqual((res.data["color"], res.data["textColor"]), ("#ffd93b", "black"))
        label = ProjectLabel.objects.get(label_id=label_id)
        self.assertEqual((label.color, label.text_color), ("#ffd93b", "black"))

    def test_a_rename_that_sends_back_the_stored_colours_still_works(self):
        """The edit row re-sends the label's current colours with a rename. A label
        written before this check may hold a value the check would refuse; sending it
        back unchanged must not lock the label out of a rename."""
        legacy = ProjectLabel.objects.create(
            team=self.team, name="Legacy", color="#abc", text_color="white", created_by=self.owner
        )
        res = self._put_label(legacy.label_id, name="Legacy 2", color="#abc", text_color="white")
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        self.assertEqual(res.data["name"], "Legacy 2")
        # ...but moving it to a different invalid value is still refused.
        res = self._put_label(legacy.label_id, color="#abd")
        self.assertEqual(res.status_code, 400)

    def test_recolour_with_an_empty_colour_keeps_the_stored_one(self):
        label_id = self._create_label(name="Keep colour").data["labelId"]
        res = self._put_label(label_id, color="", text_color=None)
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        self.assertEqual((res.data["color"], res.data["textColor"]), ("#7c3aed", "#ffffff"))

    def test_delete_label_detaches_from_every_project(self):
        label_id = self._create_label().data["labelId"]
        self._assign([label_id])
        res = self.client.delete(
            f"/api/v2/project/label/?project_id={self.project.project_id}&label_id={label_id}"
        )
        self.assertEqual(res.status_code, 204)
        self.assertFalse(ProjectLabel.objects.filter(label_id=label_id).exists())
        self.assertFalse(ProjectLabelAssignment.objects.filter(label_id=label_id).exists())

    # ── Assignment ─────────────────────────────────────────────────

    def test_assign_replaces_full_set(self):
        a = self._create_label(name="A").data["labelId"]
        b = self._create_label(name="B").data["labelId"]
        self._assign([a, b])
        self.assertEqual(
            ProjectLabelAssignment.objects.filter(project=self.project.project_id).count(), 2
        )
        # Replace with just `a` — `b` must be detached, not accumulated.
        res = self._assign([a])
        self.assertEqual(res.status_code, 200)
        self.assertEqual([lb["labelId"] for lb in res.data], [a])
        self.assertEqual(
            ProjectLabelAssignment.objects.filter(project=self.project.project_id).count(), 1
        )

    def test_assign_is_idempotent(self):
        a = self._create_label(name="A").data["labelId"]
        self._assign([a])
        self._assign([a])
        self.assertEqual(
            ProjectLabelAssignment.objects.filter(project=self.project.project_id).count(), 1
        )

    def test_assign_ignores_label_from_another_team(self):
        other_owner = User.objects.create_user(
            username="otherteamowner", email="otherteam@test.com", password="pw"
        )
        other_team = TeamMaster.objects.create(
            team_name="Other Team", team_email="other@test.com", owner=other_owner
        )
        foreign = ProjectLabel.objects.create(
            team=other_team, name="Foreign", color="#000000", text_color="#ffffff"
        )
        res = self._assign([foreign.label_id])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, [])
        self.assertEqual(
            ProjectLabelAssignment.objects.filter(project=self.project.project_id).count(), 0
        )

    def test_assign_rejects_non_list(self):
        res = self.client.put(
            "/api/v2/project/label/assign/",
            {"project_id": self.project.project_id, "label_ids": "not-a-list"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    # ── Owner gate ─────────────────────────────────────────────────

    def test_non_owner_cannot_create_label(self):
        self._auth(self.member)
        self.assertEqual(self._create_label().status_code, 403)

    def test_non_owner_cannot_assign(self):
        label_id = self._create_label().data["labelId"]
        self._auth(self.member)
        self.assertEqual(self._assign([label_id]).status_code, 403)

    def test_non_owner_cannot_rename_or_delete(self):
        label_id = self._create_label().data["labelId"]
        self._auth(self.member)
        rename = self.client.put(
            "/api/v2/project/label/",
            {"project_id": self.project.project_id, "label_id": label_id, "name": "Nope"},
            format="json",
        )
        self.assertEqual(rename.status_code, 403)
        delete = self.client.delete(
            f"/api/v2/project/label/?project_id={self.project.project_id}&label_id={label_id}"
        )
        self.assertEqual(delete.status_code, 403)

    def test_non_owner_can_read_catalog(self):
        """Chips render for everyone — only mutations are gated."""
        self._create_label()
        self._auth(self.member)
        res = self.client.get(f"/api/v2/project/label/?team_id={self.team.team_id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 1)

    def test_unknown_project_404s(self):
        res = self.client.post(
            "/api/v2/project/label/",
            {"project_id": 999999, "name": "Ghost"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    # ── Cache invalidation ─────────────────────────────────────────
    #
    # `/project/projects/` caches per (team, viewer) for 60s and now
    # embeds `projectLabels`. Without invalidation an assignment stays
    # invisible for the rest of the TTL. LocMem (see override above) so
    # this can't accidentally pass via django-redis `delete_pattern`.

    def _list_labels_for_project(self):
        res = self.client.get(
            f"/api/v2/project/projects/?team_id={self.team.team_id}&attendee_id={self.owner.id}"
        )
        row = next(p for p in res.data if p["projectId"] == self.project.project_id)
        return [lb["name"] for lb in row["projectLabels"]]

    def test_assign_invalidates_cached_project_list(self):
        self.assertEqual(self._list_labels_for_project(), [])  # primes the cache
        label_id = self._create_label(name="Cached").data["labelId"]
        self._assign([label_id])
        self.assertEqual(self._list_labels_for_project(), ["Cached"])

    def test_rename_invalidates_cached_project_list(self):
        label_id = self._create_label(name="Before").data["labelId"]
        self._assign([label_id])
        self.assertEqual(self._list_labels_for_project(), ["Before"])  # primes the cache
        self.client.put(
            "/api/v2/project/label/",
            {"project_id": self.project.project_id, "label_id": label_id, "name": "After"},
            format="json",
        )
        self.assertEqual(self._list_labels_for_project(), ["After"])

    def test_delete_invalidates_cached_project_list(self):
        label_id = self._create_label(name="Doomed").data["labelId"]
        self._assign([label_id])
        self.assertEqual(self._list_labels_for_project(), ["Doomed"])  # primes the cache
        self.client.delete(
            f"/api/v2/project/label/?project_id={self.project.project_id}&label_id={label_id}"
        )
        self.assertEqual(self._list_labels_for_project(), [])

    def test_cache_invalidation_reaches_other_team_members(self):
        """The cache is keyed per VIEWER — one write must clear all of them."""
        self._auth(self.member)
        self.client.get(
            f"/api/v2/project/projects/?team_id={self.team.team_id}&attendee_id={self.member.id}"
        )  # primes the member's own cache entry
        self._auth(self.owner)
        label_id = self._create_label(name="Shared").data["labelId"]
        self._assign([label_id])

        self._auth(self.member)
        res = self.client.get(
            f"/api/v2/project/projects/?team_id={self.team.team_id}&attendee_id={self.member.id}"
        )
        row = next(p for p in res.data if p["projectId"] == self.project.project_id)
        self.assertEqual([lb["name"] for lb in row["projectLabels"]], ["Shared"])
