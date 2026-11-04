"""The note-folder edits and the team-share offer, once they stopped reading `request.data[...]` (MIP-1020).

None of these four reads could raise: each `request.data["name"]` sat behind
`"name" in request.data and request.data.get("name") is not None`, and the share offer's
`data["team_id"]` etc. came after `any(not data.get(k) for k in required)`. They are rewritten
(`.get`, and `body_object`) so the AST test in `test_no_request_data_index.py` can hold the
whole of `origin/views` to "no `request.data[...]`", and these tests pin that the edit did
not move: the same statuses and the same words for `{}`, a partial body, a name that is
absent, `null`, empty, too long, or not text, and a valid rename.
"""

from origin.models.note.common_note_models import NoteFolderTag
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.tests.test_base import BaseAPITestCase

PERSONAL = "/api/v2/note/personal/folder/"
TEAM = "/api/v2/note/team/folder/"
TAG = "/api/v2/note/team/tag/"
SHARE = "/api/v2/team/share/"


class NoteFolderNameReadTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.team_id = str(self.team.team_id)
        self.user_id = str(self.user.id)
        personal = self.client.post(
            PERSONAL,
            {"team_id": self.team_id, "user_id": self.user_id, "name": "pf"},
            format="json",
        )
        self.assertEqual(personal.status_code, 201, personal.content)
        self.personal_id = personal.json()["folderId"]
        team = self.client.post(
            TEAM,
            {
                "team_id": self.team_id,
                "user_id": self.user_id,
                "name": "tf",
                "visibility": "public",
            },
            format="json",
        )
        self.assertEqual(team.status_code, 201, team.content)
        self.team_folder_id = team.json()["folderId"]
        tag = self.client.post(TAG, {"team_id": self.team_id, "name": "tg"}, format="json")
        self.assertEqual(tag.status_code, 201, tag.content)
        self.tag_id = tag.json()["tagId"]

    # name of the thing -> (url, ids body, how the answer names the thing, current name)
    def cases(self):
        return {
            "personal": (
                PERSONAL,
                {"team_id": self.team_id, "user_id": self.user_id, "folder_id": self.personal_id},
                "name",
                "pf",
            ),
            "team": (
                TEAM,
                {
                    "team_id": self.team_id,
                    "user_id": self.user_id,
                    "folder_id": self.team_folder_id,
                },
                "name",
                "tf",
            ),
            "tag": (TAG, {"team_id": self.team_id, "tag_id": self.tag_id}, "name", "tg"),
        }

    def put(self, url, body):
        return self.client.put(url, body, format="json")

    def test_a_rename_is_unchanged(self):
        for label, (url, ids, key, _) in self.cases().items():
            with self.subTest(view=label):
                resp = self.put(url, {**ids, "name": "  renamed  "})
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual(resp.json()[key], "renamed")

    def test_no_name_and_a_null_name_leave_the_name_alone(self):
        for label, (url, ids, key, current) in self.cases().items():
            for body in (ids, {**ids, "name": None}):
                with self.subTest(view=label, body=body):
                    resp = self.put(url, body)
                    self.assertEqual(resp.status_code, 200, resp.content)
                    self.assertEqual(resp.json()[key], current)

    def test_an_empty_or_blank_name_is_still_refused(self):
        for label, (url, ids, key, current) in self.cases().items():
            for name in ("", "   "):
                with self.subTest(view=label, name=name):
                    resp = self.put(url, {**ids, "name": name})
                    self.assertEqual(resp.status_code, 400, resp.content)
        resp = self.put(TEAM, {**self.cases()["team"][1], "name": "x" * 256})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "Folder name must be 1-255 characters."})

    def test_a_name_that_is_not_text_is_read_as_text_where_it_always_was(self):
        for label in ("team", "tag"):
            url, ids, key, _ = self.cases()[label]
            with self.subTest(view=label):
                resp = self.put(url, {**ids, "name": 5})
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual(resp.json()[key], "5")

    def test_an_empty_body_and_a_partial_one_keep_their_400s(self):
        expected = {
            "personal": [
                {},
                {"team_id": self.team_id},
                {"team_id": self.team_id, "user_id": self.user_id},
            ],
            "team": [
                {},
                {"team_id": self.team_id},
                {"team_id": self.team_id, "user_id": self.user_id},
            ],
            "tag": [{}, {"team_id": self.team_id}],
        }
        words = {
            "personal": ["team_id", "user_id", "folder_id"],
            "team": ["team_id", "user_id", "folder_id"],
            "tag": ["team_id", "tag_id"],
        }
        for label, (url, _, _, _) in self.cases().items():
            for body in expected[label]:
                with self.subTest(view=label, body=body):
                    resp = self.put(url, body)
                    missing = next(k for k in words[label] if body.get(k) is None)
                    self.assertEqual(resp.status_code, 400, resp.content)
                    self.assertEqual(resp.json(), {"error": f"{missing} is required."})

    def test_a_refused_rename_changes_nothing(self):
        url, ids, _, _ = self.cases()["personal"]
        self.put(url, {**ids, "name": ""})
        self.assertEqual(PersonalNoteFolder.objects.get(folder_id=self.personal_id).name, "pf")
        url, ids, _, _ = self.cases()["tag"]
        self.put(url, {**ids, "name": ""})
        self.assertEqual(NoteFolderTag.objects.get(tag_id=self.tag_id).name, "tg")


class ExternalShareOfferBodyTests(BaseAPITestCase):
    FIELDS = ("team_id", "guest_team_id", "object_type", "object_id")
    WORDS = "team_id, guest_team_id, object_type, object_id are required."

    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_an_empty_body_and_each_field_missing_or_empty_is_the_same_400(self):
        full = {
            "team_id": str(self.team.team_id),
            "guest_team_id": "g",
            "object_type": "note_folder",
            "object_id": "1",
        }
        bodies = [{}] + [{k: v for k, v in full.items() if k != f} for f in self.FIELDS]
        bodies += [{**full, f: None} for f in self.FIELDS] + [{**full, f: ""} for f in self.FIELDS]
        for body in bodies:
            with self.subTest(body=body):
                resp = self.client.post(SHARE, body, format="json")
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(resp.json(), {"error": self.WORDS})

    def test_a_complete_body_reaches_the_offer(self):
        body = {
            "team_id": str(self.team.team_id),
            "guest_team_id": str(self.team.team_id),
            "object_type": "not-a-type",
            "object_id": "1",
        }
        resp = self.client.post(SHARE, body, format="json")
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(resp.json(), {"error": "bad_object"})
