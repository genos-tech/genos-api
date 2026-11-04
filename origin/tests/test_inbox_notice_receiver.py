"""`POST inbox/` addresses a notice to someone who is meant to get one (MIP-1029).

`receiver_id` was taken as it came. A UUID of a user in no team of the caller's got a notice
(and a web push) with a body of the caller's choosing; `null` filed an item addressed to no
one; text, a list or an object was a ValidationError (500) at the duplicate check.

A notice goes to the caller, to a member or guest of the team, or to the person who asked the
caller for something in this team: the sockets service answers a rejected join-team request
with a notice to the requester, who is not a member and never was.
"""

import uuid

from rest_framework import status

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.tests.test_base import BaseAPITestCase, User

INBOX = "/api/v2/inbox/"


class NoticeReceiverTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        self.authenticate(self.user)

    def body(self, **over):
        body = {
            "team_id": str(self.team.team_id),
            "item_type": 0,
            "receiver_id": str(self.user2.id),
            "item_body": [{"type": "paragraph", "content": []}],
        }
        body.update(over)
        return body

    def post(self, **over):
        return self.client.post(INBOX, self.body(**over), format="json")

    def assertRefused(self, res):
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.content)
        self.assertEqual(res.json(), {"error": "receiver_id must be a member of the team."})
        self.assertFalse(InboxItems.objects.exists(), "a refused notice wrote a row")

    def test_a_team_member_gets_the_notice_as_before(self):
        res = self.post()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(res.json()["receiver"], str(self.user2.id))
        self.assertFalse(res.json()["alreadyExist"])
        item = InboxItems.objects.get()
        self.assertEqual(
            (item.sender_id, item.receiver_id, item.item_type, item.team_id),
            (self.user.id, self.user2.id, 0, self.team.team_id),
        )

    def test_the_same_notice_twice_is_still_one(self):
        self.post()
        res = self.post()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertTrue(res.json()["alreadyExist"])
        self.assertEqual(InboxItems.objects.count(), 1)

    def test_the_caller_may_notify_themselves(self):
        res = self.post(receiver_id=str(self.user.id))
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(InboxItems.objects.get().receiver_id, self.user.id)

    def test_any_spelling_of_a_members_id_is_that_member(self):
        res = self.post(receiver_id=str(self.user2.id).upper())
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(InboxItems.objects.get().receiver_id, self.user2.id)

    def test_a_guest_of_the_team_gets_it(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.stranger)
        res = self.post(receiver_id=str(self.stranger.id))
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)

    def test_a_user_in_no_team_of_the_callers_gets_nothing(self):
        self.assertRefused(self.post(receiver_id=str(self.stranger.id)))

    def test_a_member_of_another_team_only_gets_nothing(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=other, attendee=self.stranger)
        self.assertRefused(self.post(receiver_id=str(self.stranger.id)))

    def test_a_uuid_that_names_nobody_gets_nothing(self):
        self.assertRefused(self.post(receiver_id=str(uuid.uuid4())))

    def test_a_receiver_that_is_not_an_id_is_the_same_400_and_writes_nothing(self):
        for value in ("abc", "", [str(self.user2.id)], {"id": 1}, 5, True, None):
            with self.subTest(receiver_id=value):
                self.assertRefused(self.post(receiver_id=value))

    def test_a_missing_receiver_keeps_its_own_400(self):
        body = self.body()
        del body["receiver_id"]
        res = self.client.post(INBOX, body, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.json(), {"error": "receiver_id is required."})

    def test_the_requester_of_a_join_request_is_told_it_was_rejected(self):
        # The sockets reject handler: the team's owner answers a stranger's join-team request.
        InboxItems.objects.create(
            team=self.team, sender=self.stranger, receiver=self.user, item_type=1
        )
        res = self.post(receiver_id=str(self.stranger.id))
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(InboxItems.objects.filter(item_type=0).get().receiver_id, self.stranger.id)

    def test_a_request_to_someone_else_does_not_make_you_a_receiver(self):
        InboxItems.objects.create(
            team=self.team, sender=self.stranger, receiver=self.user2, item_type=1
        )
        res = self.post(receiver_id=str(self.stranger.id))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.content)
        self.assertFalse(InboxItems.objects.filter(item_type=0).exists())

    def test_a_request_in_another_team_does_not_make_you_a_receiver(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user
        )
        InboxItems.objects.create(team=other, sender=self.stranger, receiver=self.user, item_type=1)
        res = self.post(receiver_id=str(self.stranger.id))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.content)

    def test_a_plain_notice_from_them_is_not_a_request(self):
        InboxItems.objects.create(
            team=self.team, sender=self.stranger, receiver=self.user, item_type=0
        )
        res = self.post(receiver_id=str(self.stranger.id))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.content)

    def test_a_team_id_that_is_not_the_callers_team_is_still_the_404(self):
        for value in ("abc", [1], {"a": 1}, str(uuid.uuid4())):
            with self.subTest(team_id=value):
                res = self.post(team_id=value)
                self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND, res.content)
        self.assertFalse(InboxItems.objects.exists())
