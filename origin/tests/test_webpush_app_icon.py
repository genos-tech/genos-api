"""MIP-1600: the web push card shows the connected app's mark when the change came from one.

The in-app surfaces already draw the Claude / ChatGPT mark from `Activity.meta["via"]` (MIP-1573) and
the AI-app notice's `meta["appFamily"]` (MIP-1571). The Web Push card is the OS notification the
service worker builds from the payload, so what can change here is the payload's `icon` (the large
picture) and `badge` (the monochrome status-bar glyph on Android).

Rules pinned here:

  * Only the exact family whitelist (`mcp_client_family.APP_FAMILIES`) earns the mark; the row's `via`
    is read through `via_for_viewer` with the row's recipient as the viewer, so the same switch
    (`SHOW_OAUTH_APP_TO_EVERYONE`) that governs the in-app card governs the push.
  * The minimal card (the task or note a recipient cannot open) stays as it was: no app mark.
  * Everything else of the payload (title, body, url, tag, actions, badge_count, category, mutes) is
    exactly what it was.
  * Any other push (a message, an inbox item, a reminder) is untouched.
"""

import uuid
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from origin.models.chat.unified_models import ActivityType, ChannelKind
from origin.models.common.notification_models import PushSubscription
from origin.services import webpush_dispatch
from origin.services.mcp_client_family import APP_FAMILIES
from origin.services.v3_activity import SURFACE_TASK_BODY
from origin.tests.test_base import BaseAPITestCase
from origin.views.public.mcp import via_privacy

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

CLAUDE_ICON = "/ai-apps/claude-192.png"
CLAUDE_BADGE = "/ai-apps/claude-badge-96.png"
CHATGPT_ICON = "/ai-apps/chatgpt-192.png"
CHATGPT_BADGE = "/ai-apps/chatgpt-badge-96.png"

CLAUDE_VIA = {"kind": "oauth", "app": "claude"}
CHATGPT_VIA = {"kind": "oauth", "app": "chatgpt"}


@override_settings(CACHES=LOCMEM)
class AppIconPushTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        PushSubscription.objects.create(
            user=self.user2,
            endpoint=f"https://example.com/{uuid.uuid4()}",
            p256dh="p256",
            auth="auth",
        )

    def _activity(
        self,
        atype=ActivityType.MENTION,
        *,
        meta=None,
        surface_type=None,
        channel=True,
        actor_id=None,
        avatar="",
    ):
        return SimpleNamespace(
            id=uuid.uuid4(),
            activity_type=atype,
            recipient_id=self.user2.id,
            actor_id=actor_id or uuid.uuid4(),
            actor=SimpleNamespace(username="Alice", profile_image_file_name=avatar),
            channel=SimpleNamespace(kind=ChannelKind.DM, id="chan-uuid") if channel else None,
            message_id=uuid.uuid4(),
            message=SimpleNamespace(
                body_text="hello there", metadata={}, thread_root_id=None, parent_id=None
            ),
            surface_type=surface_type,
            meta=(meta or {}),
        )

    def _payload(self, act, *, minimal=False):
        with (
            mock.patch.object(webpush_dispatch, "vapid_configured", return_value=True),
            mock.patch.object(webpush_dispatch, "send_web_push") as send,
            mock.patch.object(
                webpush_dispatch._executor, "submit", side_effect=lambda fn, **kw: fn(**kw)
            ),
            mock.patch.object(
                webpush_dispatch,
                "minimal_card_ids_failing_closed",
                return_value={act.id} if minimal else set(),
            ),
        ):
            webpush_dispatch.dispatch_push_for_activities([act])
        send.assert_called_once()
        return send.call_args.kwargs["payload"]

    def _assert_plain(self, payload):
        """The card as it was: no app mark, no `badge` key (the SW keeps its own)."""
        self.assertIsNone(payload["icon"])
        self.assertNotIn("badge", payload)

    # -- the rows a connected app writes ------------------------------------------------------

    def test_claude_mention_carries_the_claude_mark(self):
        payload = self._payload(self._activity(meta={"via": CLAUDE_VIA}))
        self.assertEqual(payload["icon"], CLAUDE_ICON)
        self.assertEqual(payload["badge"], CLAUDE_BADGE)

    def test_chatgpt_mention_carries_the_chatgpt_mark(self):
        payload = self._payload(self._activity(meta={"via": CHATGPT_VIA}))
        self.assertEqual(payload["icon"], CHATGPT_ICON)
        self.assertEqual(payload["badge"], CHATGPT_BADGE)

    def test_every_kind_of_row_a_connected_app_writes_gets_the_mark(self):
        rows = {
            "task-body mention": self._activity(
                surface_type=SURFACE_TASK_BODY, meta={"via": CLAUDE_VIA, "taskId": 1}
            ),
            "assignment": self._activity(meta={"via": CLAUDE_VIA, "taskAssign": True}),
            "comment participant": self._activity(
                ActivityType.THREAD_REPLY,
                channel=False,
                surface_type=SURFACE_TASK_BODY,
                meta={"via": CLAUDE_VIA, "isCommentParticipant": True, "displayId": "A-1"},
            ),
        }
        for name, act in rows.items():
            with self.subTest(name):
                self.assertEqual(self._payload(act)["icon"], CLAUDE_ICON)

    def test_the_rest_of_the_card_is_unchanged(self):
        with_app = self._payload(self._activity(meta={"via": CLAUDE_VIA}))
        without = self._payload(self._activity(meta={}))
        for key in ("title", "body", "url", "actions", "requireInteraction"):
            self.assertEqual(with_app[key], without[key], key)
        self.assertEqual(with_app["tag"].split(":")[0], without["tag"].split(":")[0])
        self.assertEqual(with_app["title"], "Alice mentioned you")
        self.assertEqual(with_app["url"], "/workspace/chat/dm/chan-uuid")

    # -- no mark ------------------------------------------------------------------------------

    def test_no_via_is_unchanged(self):
        self._assert_plain(self._payload(self._activity(meta={})))

    def test_a_connected_app_with_no_known_family_is_unchanged(self):
        self._assert_plain(self._payload(self._activity(meta={"via": {"kind": "oauth"}})))

    def test_an_api_key_never_earns_a_mark_even_with_an_app_field(self):
        for via in ({"kind": "api_key"}, {"kind": "api_key", "app": "claude"}):
            with self.subTest(via=via):
                self._assert_plain(self._payload(self._activity(meta={"via": via})))

    def test_other_kinds_never_earn_a_mark(self):
        for kind in ("agent", "app", "github", "unknown", "pr_merge", "", None, 5):
            with self.subTest(kind=kind):
                via = {"kind": kind, "app": "claude"}
                self._assert_plain(self._payload(self._activity(meta={"via": via})))

    def test_spoofed_family_strings_never_earn_a_mark(self):
        spoofs = [
            "Claude",
            "CLAUDE",
            " claude",
            "claude ",
            "claude\n",
            "claude.ai",
            "claude-code",
            "claudex",
            "chat gpt",
            "ChatGPT",
            "../claude",
            "",
            None,
            0,
            True,
            ["claude"],
            {"claude": 1},
        ]
        for app in spoofs:
            with self.subTest(app=app):
                via = {"kind": "oauth", "app": app}
                self._assert_plain(self._payload(self._activity(meta={"via": via})))

    def test_a_via_that_is_not_a_dict_never_earns_a_mark(self):
        for via in ("oauth", "claude", ["oauth"], 7, True, {}):
            with self.subTest(via=via):
                self._assert_plain(self._payload(self._activity(meta={"via": via})))

    def test_a_planted_top_level_appFamily_on_a_member_row_does_nothing(self):
        # `appFamily` is the AI-app notice's key; a mention row's meta is never read for it.
        self._assert_plain(self._payload(self._activity(meta={"appFamily": "claude"})))

    def test_self_row_family_is_judged_by_the_dispatcher_itself(self):
        # On a row whose actor IS the recipient (a member's own app assigning them, say)
        # `via_for_viewer` hands the `via` back whole, so the viewer rule filters nothing and the
        # dispatcher's own checks (kind == oauth, exact family) are the only gate left.
        own = self.user2.id
        good = {"claude": CLAUDE_VIA, "chatgpt": CHATGPT_VIA}
        for family, via in good.items():
            with self.subTest(family=family):
                act = self._activity(meta={"via": via}, actor_id=own)
                self.assertEqual(webpush_dispatch._app_family(act), family)
        full = {"kind": "oauth", "app": "claude", "connection_id": "c-1", "client_name": "Mine"}
        self.assertEqual(
            webpush_dispatch._app_family(self._activity(meta={"via": full}, actor_id=own)),
            "claude",
        )
        bad = [
            {"kind": "api_key", "app": "claude"},
            {"kind": "agent", "app": "chatgpt"},
            {"kind": "app", "app": "claude"},
            {"app": "claude"},
            {"kind": "oauth", "app": "Claude"},
            {"kind": "oauth", "app": "claude "},
            {"kind": "oauth", "app": "claude.ai"},
            {"kind": "oauth", "app": ["claude"]},
            {"kind": "oauth", "app": None},
            {"kind": "oauth"},
            "oauth",
            None,
        ]
        for via in bad:
            with self.subTest(via=via):
                act = self._activity(meta={"via": via}, actor_id=own)
                self.assertIsNone(webpush_dispatch._app_family(act))
                self._assert_plain(self._payload(act))
        for fam in ("Claude", "claude.ai", ["claude"], None, 1):
            with self.subTest(notice=fam):
                self.assertIsNone(
                    webpush_dispatch._app_family(self._notice(appFamily=fam, via=CLAUDE_VIA))
                )

    # -- the viewer rule is the in-app card's --------------------------------------------------

    def test_switch_off_hides_the_family_from_everyone_but_the_actor(self):
        act = self._activity(meta={"via": CLAUDE_VIA})
        with mock.patch.object(via_privacy, "SHOW_OAUTH_APP_TO_EVERYONE", False):
            self._assert_plain(self._payload(act))
            own = self._activity(meta={"via": CLAUDE_VIA}, actor_id=self.user2.id)
            self.assertEqual(self._payload(own)["icon"], CLAUDE_ICON)

    # -- the minimal card stays minimal -------------------------------------------------------

    def test_minimal_card_carries_no_app_mark(self):
        act = self._activity(
            surface_type=SURFACE_TASK_BODY,
            meta={"via": CLAUDE_VIA, "taskId": 9, "projectId": 3, "displayId": "SECRET-9"},
        )
        payload = self._payload(act, minimal=True)
        self._assert_plain(payload)
        self.assertEqual(payload["url"], "/workspace/tasks")
        self.assertNotIn("SECRET-9", str(payload))
        # and the same row for a reader who can open the task does carry it
        full = self._payload(act, minimal=False)
        self.assertEqual(full["icon"], CLAUDE_ICON)

    def test_minimal_card_keeps_the_avatar(self):
        with override_settings(WEBPUSH_MEDIA_BASE_URL="https://media.example/media"):
            act = self._activity(
                surface_type=SURFACE_TASK_BODY,
                meta={"via": CLAUDE_VIA, "taskId": 9},
                avatar="avatars/a.png",
            )
            payload = self._payload(act, minimal=True)
        self.assertEqual(payload["icon"], "https://media.example/media/avatars/a.png")
        self.assertNotIn("badge", payload)

    # -- the avatar is replaced only by the mark ----------------------------------------------

    def test_the_mark_replaces_the_senders_avatar_and_no_mark_keeps_it(self):
        with override_settings(WEBPUSH_MEDIA_BASE_URL="https://media.example/media"):
            plain = self._payload(self._activity(meta={}, avatar="avatars/a.png"))
            app = self._payload(self._activity(meta={"via": CHATGPT_VIA}, avatar="avatars/a.png"))
        self.assertEqual(plain["icon"], "https://media.example/media/avatars/a.png")
        self.assertNotIn("badge", plain)
        self.assertEqual(app["icon"], CHATGPT_ICON)

    def test_the_mark_needs_no_media_setting(self):
        # Root-relative: the service worker resolves it against its own (the web app's) origin, so
        # there is no base to be missing and no broken URL to emit.
        with override_settings(WEBPUSH_MEDIA_BASE_URL=""):
            payload = self._payload(self._activity(meta={"via": CLAUDE_VIA}))
        self.assertTrue(payload["icon"].startswith("/ai-apps/"))
        self.assertNotIn("//", payload["icon"])

    # -- the AI-app notice --------------------------------------------------------------------

    def _notice(self, **meta):
        base = {
            "aiApp": True,
            "app": "Claude",
            "verb": "created",
            "kind": "task",
            "count": 1,
            "items": [{"title": "Fix it"}],
            "url": "/workspace/tasks",
        }
        return self._activity(ActivityType.AI_APP, channel=False, meta={**base, **meta})

    def test_ai_app_notice_carries_the_mark_of_its_family(self):
        claude = self._payload(self._notice(appFamily="claude"))
        chatgpt = self._payload(self._notice(appFamily="chatgpt", app="ChatGPT"))
        self.assertEqual((claude["icon"], claude["badge"]), (CLAUDE_ICON, CLAUDE_BADGE))
        self.assertEqual((chatgpt["icon"], chatgpt["badge"]), (CHATGPT_ICON, CHATGPT_BADGE))
        self.assertTrue(claude["title"].startswith("Claude "))

    def test_ai_app_notice_without_or_with_a_bad_family_is_unchanged(self):
        for fam in (None, "", "Claude", "claude.ai", "other", ["claude"], 3):
            with self.subTest(fam=fam):
                meta = {} if fam is None else {"appFamily": fam}
                self._assert_plain(self._payload(self._notice(**meta)))

    def test_ai_app_notice_ignores_a_via_key(self):
        # The notice's family is `appFamily`; a `via` on its meta is not read.
        self._assert_plain(self._payload(self._notice(via=CLAUDE_VIA)))

    # -- everything else ----------------------------------------------------------------------

    def test_other_pushes_are_untouched(self):
        with (
            mock.patch.object(webpush_dispatch, "send_web_push") as send,
            mock.patch.object(
                webpush_dispatch._executor, "submit", side_effect=lambda fn, **kw: fn(**kw)
            ),
        ):
            webpush_dispatch._queue_push(
                recipient_id=self.user2.id,
                category="inbox",
                title="t",
                body="b",
                url="/workspace/inbox",
                tag="inbox:1",
                actor=SimpleNamespace(profile_image_file_name=""),
            )
        self._assert_plain(send.call_args.kwargs["payload"])

    def test_every_official_family_has_a_mark(self):
        self.assertEqual(set(webpush_dispatch.APP_MARKS), set(APP_FAMILIES))
        for family, mark in webpush_dispatch.APP_MARKS.items():
            self.assertEqual(mark["icon"], f"/ai-apps/{family}-192.png")
            self.assertEqual(mark["badge"], f"/ai-apps/{family}-badge-96.png")
