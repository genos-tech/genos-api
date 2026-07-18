"""Tests for the personal-tag endpoints (`/api/v3/personal-tags/` +
`PUT /api/v3/channels/{id}/personal-tags/`).

Personal tags are PRIVATE per-user labels on GM channels. The suites
below cover: tag CRUD (incl. the case-insensitive duplicate guard and
the per-user cap), replace-set channel assignment (membership gate,
GM-only gate, ownership of tag ids), and the bundle GET (cross-user
isolation, active-membership filtering, pinned-vs-recency default-chip
ranking).

Message rows use `auto_now_add` on `ts_sent_at`, so recency tests set
timestamps via a post-create `.update()` — the same trick other
ts-ordering tests use.
"""

from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from origin.models.chat.personal_tag_models import (
    PersonalChannelTag,
    PersonalChannelTagAssignment,
)
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.tests.test_base import BaseAPITestCase
from origin.views.chat.personal_tag_views import (
    DEFAULT_VISIBLE_CAP,
    MAX_TAGS_PER_CHANNEL,
    MAX_TAGS_PER_USER,
)

LIST_URL = reverse("v3_personal_tag_list")


def _detail_url(tag_id):
    return reverse("v3_personal_tag_detail", args=[tag_id])


def _channel_tags_url(channel_id):
    return reverse("v3_channel_personal_tags", args=[channel_id])


class PersonalTagTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # GM with both users as active members.
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Tagged GM", owner=self.user
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.gm, user=self.user2, role="member")

    def _make_tag(self, user=None, name="Client A", **overrides):
        return PersonalChannelTag.objects.create(
            user=user or self.user,
            name=name,
            color=overrides.pop("color", "#ff2323"),
            text_color=overrides.pop("text_color", "white"),
            **overrides,
        )

    def _make_gm(self, title, member=True):
        gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title=title, owner=self.user
        )
        if member:
            ChannelMember.objects.create(channel=gm, user=self.user, role="owner")
        return gm

    def _send(self, channel, user, seq, ts):
        msg = Message.objects.create(channel=channel, sender=user, seq=seq, body={"text": "hi"})
        Message.objects.filter(id=msg.id).update(ts_sent_at=ts)
        return msg


class PersonalTagCrudTests(PersonalTagTestBase):
    def test_create_tag(self):
        self.authenticate()
        resp = self.client.post(
            LIST_URL,
            {"name": " Client A ", "color": "#ff2323", "textColor": "white"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["name"], "Client A")  # stripped
        self.assertEqual(resp.data["color"], "#ff2323")
        self.assertEqual(resp.data["textColor"], "white")
        self.assertFalse(resp.data["isDefaultVisible"])

    def test_create_requires_auth(self):
        resp = self.client.post(LIST_URL, {"name": "x"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_rejects_empty_and_long_names(self):
        self.authenticate()
        for bad in ["", "   ", "x" * 31]:
            resp = self.client.post(
                LIST_URL, {"name": bad, "color": "#fff", "textColor": "black"}, format="json"
            )
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, bad)

    def test_create_rejects_case_insensitive_duplicate(self):
        self._make_tag(name="Client")
        self.authenticate()
        resp = self.client.post(
            LIST_URL, {"name": "client", "color": "#fff", "textColor": "black"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_name_allowed_across_users(self):
        self._make_tag(user=self.user2, name="Client")
        self.authenticate()
        resp = self.client.post(
            LIST_URL, {"name": "Client", "color": "#fff", "textColor": "black"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_create_enforces_per_user_cap(self):
        for i in range(MAX_TAGS_PER_USER):
            self._make_tag(name=f"t{i}")
        self.authenticate()
        resp = self.client.post(
            LIST_URL, {"name": "one more", "color": "#fff", "textColor": "black"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_rename_recolor_pin_sort(self):
        tag = self._make_tag()
        self.authenticate()
        resp = self.client.patch(
            _detail_url(tag.tag_id),
            {"name": "Renamed", "color": "#00ff00", "isDefaultVisible": True, "sortOrder": 3},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        tag.refresh_from_db()
        self.assertEqual(tag.name, "Renamed")
        self.assertEqual(tag.color, "#00ff00")
        self.assertTrue(tag.is_default_visible)
        self.assertEqual(tag.sort_order, 3)

    def test_patch_rename_to_own_name_is_allowed(self):
        # The iexact dup check must exclude the tag being renamed.
        tag = self._make_tag(name="Keep")
        self.authenticate()
        resp = self.client.patch(_detail_url(tag.tag_id), {"name": "keep"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_patch_foreign_tag_404(self):
        tag = self._make_tag(user=self.user2)
        self.authenticate()
        resp = self.client.patch(_detail_url(tag.tag_id), {"name": "hijack"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_cascades_assignments(self):
        tag = self._make_tag()
        PersonalChannelTagAssignment.objects.create(tag=tag, channel=self.gm)
        self.authenticate()
        resp = self.client.delete(_detail_url(tag.tag_id))
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(PersonalChannelTagAssignment.objects.filter(tag_id=tag.tag_id).exists())

    def test_delete_foreign_tag_404(self):
        tag = self._make_tag(user=self.user2)
        self.authenticate()
        resp = self.client.delete(_detail_url(tag.tag_id))
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(PersonalChannelTag.objects.filter(tag_id=tag.tag_id).exists())


class ChannelPersonalTagsPutTests(PersonalTagTestBase):
    def test_replace_set_add_and_remove(self):
        t1, t2, t3 = (self._make_tag(name=n) for n in ["a", "b", "c"])
        PersonalChannelTagAssignment.objects.create(tag=t1, channel=self.gm)
        self.authenticate()
        resp = self.client.put(
            _channel_tags_url(self.gm.id),
            {"tagIds": [t2.tag_id, t3.tag_id]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["tagIds"], [t2.tag_id, t3.tag_id])
        current = set(
            PersonalChannelTagAssignment.objects.filter(
                tag__user=self.user, channel=self.gm
            ).values_list("tag_id", flat=True)
        )
        self.assertEqual(current, {t2.tag_id, t3.tag_id})

    def test_put_is_idempotent(self):
        tag = self._make_tag()
        self.authenticate()
        for _ in range(2):
            resp = self.client.put(
                _channel_tags_url(self.gm.id), {"tagIds": [tag.tag_id]}, format="json"
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            PersonalChannelTagAssignment.objects.filter(tag=tag, channel=self.gm).count(), 1
        )

    def test_put_does_not_touch_other_users_assignments(self):
        # user2 has their own tag on the same GM; user1's replace-set
        # must leave it alone.
        theirs = self._make_tag(user=self.user2, name="theirs")
        PersonalChannelTagAssignment.objects.create(tag=theirs, channel=self.gm)
        mine = self._make_tag(name="mine")
        self.authenticate()
        resp = self.client.put(
            _channel_tags_url(self.gm.id), {"tagIds": [mine.tag_id]}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(
            PersonalChannelTagAssignment.objects.filter(tag=theirs, channel=self.gm).exists()
        )

    def test_non_member_channel_404(self):
        outside = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="not mine", owner=self.user2
        )
        ChannelMember.objects.create(channel=outside, user=self.user2, role="owner")
        tag = self._make_tag()
        self.authenticate()
        resp = self.client.put(
            _channel_tags_url(outside.id), {"tagIds": [tag.tag_id]}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_soft_deleted_membership_404(self):
        ChannelMember.objects.filter(channel=self.gm, user=self.user).update(is_deleted=True)
        tag = self._make_tag()
        self.authenticate()
        resp = self.client.put(
            _channel_tags_url(self.gm.id), {"tagIds": [tag.tag_id]}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_non_gm_channel_400(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        ChannelMember.objects.create(channel=dm, user=self.user, role="member")
        tag = self._make_tag()
        self.authenticate()
        resp = self.client.put(_channel_tags_url(dm.id), {"tagIds": [tag.tag_id]}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_foreign_tag_id_400(self):
        theirs = self._make_tag(user=self.user2)
        self.authenticate()
        resp = self.client.put(
            _channel_tags_url(self.gm.id), {"tagIds": [theirs.tag_id]}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_malformed_tag_ids_400(self):
        self.authenticate()
        for bad in [None, "x", [1, "two"], {"a": 1}]:
            resp = self.client.put(_channel_tags_url(self.gm.id), {"tagIds": bad}, format="json")
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, bad)

    def test_per_channel_cap_400(self):
        tags = [self._make_tag(name=f"t{i}") for i in range(MAX_TAGS_PER_CHANNEL + 1)]
        self.authenticate()
        resp = self.client.put(
            _channel_tags_url(self.gm.id),
            {"tagIds": [t.tag_id for t in tags]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class PersonalTagBundleGetTests(PersonalTagTestBase):
    def test_bundle_isolated_per_user(self):
        mine = self._make_tag(name="mine")
        PersonalChannelTagAssignment.objects.create(tag=mine, channel=self.gm)
        theirs = self._make_tag(user=self.user2, name="theirs")
        PersonalChannelTagAssignment.objects.create(tag=theirs, channel=self.gm)

        self.authenticate(self.user2)
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([t["name"] for t in resp.data["tags"]], ["theirs"])
        self.assertEqual(resp.data["assignments"], {str(self.gm.id): [theirs.tag_id]})

    def test_assignments_hide_left_and_deleted_channels(self):
        tag = self._make_tag()
        left_gm = self._make_gm("left")
        deleted_gm = self._make_gm("deleted")
        for ch in (self.gm, left_gm, deleted_gm):
            PersonalChannelTagAssignment.objects.create(tag=tag, channel=ch)
        ChannelMember.objects.filter(channel=left_gm, user=self.user).update(is_deleted=True)
        Channel.objects.filter(id=deleted_gm.id).update(is_deleted=True)

        self.authenticate()
        resp = self.client.get(LIST_URL)
        self.assertEqual(list(resp.data["assignments"].keys()), [str(self.gm.id)])
        # Rows are kept in the DB (rejoin resurrects them).
        self.assertEqual(PersonalChannelTagAssignment.objects.filter(tag=tag).count(), 3)

    def test_default_visible_prefers_pinned(self):
        pinned = self._make_tag(name="pinned", is_default_visible=True, sort_order=1)
        pinned2 = self._make_tag(name="also pinned", is_default_visible=True, sort_order=0)
        recent = self._make_tag(name="recent")
        PersonalChannelTagAssignment.objects.create(tag=recent, channel=self.gm)
        self._send(self.gm, self.user, seq=1, ts=timezone.now())

        self.authenticate()
        resp = self.client.get(LIST_URL)
        # Pinned wins over recency, ordered by (sort_order, name).
        self.assertEqual(resp.data["defaultVisibleTagIds"], [pinned2.tag_id, pinned.tag_id])

    def test_default_visible_recency_fallback_ordering(self):
        now = timezone.now()
        gm_old = self._make_gm("older activity")
        tag_recent = self._make_tag(name="recent tag")
        tag_older = self._make_tag(name="older tag")
        PersonalChannelTagAssignment.objects.create(tag=tag_recent, channel=self.gm)
        PersonalChannelTagAssignment.objects.create(tag=tag_older, channel=gm_old)
        self._send(gm_old, self.user, seq=1, ts=now - timedelta(days=2))
        self._send(self.gm, self.user, seq=1, ts=now - timedelta(hours=1))

        self.authenticate()
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.data["defaultVisibleTagIds"], [tag_recent.tag_id, tag_older.tag_id])

    def test_recency_ignores_old_deleted_and_foreign_sends(self):
        now = timezone.now()
        tag = self._make_tag()
        PersonalChannelTagAssignment.objects.create(tag=tag, channel=self.gm)
        # Outside the 30-day window.
        self._send(self.gm, self.user, seq=1, ts=now - timedelta(days=31))
        # Soft-deleted message inside the window.
        deleted = self._send(self.gm, self.user, seq=2, ts=now - timedelta(days=1))
        Message.objects.filter(id=deleted.id).update(deleted_at=now)
        # Someone else's message doesn't count as MY response.
        self._send(self.gm, self.user2, seq=3, ts=now)

        self.authenticate()
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.data["defaultVisibleTagIds"], [])

    def test_recency_caps_default_chips(self):
        now = timezone.now()
        for i in range(DEFAULT_VISIBLE_CAP + 2):
            gm = self._make_gm(f"gm{i}")
            tag = self._make_tag(name=f"tag{i}")
            PersonalChannelTagAssignment.objects.create(tag=tag, channel=gm)
            self._send(gm, self.user, seq=1, ts=now - timedelta(minutes=i))

        self.authenticate()
        resp = self.client.get(LIST_URL)
        self.assertEqual(len(resp.data["defaultVisibleTagIds"]), DEFAULT_VISIBLE_CAP)

    def test_get_requires_auth(self):
        resp = self.client.get(LIST_URL)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
