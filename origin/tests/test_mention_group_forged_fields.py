"""Mention groups take their creator and adder from the caller (MIP-1018).

`created_by` and `added_by` were stored as the body sent them, so attribution
could be forged, and a value that was no user id at all was a 500. The app
sends the caller's own id, which stays accepted; anybody else's is refused
with a 400 and nothing is written. Members must be on the team: a group
names colleagues, and an @group mention fans out to whoever is in it.
"""

from django.contrib.auth import get_user_model

from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

GROUPS = "/api/v2/mention-group/"
MEMBERS = "/api/v2/mention-group/members/"


class MentionGroupForgedFieldTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="mgfout", email="mgfout@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="MGF Other", team_email="mgfother@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)
        self.authenticate(self.user)

    def _create(self, name, **extra):
        body = {"team_id": str(self.team.team_id), "group_name": name}
        body.update(extra)
        return self.client.post(GROUPS, body, format="json")

    def _group(self, name="design"):
        return MentionGroupMaster.objects.create(
            team=self.team, group_name=name, created_by=self.user
        )

    # ── created_by ────────────────────────────────────────────────────

    def test_someone_elses_created_by_is_refused_and_writes_nothing(self):
        before = MentionGroupMaster.objects.count()
        forged = (str(self.user2.id), str(self.outsider.id), "nonsense", "", 5, [str(self.user.id)])
        for i, value in enumerate(forged):
            with self.subTest(created_by=value):
                res = self._create(f"forged{i}", created_by=value)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data, {"error": "created_by must be your own user id."})
        self.assertEqual(MentionGroupMaster.objects.count(), before)

    def test_the_apps_exact_body_still_works(self):
        """`useMentionGroups.createGroup`: team_id, group_name, description, created_by."""
        res = self.client.post(
            GROUPS,
            {
                "team_id": str(self.team.team_id),
                "group_name": "design-team",
                "description": "Designers",
                "created_by": str(self.user.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["createdBy"], str(self.user.id))
        self.assertEqual(res.data["description"], "Designers")

    def test_own_id_in_any_spelling_is_accepted(self):
        for i, value in enumerate((str(self.user.id).upper(), self.user.id.hex)):
            with self.subTest(created_by=value):
                res = self._create(f"mine{i}", created_by=value)
                self.assertEqual(res.status_code, 201, res.data)
                self.assertEqual(res.data["createdBy"], str(self.user.id))

    def test_an_absent_creator_is_the_caller(self):
        """Was NULL: a group nobody had made."""
        for i, extra in enumerate(({}, {"created_by": None})):
            with self.subTest(body=extra):
                res = self._create(f"absent{i}", **extra)
                self.assertEqual(res.status_code, 201, res.data)
                group = MentionGroupMaster.objects.get(group_id=res.data["groupId"])
                self.assertEqual(group.created_by_id, self.user.id)

    def test_the_team_check_still_comes_first(self):
        self.authenticate(self.outsider)
        res = self._create("planted", created_by=str(self.user.id))
        self.assertEqual(res.status_code, 404)

    def test_an_edit_cannot_change_the_creator_or_delete_the_group(self):
        group = self._group()
        res = self.client.put(
            GROUPS,
            {
                "group_id": group.group_id,
                "description": "edited",
                "created_by": str(self.user2.id),
                "is_deleted": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        group.refresh_from_db()
        self.assertEqual(group.created_by_id, self.user.id)
        self.assertFalse(group.is_deleted)
        self.assertEqual(group.description, "edited")

    # ── added_by and who may be added ─────────────────────────────────

    def test_someone_elses_added_by_is_refused_and_writes_nothing(self):
        group = self._group()
        for i, value in enumerate((str(self.user2.id), str(self.outsider.id), "nonsense", "")):
            with self.subTest(added_by=value):
                res = self.client.post(
                    MEMBERS,
                    {
                        "group_id": group.group_id,
                        "user_ids": [str(self.user2.id)],
                        "added_by": value,
                    },
                    format="json",
                )
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data, {"error": "added_by must be your own user id."})
        self.assertFalse(MentionGroupMembers.objects.filter(group=group).exists())

    def test_the_apps_exact_add_body_still_works(self):
        """`useMentionGroups.addMembers`: group_id, user_ids, added_by (own id)."""
        group = self._group()
        res = self.client.post(
            MEMBERS,
            {
                "group_id": group.group_id,
                "user_ids": [str(self.user.id), str(self.user2.id)],
                "added_by": str(self.user.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["memberCount"], 2)
        rows = MentionGroupMembers.objects.filter(group=group)
        self.assertEqual({str(r.added_by_id) for r in rows}, {str(self.user.id)})

    def test_an_absent_adder_is_the_caller(self):
        group = self._group()
        res = self.client.post(
            MEMBERS, {"group_id": group.group_id, "user_ids": [str(self.user2.id)]}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)
        row = MentionGroupMembers.objects.get(group=group, user=self.user2)
        self.assertEqual(row.added_by_id, self.user.id)

    def test_a_user_outside_the_team_cannot_be_added_and_nothing_is_added(self):
        group = self._group()
        bad_lists = (
            [str(self.outsider.id)],
            [str(self.user2.id), str(self.outsider.id)],  # one good, one not: none added
            [str(self.user2.id), "nonsense"],
            "nonsense",
            {"id": str(self.user2.id)},
        )
        for user_ids in bad_lists:
            with self.subTest(user_ids=user_ids):
                res = self.client.post(
                    MEMBERS,
                    {
                        "group_id": group.group_id,
                        "user_ids": user_ids,
                        "added_by": str(self.user.id),
                    },
                    format="json",
                )
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data, {"error": "user_ids must all be members of this team."})
        self.assertFalse(MentionGroupMembers.objects.filter(group=group).exists())
