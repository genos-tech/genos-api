from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster

User = get_user_model()


class TestNoteViews(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass123"
        )
        self.user2 = User.objects.create_user(
            username="testuser2", email="test2@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.access_token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access_token}")

        self.team = TeamMaster.objects.create(
            team_name="Test Team",
            team_email="team@test.com",
            owner=self.user,
        )

    def _create_note(self, title="Test Note", body=None, parent_note_id=None):
        payload = {
            "team_id": str(self.team.team_id),
            "user_id": self.user.id,
            "title": title,
            "body": body or {"content": "hello"},
        }
        if parent_note_id is not None:
            payload["parent_note_id"] = parent_note_id
        return self.client.post("/api/v2/note/personal/", payload, format="json")

    # ── Create Personal Note ───────────────────────────────────────

    def test_create_personal_note(self):
        response = self._create_note()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["title"], "Test Note")
        self.assertIn("noteId", response.data)

    def test_create_personal_note_with_parent(self):
        parent = self._create_note(title="Parent")
        parent_note_id = parent.data["noteId"]
        response = self._create_note(title="Child", parent_note_id=parent_note_id)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["parentNoteId"], parent_note_id)

    def test_create_personal_note_missing_title(self):
        response = self.client.post(
            "/api/v2/note/personal/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
                "body": {"content": "no title"},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    # ── Update Personal Note ───────────────────────────────────────

    def test_update_personal_note(self):
        create_resp = self._create_note()
        note_id = create_resp.data["noteId"]

        response = self.client.put(
            "/api/v2/note/personal/",
            {
                "user_id": self.user.id,
                "note_id": note_id,
                "title": "Updated Title",
                "body": {"content": "updated"},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["title"], "Updated Title")

    def test_update_nonexistent_note(self):
        response = self.client.put(
            "/api/v2/note/personal/",
            {
                "user_id": self.user.id,
                "note_id": 999999,
                "title": "Ghost",
                "body": {"content": "nope"},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_update_note_missing_params(self):
        response = self.client.put(
            "/api/v2/note/personal/",
            {"user_id": self.user.id},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    # ── Delete Personal Note ───────────────────────────────────────

    def test_delete_personal_note(self):
        create_resp = self._create_note()
        note_id = create_resp.data["noteId"]

        response = self.client.delete(
            f"/api/v2/note/personal/?team_id={self.team.team_id}&user_id={self.user.id}&note_id={note_id}",
        )
        self.assertEqual(response.status_code, 204)

    def test_delete_nonexistent_note(self):
        response = self.client.delete(
            f"/api/v2/note/personal/?team_id={self.team.team_id}&user_id={self.user.id}&note_id=999999",
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_note_missing_params(self):
        response = self.client.delete("/api/v2/note/personal/")
        self.assertEqual(response.status_code, 400)

    # ── Get Single Note ────────────────────────────────────────────

    def test_get_single_note(self):
        create_resp = self._create_note()
        note_id = create_resp.data["noteId"]

        response = self.client.get(
            "/api/v2/note/personal/single/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
                "note_id": note_id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["noteId"], note_id)

    def test_get_single_note_not_found(self):
        response = self.client.get(
            "/api/v2/note/personal/single/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
                "note_id": 999999,
            },
        )
        self.assertEqual(response.status_code, 404)

    def test_agent_created_personal_note_is_readable_by_creator(self):
        """Regression: a personal note created by the agent `create_note`
        tool must be openable by its creator. Personal notes have no
        implicit access (`note_role.get_effective_role` returns None), so
        the tool must write a ROLE_OWNER `NotePermissionMaster` row like
        the UI create path does — otherwise the single-note GET 403s."""
        from origin.search_engine.agent.tools.base import ToolContext
        from origin.search_engine.agent.tools.create_note import _run

        result = _run(
            {
                "note_type": "personal",
                "title": "Filed by Genos",
                "content_text": "spotlight answer body",
            },
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id)),
        )
        note_id = result["note_id"]

        response = self.client.get(
            "/api/v2/note/personal/single/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
                "note_id": note_id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["title"], "Filed by Genos")

    # ── Get All Notes ──────────────────────────────────────────────

    def test_get_all_notes(self):
        self._create_note(title="Note 1")
        self._create_note(title="Note 2")

        response = self.client.get(
            "/api/v2/note/personal/all/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 2)

    def test_get_all_notes_missing_params(self):
        response = self.client.get("/api/v2/note/personal/all/")
        self.assertEqual(response.status_code, 400)

    # ── Note Meta ──────────────────────────────────────────────────

    def test_get_note_meta(self):
        self._create_note(title="Meta Note")

        response = self.client.get(
            "/api/v2/note/personal/meta/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 1)
        entry = response.data[0]
        self.assertIn("noteId", entry)
        self.assertIn("title", entry)
        self.assertNotIn("body", entry)

    # ── Favorite Notes ─────────────────────────────────────────────

    def test_favorite_note_add(self):
        create_resp = self._create_note()
        note_id = create_resp.data["noteId"]

        response = self.client.post(
            "/api/v2/note/favorite/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
                "note_id": note_id,
                "note_type": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["isFavorited"])

    def test_favorite_note_add_duplicate(self):
        create_resp = self._create_note()
        note_id = create_resp.data["noteId"]

        self.client.post(
            "/api/v2/note/favorite/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
                "note_id": note_id,
                "note_type": 1,
            },
            format="json",
        )
        response = self.client.post(
            "/api/v2/note/favorite/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
                "note_id": note_id,
                "note_type": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["isFavorited"])

    def test_favorite_note_remove(self):
        create_resp = self._create_note()
        note_id = create_resp.data["noteId"]

        self.client.post(
            "/api/v2/note/favorite/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
                "note_id": note_id,
                "note_type": 1,
            },
            format="json",
        )
        response = self.client.delete(
            f"/api/v2/note/favorite/?team_id={self.team.team_id}&user_id={self.user.id}&note_id={note_id}&note_type=1",
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["isFavorited"])

    def test_favorite_note_remove_not_found(self):
        response = self.client.delete(
            f"/api/v2/note/favorite/?team_id={self.team.team_id}&user_id={self.user.id}&note_id=999999&note_type=1",
        )
        self.assertEqual(response.status_code, 404)

    def test_get_favorite_notes_meta(self):
        create_resp = self._create_note()
        note_id = create_resp.data["noteId"]

        self.client.post(
            "/api/v2/note/favorite/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
                "note_id": note_id,
                "note_type": 1,
            },
            format="json",
        )
        response = self.client.get(
            "/api/v2/note/favorite/meta/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("personalNotes", response.data)
        self.assertEqual(len(response.data["personalNotes"]), 1)

    # ── User Isolation ─────────────────────────────────────────────

    def test_cannot_access_other_users_notes(self):
        """validate_request_user should block cross-user access with 403."""
        self._create_note()

        response = self.client.get(
            "/api/v2/note/personal/all/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user2.id,
            },
        )
        self.assertEqual(response.status_code, 403)

    # ── Unauthorized ───────────────────────────────────────────────

    def test_unauthenticated_post(self):
        client = APIClient()
        response = client.post(
            "/api/v2/note/personal/",
            {
                "team_id": str(self.team.team_id),
                "user_id": self.user.id,
                "title": "no auth",
                "body": {},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_get(self):
        client = APIClient()
        response = client.get("/api/v2/note/personal/all/")
        self.assertEqual(response.status_code, 401)
