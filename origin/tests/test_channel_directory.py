"""Group-chat directory (MIP-1694): browse the team's public group chats.

The directory is a team-wide list, so the failure that matters is a leak: a private,
external, deleted or archived chat must never appear, and a person outside the team
must learn nothing. The description is a new field the owner or an editor writes.
"""

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

DIRECTORY = "/api/v3/channels/directory/"


class ChannelDirectoryTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.user2)

    def _chat(self, title, members=(), **fields):
        channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title=title, owner=self.user, **fields
        )
        for user in members:
            ChannelMember.objects.create(channel=channel, user=user)
        return channel

    def _get(self, **params):
        res = self.client.get(DIRECTORY, {"team_id": str(self.team.team_id), **params})
        self.assertEqual(res.status_code, 200, res.content[:300])
        return res.json()

    def _titles(self, **params):
        return [r["title"] for r in self._get(**params)["results"]]

    def test_lists_public_group_chats_with_their_facts(self):
        self._chat("Design", members=[self.user, self.user2], description="Pixels and taste")

        row = self._get()["results"][0]

        self.assertEqual(row["title"], "Design")
        self.assertEqual(row["description"], "Pixels and taste")
        self.assertEqual(row["memberCount"], 2)
        self.assertTrue(row["isJoined"])

    def test_joined_state_and_member_count_ignore_left_members(self):
        chat = self._chat("Ops", members=[self.user])
        ChannelMember.objects.create(channel=chat, user=self.user2, is_deleted=True)

        row = self._get()["results"][0]

        self.assertFalse(row["isJoined"])
        self.assertEqual(row["memberCount"], 1)

    def test_never_lists_private_external_deleted_or_archived_chats(self):
        self._chat("open")
        self._chat("private", is_private=True)
        self._chat("external", is_private=True, is_external=True)
        self._chat("gone", is_deleted=True)
        self._chat("archived", archived_at=timezone.now())
        Channel.objects.create(team=self.team, kind=ChannelKind.MDM, title="mdm")

        self.assertEqual(self._titles(), ["open"])

    def test_a_private_chat_the_caller_is_in_is_still_not_listed(self):
        self._chat("secret", members=[self.user2], is_private=True)

        self.assertEqual(self._titles(), [])

    def test_other_teams_chats_are_not_listed(self):
        other = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="dir-else@example.com", owner=self.user
        )
        Channel.objects.create(team=other, kind=ChannelKind.GM, title="elsewhere")

        self.assertEqual(self._titles(), [])

    def test_a_non_member_of_the_team_gets_404(self):
        stranger = User.objects.create_user(
            username="dirstranger", email="dir-stranger@example.com", password="x12345678"
        )
        self.authenticate(stranger)

        res = self.client.get(DIRECTORY, {"team_id": str(self.team.team_id)})

        self.assertEqual(res.status_code, 404)

    def test_team_id_is_required(self):
        res = self.client.get(DIRECTORY)

        self.assertEqual(res.status_code, 400)

    def test_search_matches_title_and_description(self):
        self._chat("Marketing", description="Campaigns")
        self._chat("Random", description="Marketing memes")
        self._chat("Ops")

        self.assertEqual(self._titles(q="market"), ["Marketing", "Random"])

    def test_search_treats_wildcards_literally(self):
        self._chat("100% done")
        self._chat("other")

        self.assertEqual(self._titles(q="%"), ["100% done"])

    def test_pages_cover_every_chat_once_in_name_order(self):
        for i in range(7):
            self._chat(f"c{i}")

        first = self._get(limit=3)
        second = self._get(limit=3, offset=3)
        third = self._get(limit=3, offset=6)

        self.assertTrue(first["hasMore"])
        self.assertTrue(second["hasMore"])
        self.assertFalse(third["hasMore"])
        self.assertEqual(first["total"], 7)
        names = [r["title"] for p in (first, second, third) for r in p["results"]]
        self.assertEqual(names, [f"c{i}" for i in range(7)])

    def test_the_page_size_is_capped(self):
        for i in range(3):
            self._chat(f"c{i}")

        self.assertEqual(len(self._get(limit=100000)["results"]), 3)

    def test_the_query_count_does_not_grow_with_the_chats(self):
        self._chat("a", members=[self.user])
        with self.assertNumQueries(self._count_queries()):
            self._get()
        for i in range(15):
            self._chat(f"more{i}", members=[self.user, self.user2])
        with self.assertNumQueries(self._count_queries()):
            self._get()

    def _count_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self._get()
        return len(ctx)


class ChannelDescriptionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.chat = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Talk", owner=self.user
        )
        ChannelMember.objects.create(channel=self.chat, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.chat, user=self.user2, role="member")
        self.url = f"/api/v3/channels/{self.chat.id}/"

    def test_the_owner_sets_the_description_and_it_is_returned(self):
        self.authenticate(self.user)

        res = self.client.patch(self.url, {"description": "  Weekly talk  "}, format="json")

        self.assertEqual(res.status_code, 200, res.content[:300])
        self.chat.refresh_from_db()
        self.assertEqual(self.chat.description, "Weekly talk")
        self.assertEqual(self.client.get(self.url).json()["channel"]["description"], "Weekly talk")

    def test_a_plain_member_cannot_set_it(self):
        self.authenticate(self.user2)

        res = self.client.patch(self.url, {"description": "mine"}, format="json")

        self.assertEqual(res.status_code, 403)
        self.chat.refresh_from_db()
        self.assertEqual(self.chat.description, "")

    def test_too_long_or_not_a_string_is_refused(self):
        self.authenticate(self.user)

        self.assertEqual(
            self.client.patch(self.url, {"description": "x" * 281}, format="json").status_code,
            400,
        )
        self.assertEqual(
            self.client.patch(self.url, {"description": 5}, format="json").status_code, 400
        )

    def test_an_empty_description_clears_it(self):
        self.chat.description = "old"
        self.chat.save()
        self.authenticate(self.user)

        res = self.client.patch(self.url, {"description": ""}, format="json")

        self.assertEqual(res.status_code, 200)
        self.chat.refresh_from_db()
        self.assertEqual(self.chat.description, "")


class ChannelJoinClosedTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.user2)

    def _join(self, **fields):
        chat = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="x", owner=self.user, **fields
        )
        return self.client.post(f"/api/v3/channels/{chat.id}/join/")

    def test_an_archived_chat_cannot_be_self_joined(self):
        self.assertEqual(self._join(archived_at=timezone.now()).status_code, 403)

    def test_an_external_chat_cannot_be_self_joined(self):
        self.assertEqual(self._join(is_private=True, is_external=True).status_code, 403)

    def test_a_public_chat_can_still_be_joined(self):
        self.assertEqual(self._join().status_code, 200)
