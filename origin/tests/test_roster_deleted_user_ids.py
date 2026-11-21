"""MIP-1820: the roster tells the client which accounts of the team's history are DELETED.

An @mention keeps the real name typed in the text (`props.userName`). The client may show
"Deleted user" for it only when it can PROVE the account is deleted, and until now it could not:
the roster's `isDeleted` is `membership.is_deleted OR attendee.is_deleted`, so leaving the team and
deleting the account send the same flag, and a full load omits both. A client without a checkpoint
(a new device, a cleared cache) never learned anything.

`GET /api/v2/team/getTeamMembers/` now carries `data.deletedUserIds`: the ids of ANONYMISED
accounts that were members of THIS team (the same ids the team's messages already carry as
senders and mention `userId`s). Rules pinned here:

* a member who merely LEFT stays a normal former member and is never in the list;
* an account that left and was deleted afterwards IS in it (it is still this team's history);
* an account deleted elsewhere (another team only) is never in it: no global list;
* it is on a full load AND an incremental one (a checkpoint older than the deletion must not hide
  it), on the LAST page of a paged load only, and it is empty for a guest or outside caller;
* additive: every pre-existing key of the response, and every row, is unchanged.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.account_deletion import delete_account
from origin.services.member_roles import GUEST
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

URL = "/api/v2/team/getTeamMembers/"


class DeletedUserIdsTests(BaseAPITestCase):
    """`self.user` owns the team and reads the roster; `user2` stays."""

    def setUp(self):
        super().setUp()
        self.leaver = User.objects.create_user(
            username="Lee Leaver", email="lee@example.com", password="x"
        )
        self.doomed = User.objects.create_user(
            username="Dora Doomed", email="dora@example.com", password="x"
        )
        self.left_then_deleted = User.objects.create_user(
            username="Lena Later", email="lena@example.com", password="x"
        )
        for u in (self.leaver, self.doomed, self.left_then_deleted):
            TeamMembers.objects.create(team=self.team, attendee=u)
        # Lee simply leaves the team: the membership is soft-deleted, the account lives on.
        TeamMembers.objects.filter(team=self.team, attendee=self.leaver).update(
            is_deleted=True, ts_updated_at=timezone.now()
        )
        # Lena leaves first and deletes her account afterwards.
        TeamMembers.objects.filter(team=self.team, attendee=self.left_then_deleted).update(
            is_deleted=True, ts_updated_at=timezone.now()
        )
        delete_account(self.left_then_deleted)
        delete_account(self.doomed)
        self.authenticate(self.user)

    def _get(self, user=None, team=None, **params):
        if user is not None:
            self.authenticate(user)
        team = team or self.team
        res = self.client.get(
            URL,
            {"team_id": str(team.team_id), "team_name": team.team_name, **params},
        )
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        return res.json()

    def test_a_full_load_lists_the_deleted_accounts_of_the_team(self):
        body = self._get()
        self.assertEqual(
            sorted(body["data"]["deletedUserIds"]),
            sorted([str(self.doomed.id), str(self.left_then_deleted.id)]),
        )

    def test_a_member_who_left_is_never_reported_deleted(self):
        """The red test of the task: leaving the team is not deleting the account."""
        ids = self._get()["data"]["deletedUserIds"]
        self.assertNotIn(str(self.leaver.id), ids)
        # Not a stale answer either: the incremental tombstone for Lee still reads `isDeleted`
        # (existing behaviour, a membership tombstone), and still is NOT a deleted account.
        since = (timezone.now() - timedelta(days=1)).isoformat()
        body = self._get(since=since)
        self.assertNotIn(str(self.leaver.id), body["data"]["deletedUserIds"])
        row = next(r for r in body["data"]["members"] if r["userId"] == str(self.leaver.id))
        self.assertTrue(row["isDeleted"])

    def test_a_live_member_is_not_listed(self):
        ids = self._get()["data"]["deletedUserIds"]
        for live in (self.user, self.user2):
            self.assertNotIn(str(live.id), ids)

    def test_nothing_of_another_team_is_listed(self):
        """No global list: a person deleted who was only ever in ANOTHER team stays out."""
        other_owner = User.objects.create_user(
            username="Other Owner", email="oo@example.com", password="x"
        )
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=other_owner
        )
        TeamMembers.objects.create(team=other_team, attendee=other_owner)
        elsewhere = User.objects.create_user(
            username="Eve Elsewhere", email="eve@example.com", password="x"
        )
        TeamMembers.objects.create(team=other_team, attendee=elsewhere)
        delete_account(elsewhere)

        mine = self._get()["data"]["deletedUserIds"]
        self.assertNotIn(str(elsewhere.id), mine)
        theirs = self._get(user=other_owner, team=other_team)["data"]["deletedUserIds"]
        self.assertEqual(theirs, [str(elsewhere.id)])

    def test_an_incremental_load_with_an_old_checkpoint_still_lists_them(self):
        """A client whose checkpoint predates the deletion (or the deploy) must learn it."""
        since = (timezone.now() + timedelta(days=1)).isoformat()  # after every change
        body = self._get(since=since)
        self.assertEqual(
            sorted(body["data"]["deletedUserIds"]),
            sorted([str(self.doomed.id), str(self.left_then_deleted.id)]),
        )

    def test_a_team_with_no_deleted_account_gets_an_empty_list(self):
        owner = User.objects.create_user(username="Solo", email="solo@example.com", password="x")
        team = TeamMaster.objects.create(team_name="Solo", team_email="s@example.com", owner=owner)
        TeamMembers.objects.create(team=team, attendee=owner)
        self.assertEqual(self._get(user=owner, team=team)["data"]["deletedUserIds"], [])

    def test_a_paged_load_lists_them_on_the_last_page_only(self):
        first = self._get(limit=1)
        self.assertTrue(first["has_more"])
        self.assertNotIn("deletedUserIds", first["data"])
        pages = [first]
        while pages[-1]["has_more"]:
            pages.append(self._get(limit=1, cursor=pages[-1]["next_cursor"]))
        listed = [p["data"]["deletedUserIds"] for p in pages if "deletedUserIds" in p["data"]]
        self.assertEqual(len(listed), 1)
        self.assertIn("deletedUserIds", pages[-1]["data"])
        self.assertEqual(
            sorted(listed[0]),
            sorted([str(self.doomed.id), str(self.left_then_deleted.id)]),
        )

    def test_a_guest_never_receives_the_list(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="Client", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        guest = User.objects.create_user(username="Gus", email="gus@agency.example", password="x")
        ProjectMembers.objects.create(
            team=self.team, project=project, attendee=guest, member_role=GUEST
        )
        self.assertEqual(self._get(user=guest)["data"]["deletedUserIds"], [])

    def test_a_stranger_gets_not_found_and_no_ids(self):
        """Not a member, not a guest, not an external collaborator: the team does not exist."""
        stranger = User.objects.create_user(
            username="Stan Stranger", email="stan@example.com", password="x"
        )
        self.authenticate(stranger)
        res = self.client.get(URL, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 404)
        self.assertNotIn(str(self.doomed.id), res.content.decode())

    def test_additive_only_the_existing_keys_and_rows_are_unchanged(self):
        """Old clients ignore the new key: nothing that was there moved or changed."""
        body = self._get()
        self.assertEqual(set(body), {"server_time", "data"})
        self.assertEqual(list(body["data"]), ["members", "deletedUserIds"])
        # The rows are exactly the live members (full load: no tombstone rows), with the keys
        # they always had.
        members = body["data"]["members"]
        self.assertEqual(
            sorted(m["userName"] for m in members),
            sorted([self.user.username, self.user2.username]),
        )
        self.assertEqual(
            sorted(members[0]),
            sorted(
                [
                    "teamId",
                    "teamName",
                    "userId",
                    "userName",
                    "userEmail",
                    "avatarImgPath",
                    "isOfflineForced",
                    "role",
                    "memberRole",
                    "baseCountry",
                    "phoneNumber",
                    "currentLocation",
                    "timezone",
                    "aboutMe",
                    "customStatus",
                    "customStatusExpiry",
                    "tsLastSeen",
                    "tsJoined",
                    "isDeleted",
                ]
            ),
        )
