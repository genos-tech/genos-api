"""Tests for the per-tier message-history retention window (hide, not delete).

Read-path enforcement in `message_views.py`:
  - `MessagesDeltaView` / `ThreadMessagesDeltaView` filter
    `ts_sent_at >= cutoff` AFTER the reaction-union (a fresh reaction
    can't resurrect an out-of-window message) and stamp the additive
    `retention` envelope key with a `truncated` flag.
  - `MessageDetailView.get` 404s out-of-window messages (same
    existence-hiding rule as non-membership).
  - Paid tiers (retention None) get byte-identical responses — no
    `retention` key.

Quota numbers from `TEST_QUOTAS`: free = 90 days, pro/max/ent = None.
The SHIPPED default is None for every tier (dark) — covered by the
final test class.
"""

from datetime import timedelta

from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageReaction,
)
from origin.search_engine import quota

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas


class RetentionTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Retention Test GM",
            owner=self.user,
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        self._seq = 0
        self.authenticate()
        quota.invalidate_effective_tier([self.user.id, self.user2.id])

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id, self.user2.id])
        super().tearDown()

    def make_message(self, text, *, days_ago=0, thread_root=None):
        self._seq += 1
        msg = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=self._seq,
            body=[{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
            body_text=text,
            is_thread_reply=thread_root is not None,
            thread_root=thread_root,
        )
        if days_ago:
            # ts_sent_at is auto_now_add — backdate via queryset update.
            sent = timezone.now() - timedelta(days=days_ago)
            Message.objects.filter(pk=msg.pk).update(ts_sent_at=sent, ts_updated_at=sent)
            msg.refresh_from_db()
        return msg

    def delta(self, *, thread=False, since=None):
        path = f"/api/v3/channels/{self.channel.id}/{'threads' if thread else 'messages'}/"
        if since:
            path += f"?since={since}"
        return self.client.get(path)

    def message_texts(self, res):
        return [m["bodyText"] for m in res.data["data"]["messages"]]


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class FreeTierRetentionTests(RetentionTestBase):
    def test_full_load_hides_out_of_window_and_flags_truncated(self):
        self.make_message("ancient", days_ago=120)
        self.make_message("recent")
        res = self.delta()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.message_texts(res), ["recent"])
        retention = res.data["retention"]
        self.assertEqual(retention["days"], 90)
        self.assertTrue(retention["truncated"])
        self.assertIn("cutoff", retention)

    def test_truncated_false_when_no_hidden_history(self):
        self.make_message("recent")
        res = self.delta()
        self.assertEqual(res.data["retention"]["truncated"], False)
        self.assertEqual(self.message_texts(res), ["recent"])

    def test_reaction_union_cannot_resurrect_old_message(self):
        old = self.make_message("ancient", days_ago=120)
        self.make_message("recent")
        since = (timezone.now() - timedelta(hours=1)).isoformat()
        # A fresh reaction on the ancient message would normally union
        # it back into the delta — retention must still exclude it.
        MessageReaction.objects.create(message=old, user=self.user2, emoji="+1")
        res = self.delta(since=since)
        self.assertNotIn("ancient", self.message_texts(res))

    def test_thread_delta_filtered_and_flagged(self):
        root = self.make_message("root")
        self.make_message("old reply", days_ago=120, thread_root=root)
        self.make_message("new reply", thread_root=root)
        res = self.delta(thread=True)
        self.assertEqual(self.message_texts(res), ["new reply"])
        self.assertTrue(res.data["retention"]["truncated"])

    def test_detail_get_404_for_out_of_window(self):
        old = self.make_message("ancient", days_ago=120)
        recent = self.make_message("recent")
        self.assertEqual(self.client.get(f"/api/v3/messages/{old.id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v3/messages/{recent.id}/").status_code, 200)


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class PaidTierRetentionTests(RetentionTestBase):
    def _upgrade(self, tier):
        self.user.tier = tier
        self.user.save(update_fields=["tier"])
        quota.invalidate_effective_tier([self.user.id])

    def test_pro_sees_full_history_and_no_retention_key(self):
        self.make_message("ancient", days_ago=120)
        self.make_message("recent")
        self._upgrade("pro")
        res = self.delta()
        self.assertEqual(self.message_texts(res), ["ancient", "recent"])
        self.assertNotIn("retention", res.data)

    def test_upgrade_instantly_restores_deep_link(self):
        old = self.make_message("ancient", days_ago=120)
        self.assertEqual(self.client.get(f"/api/v3/messages/{old.id}/").status_code, 404)
        self._upgrade("pro")
        self.assertEqual(self.client.get(f"/api/v3/messages/{old.id}/").status_code, 200)

    def test_team_plan_lifts_retention_for_member(self):
        self.make_message("ancient", days_ago=120)
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        quota.invalidate_effective_tier([self.user.id])
        res = self.delta()
        self.assertIn("ancient", self.message_texts(res))
        self.assertNotIn("retention", res.data)


class ShippedDefaultsRetentionTests(RetentionTestBase):
    """SHIPPED config (enable PR): free = 90-day window; paid tiers
    unlimited."""

    def test_free_user_default_window_is_90_days(self):
        self.assertEqual(
            settings.SEARCH_ENGINE["TIER_QUOTAS"]["free"]["message_retention_days"], 90
        )
        self.make_message("ancient", days_ago=120)
        self.make_message("recent")
        res = self.delta()
        self.assertEqual(self.message_texts(res), ["recent"])
        self.assertTrue(res.data["retention"]["truncated"])

    def test_pro_default_is_unlimited(self):
        self.user.tier = "pro"
        self.user.save(update_fields=["tier"])
        quota.invalidate_effective_tier([self.user.id])
        self.make_message("ancient", days_ago=400)
        res = self.delta()
        self.assertEqual(self.message_texts(res), ["ancient"])
        self.assertNotIn("retention", res.data)
