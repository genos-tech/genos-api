"""A deleted mention group no longer keeps its name taken (MIP-1733).

`MentionGroupView.delete` soft-deletes (`is_deleted = True`) and the unique constraint on
`(team, group_name)` used to ignore that flag, so the name of a deleted group could not be used
again: the create view's own check looks at live groups only, passed, and the insert then hit the
constraint (an unhandled `IntegrityError`, a 500). The sample data's group `sample-dept` is
deleted this way by the "remove the sample data" action, so it was user-visible.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.tests import test_sample_data
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

GROUPS = "/api/v2/mention-group/"


class _Api:
    def create(self, name, team=None, **extra):
        return self.client.post(
            GROUPS,
            {"team_id": str((team or self.team).team_id), "group_name": name, **extra},
            format="json",
        )

    def delete(self, group_id):
        return self.client.delete(GROUPS, {"group_id": group_id}, format="json")


class NameReuseTests(_Api, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_a_deleted_groups_name_can_be_used_again(self):
        first = self.create("design")
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(self.delete(first.data["groupId"]).status_code, 200)

        again = self.create("design")

        self.assertEqual(again.status_code, 201, getattr(again, "data", None))
        self.assertEqual(again.data["groupName"], "design")

    def test_the_new_group_is_a_new_one_with_nobody_from_the_old_one(self):
        first = self.create("design").data
        MentionGroupMembers.objects.create(
            team=self.team, group_id=first["groupId"], user=self.user2, added_by=self.user
        )
        self.delete(first["groupId"])

        again = self.create("design").data

        self.assertNotEqual(again["groupId"], first["groupId"])
        self.assertEqual(again["memberUserIds"], [])
        listed = self.client.get(GROUPS, {"team_id": str(self.team.team_id)}).data["mentionGroups"]
        self.assertEqual([g["groupId"] for g in listed], [again["groupId"]])
        self.assertEqual(listed[0]["memberCount"], 0)
        # The old one stays deleted, with its rows, so a message that names it keeps rendering.
        old = MentionGroupMaster.objects.get(group_id=first["groupId"])
        self.assertTrue(old.is_deleted)
        self.assertEqual(old.members.count(), 1)

    def test_the_name_can_be_reused_more_than_once(self):
        for _ in range(3):
            made = self.create("design")
            self.assertEqual(made.status_code, 201)
            self.assertEqual(self.delete(made.data["groupId"]).status_code, 200)
        self.assertEqual(MentionGroupMaster.objects.filter(group_name="design").count(), 3)
        self.assertEqual(self.create("design").status_code, 201)

    def test_a_group_can_be_renamed_to_the_name_of_a_deleted_one(self):
        gone = self.create("design").data["groupId"]
        self.delete(gone)
        other = self.create("ops").data["groupId"]

        res = self.client.put(GROUPS, {"group_id": other, "group_name": "design"}, format="json")

        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        self.assertEqual(res.data["groupName"], "design")

    def test_a_live_groups_name_is_still_unique(self):
        self.assertEqual(self.create("design").status_code, 201)

        again = self.create("design")

        self.assertEqual(again.status_code, 409)
        self.assertEqual(MentionGroupMaster.objects.filter(group_name="design").count(), 1)

    def test_a_rename_onto_a_live_name_is_still_refused(self):
        self.create("design")
        other = self.create("ops").data["groupId"]

        res = self.client.put(GROUPS, {"group_id": other, "group_name": "design"}, format="json")

        self.assertEqual(res.status_code, 409)

    def test_the_database_itself_refuses_a_second_live_group_of_the_name(self):
        from django.db import IntegrityError, transaction

        MentionGroupMaster.objects.create(team=self.team, group_name="design")
        with self.assertRaises(IntegrityError), transaction.atomic():
            MentionGroupMaster.objects.create(team=self.team, group_name="design")

    def test_two_teams_may_share_a_name(self):
        other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user)

        self.assertEqual(self.create("design").status_code, 201)
        self.assertEqual(self.create("design", team=other_team).status_code, 201)

    def test_reviving_nothing_the_old_row_is_left_alone(self):
        first = self.create("design", description="old words").data
        self.delete(first["groupId"])

        self.create("design", description="new words")

        old = MentionGroupMaster.objects.get(group_id=first["groupId"])
        self.assertEqual((old.is_deleted, old.description), (True, "old words"))


class AfterTheSampleDataGoesTests(_Api, test_sample_data.SampleDataTestCase):
    def test_the_sample_group_name_is_free_again(self):
        team = self.create_team()
        sample = MentionGroupMaster.objects.get(team=team, group_name="sample-dept")
        self.assertFalse(sample.is_deleted)

        resp = self.execute(team)
        self.assertEqual(resp.status_code, 200, resp.data)
        sample.refresh_from_db()
        self.assertTrue(sample.is_deleted)

        again = self.create("sample-dept", team=team)

        self.assertEqual(again.status_code, 201, getattr(again, "data", None))
        self.assertNotEqual(again.data["groupId"], sample.group_id)
