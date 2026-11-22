"""MIP-1836: import a Slack export (channels, messages, threads) into Genos group chats."""

import io
import zipfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework import status

from origin.models.chat.unified_models import (
    Channel,
    ChannelMember,
    Message,
    MessageReaction,
)
from origin.models.common.team_models import TeamMembers
from origin.services import slack_import_zip
from origin.services.slack_mrkdwn import mrkdwn_to_blocks
from origin.tests.slack_fixtures import T0, build_zip, tree
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
PREVIEW = "/api/v2/team/import/slack/preview/"
COMMIT = "/api/v2/team/import/slack/"
RUNS = "/api/v2/team/import/slack/runs/"


class SlackBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.authenticate()

    def post(self, url, zf=None, user=None, **extra):
        if user:
            self.authenticate(user)
        data = {"team_id": str(self.team.team_id), **extra}
        data["file"] = zf if zf is not None else build_zip()
        return self.client.post(url, data, format="multipart")

    def commit(self, zf=None, **extra):
        resp = self.post(COMMIT, zf, **extra)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return resp.data

    def gm(self, title):
        return Channel.objects.get(team=self.team, title=title, is_deleted=False)


class ZipSafetyTests(BaseAPITestCase):
    def read(self, buf):
        return slack_import_zip.read_export(buf)

    def code(self, buf):
        with self.assertRaises(slack_import_zip.SlackImportError) as cm:
            self.read(buf)
        return cm.exception.code

    def test_good_export_reads(self):
        ex = self.read(build_zip())
        self.assertEqual({c["name"] for c in ex.channels}, {"general", "old-stuff", "secret"})
        self.assertEqual(len(ex.messages["general"]), 8)

    def test_not_a_zip(self):
        self.assertEqual(self.code(io.BytesIO(b"nope")), "not_a_zip")

    def test_path_traversal(self):
        buf = build_zip(raw={"../evil.json": "[]"})
        self.assertEqual(self.code(buf), "zip_unsafe")

    def test_absolute_path(self):
        self.assertEqual(self.code(build_zip(raw={"/abs.json": "[]"})), "zip_unsafe")

    def test_encrypted_flag(self):
        buf = build_zip()
        zf = zipfile.ZipFile(buf, "a")
        for i in zf.infolist():
            i.flag_bits |= 1
        with mock.patch.object(slack_import_zip, "_infos", return_value=zf.infolist()):
            self.assertEqual(self.code(buf), "zip_unsafe")

    def test_ratio_bomb(self):
        buf = build_zip(raw={"general/2024-03-06.json": " " * 5_000_000})
        self.assertEqual(self.code(buf), "zip_unsafe")

    def test_too_many_entries(self):
        with mock.patch.object(slack_import_zip, "MAX_ENTRIES", 3):
            self.assertEqual(self.code(build_zip()), "zip_unsafe")

    def test_missing_users(self):
        files = tree()
        del files["users.json"]
        self.assertEqual(self.code(build_zip(files)), "not_a_slack_export")

    def test_deep_json(self):
        buf = build_zip(raw={"general/2024-03-06.json": "[" * 100000 + "]" * 100000})
        self.assertEqual(self.code(buf), "bad_json")

    def test_entry_cap_enforced_on_real_size(self):
        with mock.patch.object(slack_import_zip, "MAX_ENTRY_BYTES", 50):
            self.assertEqual(self.code(build_zip()), "export_too_large")

    def test_wrapping_folder_and_macosx_ignored(self):
        files = {f"Export/{k}": v for k, v in tree().items()}
        files["__MACOSX/x.json"] = []
        ex = self.read(build_zip(files))
        self.assertEqual(len(ex.messages["general"]), 8)

    def test_dms_skipped_counted(self):
        files = tree({"dms.json": [{"id": "D1"}], "D1/2024-03-05.json": []})
        ex = self.read(build_zip(files))
        self.assertGreaterEqual(ex.skipped_dms, 1)

    def test_caps(self):
        for const, code in (
            ("MAX_CHANNELS", "too_many_channels"),
            ("MAX_MESSAGES", "too_many_messages"),
            ("MAX_USERS", "too_many_users"),
        ):
            with self.subTest(const), mock.patch.object(slack_import_zip, const, 1):
                self.assertEqual(self.code(build_zip()), code)


class MrkdwnTests(BaseAPITestCase):
    def blocks(self, text):
        return mrkdwn_to_blocks(text, {"U2": "Bob"}, {"C2": "old-stuff"})

    def test_link_user_channel_entities_styles(self):
        b = self.blocks(
            "a *b* &amp; <@U2> <#C2|old-stuff> <!here> <https://x.test|lbl> _i_ ~s~ `c`"
        )
        flat = b[0]["content"]
        text = "".join(n["text"] if n["type"] == "text" else n["content"][0]["text"] for n in flat)
        self.assertIn("@Bob", text)
        self.assertIn("#old-stuff", text)
        self.assertIn("@here", text)
        self.assertIn("a b & ", text)
        link = [n for n in flat if n["type"] == "link"][0]
        self.assertEqual(link["href"], "https://x.test")
        self.assertTrue(any(n.get("styles", {}).get("bold") for n in flat if n["type"] == "text"))
        self.assertFalse(any(n["type"] == "mention" for n in flat))

    def test_code_fence_and_quote(self):
        b = self.blocks("```\ncode\n```\n&gt; quoted")
        self.assertEqual([x["type"] for x in b], ["codeBlock", "quote"])

    def test_unsafe_link_scheme_is_text(self):
        b = self.blocks("<javascript:alert(1)|x>")
        self.assertFalse(any(n["type"] == "link" for n in b[0]["content"]))

    def test_empty_gives_a_paragraph(self):
        self.assertEqual(self.blocks("")[0]["type"], "paragraph")


class MatchingAndPreviewTests(SlackBase):
    def preview(self, **kw):
        resp = self.post(PREVIEW, **kw)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp.data

    def test_matches_by_email_case_insensitive_and_creates_no_users(self):
        users_before = User.objects.count()
        body = self.preview()
        by = {u["slack_id"]: u for u in body["users"]}
        self.assertEqual(by["U1"]["status"], "matched")
        self.assertEqual(by["U2"]["status"], "matched")
        self.assertEqual(by["U3"]["status"], "unmatched")
        self.assertEqual(by["U4"]["status"], "bot")
        self.assertEqual(by["U5"]["status"], "deleted")
        self.assertEqual(User.objects.count(), users_before)

    def test_guest_or_removed_member_is_unmatched(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        by = {u["slack_id"]: u for u in self.preview()["users"]}
        self.assertEqual(by["U2"]["status"], "unmatched")

    def test_preview_writes_nothing(self):
        before = (Channel.objects.count(), Message.objects.count(), ChannelMember.objects.count())
        body = self.preview()
        self.assertEqual(
            before,
            (Channel.objects.count(), Message.objects.count(), ChannelMember.objects.count()),
        )
        self.assertEqual(body["counts"]["channels"], 3)
        self.assertEqual(body["counts"]["threads"], 1)
        codes = {w["code"] for w in body["warnings"]}
        self.assertIn("file_links_need_slack_login", codes)
        self.assertIn("unmatched_authors", codes)
        self.assertIn("orphan_replies", codes)
        names = {c["name"]: c for c in body["channels"]}
        self.assertEqual(names["general"]["target"]["action"], "create")
        self.assertTrue(names["secret"]["is_private"])
        self.assertTrue(names["old-stuff"]["is_archived"])
        self.assertIn("limits", body)


class GuardTests(SlackBase):
    def test_unauthenticated(self):
        self.unauthenticate()
        self.assertEqual(self.post(PREVIEW).status_code, 401)

    def test_non_member_404(self):
        outsider = User.objects.create_user(username="o", email="o@x.test", password="p")
        self.assertEqual(self.post(PREVIEW, user=outsider).status_code, 404)

    def test_member_not_owner_403(self):
        self.assertEqual(self.post(PREVIEW, user=self.user2).status_code, 403)
        self.assertEqual(self.post(COMMIT, user=self.user2).status_code, 403)

    def test_file_missing(self):
        self.authenticate()
        resp = self.client.post(PREVIEW, {"team_id": str(self.team.team_id)}, format="multipart")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "file_missing")

    def test_too_large_413(self):
        with mock.patch.object(slack_import_zip, "MAX_ZIP_BYTES", 10):
            resp = self.post(PREVIEW)
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.data["code"], "file_too_large")

    def test_bad_zip_400(self):
        f = io.BytesIO(b"junk")
        f.name = "x.zip"
        resp = self.post(PREVIEW, f)
        self.assertEqual((resp.status_code, resp.data["code"]), (400, "not_a_zip"))

    def test_concurrent_import_409(self):
        cache.add(f"slack-import:{self.team.team_id}", 1, 600)
        resp = self.post(COMMIT)
        self.assertEqual((resp.status_code, resp.data["code"]), (409, "import_in_progress"))

    def test_lock_released_after_commit(self):
        self.commit()
        self.assertTrue(cache.add(f"slack-import:{self.team.team_id}", 1, 5))

    def test_cap_returns_limit(self):
        with mock.patch.object(slack_import_zip, "MAX_MESSAGES", 2):
            resp = self.post(PREVIEW)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["limit"], 2)


class CommitTests(SlackBase):
    def test_channels_privacy_membership(self):
        out = self.commit()
        self.assertEqual(out["channels_created"], 3)
        general, secret = self.gm("general"), self.gm("secret")
        self.assertFalse(general.is_private)
        self.assertTrue(secret.is_private)
        self.assertEqual(general.owner_id, self.user.id)
        members = lambda c: set(  # noqa: E731
            ChannelMember.objects.filter(channel=c, is_deleted=False).values_list(
                "user_id", flat=True
            )
        )
        self.assertEqual(members(general), {self.user.id, self.user2.id})
        # secret: Ann (U1, the importer) + unmatched Cy. Bob is a team member but not in the channel.
        self.assertEqual(members(secret), {self.user.id})
        self.assertIsNotNone(self.gm("old-stuff").archived_at)

    def test_threads_replies_and_order(self):
        self.commit()
        ch = self.gm("general")
        root = Message.objects.get(channel=ch, body_text__startswith="Hello")
        replies = Message.objects.filter(channel=ch, thread_root=root).order_by("seq")
        self.assertEqual([r.body_text for r in replies], ["reply one", "outsider reply"])
        self.assertTrue(all(r.is_thread_reply and r.parent_id == root.id for r in replies))
        root.refresh_from_db()
        self.assertEqual(root.reply_count, 2)
        self.assertEqual(int(root.ts_sent_at.timestamp()), T0)
        seqs = list(
            Message.objects.filter(channel=ch).order_by("seq").values_list("ts_sent_at", flat=True)
        )
        self.assertEqual(seqs, sorted(seqs))

    def test_senders_unmatched_author_and_skips(self):
        self.commit()
        ch = self.gm("general")
        outsider = Message.objects.get(channel=ch, body_text="outsider reply")
        self.assertIsNone(outsider.sender)
        self.assertEqual(outsider.metadata["slackImport"]["author"], "Cy Outsider")
        self.assertNotIn("email", str(outsider.metadata))
        self.assertFalse(Message.objects.filter(channel=ch, body_text="joined").exists())
        bot = Message.objects.get(channel=ch, body_text="bot says hi")
        self.assertEqual(bot.metadata["slackImport"]["author"], "Robot")
        orphan = Message.objects.get(channel=ch, body_text="orphan")
        self.assertFalse(orphan.is_thread_reply)
        filemsg = Message.objects.get(channel=ch, body_text__startswith="file here")
        self.assertEqual(
            filemsg.metadata["slackImport"]["files"][0]["url"], "https://files.slack.test/plan.pdf"
        )

    def test_reactions_only_matched_members(self):
        self.commit()
        root = Message.objects.get(channel=self.gm("general"), body_text__startswith="Hello")
        rx = list(MessageReaction.objects.filter(message=root))
        self.assertEqual(len(rx), 1)
        self.assertEqual(rx[0].user_id, self.user2.id)
        self.assertEqual(rx[0].emoji, "👍")

    def test_title_collision(self):
        Channel.objects.create(team=self.team, kind=2, title="General", owner=self.user)
        self.commit()
        self.assertTrue(Channel.objects.filter(team=self.team, title="general (Slack)").exists())

    def test_no_side_effects(self):
        from origin.models.chat.unified_models import Activity, MessageMention

        with mock.patch("origin.views.chat.message_views.v3_activity", create=True) as act:
            self.commit()
        act.create_mention_activities.assert_not_called()
        self.assertEqual(Activity.objects.count(), 0)
        self.assertEqual(MessageMention.objects.count(), 0)

    def test_unread_is_zero_after_import(self):
        from origin.models.chat.unified_models import ReadCursor

        self.commit()
        ch = self.gm("general")
        last = Message.objects.filter(channel=ch).order_by("-seq").first()
        for uid in (self.user.id, self.user2.id):
            cur = ReadCursor.objects.get(user_id=uid, channel=ch, thread_root=None)
            self.assertEqual(cur.last_read_message.seq, last.seq)

    def test_selected_channels_only(self):
        ch_ids = '["C1"]'
        out = self.commit(channel_ids=ch_ids)
        self.assertEqual(out["channels_created"], 1)
        self.assertFalse(Channel.objects.filter(team=self.team, title="secret").exists())

    def test_reimport_creates_nothing(self):
        self.commit()
        n = Message.objects.count()
        out = self.commit()
        self.assertEqual(out["messages_created"], 0)
        self.assertEqual(out["channels_created"], 0)
        self.assertEqual(Message.objects.count(), n)

    def test_newer_export_appends_and_reply_attaches_to_old_root(self):
        self.commit()
        ch = self.gm("general")
        n = Message.objects.filter(channel=ch).count()
        newer = tree(
            {
                "general/2024-03-06.json": [
                    {
                        "type": "message",
                        "user": "U2",
                        "text": "late reply",
                        "ts": f"{T0 + 100000}.000100",
                        "thread_ts": f"{T0}.000100",
                    },
                    {
                        "type": "message",
                        "user": "U1",
                        "text": "brand new",
                        "ts": f"{T0 + 100100}.000100",
                    },
                ]
            }
        )
        out = self.commit(build_zip(newer))
        self.assertEqual(out["messages_created"], 2)
        self.assertEqual(Message.objects.filter(channel=ch).count(), n + 3)  # 2 + banner
        late = Message.objects.get(channel=ch, body_text="late reply")
        root = Message.objects.get(channel=ch, body_text__startswith="Hello")
        self.assertEqual(late.thread_root_id, root.id)
        root.refresh_from_db()
        self.assertEqual(root.reply_count, 3)
        self.assertEqual(
            Channel.objects.filter(team=self.team, title__startswith="general").count(), 1
        )

    def test_runs_list(self):
        out = self.commit()
        resp = self.client.get(RUNS, {"team_id": str(self.team.team_id)})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["runs"][0]["run_id"], out["run_id"])

    def test_archived_target_skipped(self):
        self.commit()
        Channel.objects.filter(team=self.team, title="general").update(
            archived_at="2025-01-01T00:00:00Z"
        )
        newer = tree(
            {
                "general/2024-03-06.json": [
                    {"type": "message", "user": "U1", "text": "new", "ts": f"{T0 + 99999}.000100"}
                ]
            }
        )
        out = self.commit(build_zip(newer))
        self.assertEqual(out["messages_created"], 0)
        self.assertTrue(any(r.get("warning") == "target_archived" for r in out["report"]))


def real(ch):
    return Message.objects.filter(channel=ch).exclude(correlation_id__startswith="slack-run:")


class UndoTests(SlackBase):
    def url(self, run_id):
        return f"{RUNS}{run_id}/undo/"

    def undo(self, run_id, user=None):
        if user:
            self.authenticate(user)
        return self.client.post(
            self.url(run_id), {"team_id": str(self.team.team_id)}, format="json"
        )

    def test_undo_deletes_created_channels(self):
        out = self.commit()
        plan = self.client.get(self.url(out["run_id"]), {"team_id": str(self.team.team_id)})
        self.assertEqual(plan.status_code, 200)
        self.assertEqual(plan.data["will_delete_channels"], 3)
        resp = self.undo(out["run_id"])
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(Channel.objects.filter(team=self.team, title="general").exists())
        self.assertEqual(Message.objects.count(), 0)
        self.assertEqual(self.undo(out["run_id"]).status_code, 404)  # nothing left to undo

    def test_reimport_works_after_undo(self):
        out = self.commit()
        self.undo(out["run_id"])
        again = self.commit()
        self.assertEqual(again["messages_created"], out["messages_created"])
        self.assertEqual(again["channels_created"], 3)

    def test_edited_and_foreign_messages_kept(self):
        out = self.commit()
        ch = self.gm("general")
        Message.objects.filter(channel=ch, body_text="orphan").update(
            edited_at="2025-01-01T00:00:00Z"
        )
        slack_import_seq = Message.objects.filter(channel=ch).order_by("-seq").first().seq + 1
        Message.objects.create(
            channel=ch, sender=self.user, seq=slack_import_seq, body=[], body_text="live message"
        )
        self.undo(out["run_id"])
        self.assertTrue(Channel.objects.filter(pk=ch.pk, is_deleted=False).exists())
        left = set(real(ch).values_list("body_text", flat=True))
        self.assertEqual(left, {"orphan", "live message"})

    def test_root_with_foreign_reply_kept(self):
        out = self.commit()
        ch = self.gm("general")
        root = Message.objects.get(channel=ch, body_text__startswith="Hello")
        last = Message.objects.filter(channel=ch).order_by("-seq").first().seq
        Message.objects.create(
            channel=ch,
            sender=self.user,
            seq=last + 1,
            body=[],
            body_text="my reply",
            parent=root,
            thread_root=root,
            is_thread_reply=True,
        )
        self.undo(out["run_id"])
        left = set(real(ch).values_list("body_text", flat=True))
        self.assertIn("my reply", left)
        self.assertTrue(any(t.startswith("Hello") for t in left))

    def test_non_owner_forbidden_and_unknown_run(self):
        out = self.commit()
        self.assertEqual(self.undo(out["run_id"], user=self.user2).status_code, 403)
        self.authenticate()
        import uuid

        self.assertEqual(self.undo(str(uuid.uuid4())).status_code, 404)


class ReviewRegressionTests(SlackBase):
    def test_planted_key_in_a_members_chat_is_not_mapped(self):
        from origin.services.slack_import import message_key

        mine = Channel.objects.create(team=self.team, kind=2, title="mine", owner=self.user2)
        ChannelMember.objects.create(channel=mine, user=self.user2, role="owner")
        Message.objects.create(
            channel=mine,
            sender=self.user2,
            seq=1,
            body=[],
            body_text="plant",
            correlation_id=message_key(self.team.team_id, "G1", f"{T0}.000100"),
        )
        self.commit()
        self.assertEqual(Message.objects.filter(channel=mine).count(), 1)
        self.assertTrue(Channel.objects.filter(team=self.team, title="secret").exists())

    def test_thread_broadcast_reply_is_imported(self):
        files = tree()
        files["general/2024-03-05.json"] = files["general/2024-03-05.json"] + [
            {
                "type": "message",
                "subtype": "thread_broadcast",
                "user": "U2",
                "text": "also to channel",
                "ts": f"{T0 + 500}.000100",
                "thread_ts": f"{T0}.000100",
            }
        ]
        self.commit(build_zip(files))
        m = Message.objects.get(channel=self.gm("general"), body_text="also to channel")
        self.assertTrue(m.is_thread_reply)

    def test_unread_before_import_stays_unread(self):
        from origin.models.chat.unified_models import ReadCursor

        self.commit()
        ch = self.gm("general")
        first = Message.objects.filter(channel=ch).order_by("seq").first()
        ReadCursor.objects.filter(user=self.user2, channel=ch, thread_root=None).update(
            last_read_message=first
        )
        newer = tree(
            {
                "general/2024-03-06.json": [
                    {"type": "message", "user": "U1", "text": "later", "ts": f"{T0 + 99999}.000100"}
                ]
            }
        )
        self.commit(build_zip(newer))
        cur = ReadCursor.objects.get(user=self.user2, channel=ch, thread_root=None)
        self.assertEqual(cur.last_read_message_id, first.id)

    def test_undo_removes_members_the_import_added(self):
        existing = Channel.objects.create(team=self.team, kind=2, title="x", owner=self.user)
        ChannelMember.objects.create(channel=existing, user=self.user, role="owner")
        out = self.commit()
        ch = self.gm("general")
        # Make general an "existing" chat in a later run by importing a newer export into it.
        ChannelMember.objects.filter(channel=ch, user=self.user2).delete()
        newer = tree(
            {
                "general/2024-03-06.json": [
                    {"type": "message", "user": "U1", "text": "again", "ts": f"{T0 + 99999}.000100"}
                ]
            }
        )
        run2 = self.commit(build_zip(newer))["run_id"]
        self.assertTrue(ChannelMember.objects.filter(channel=ch, user=self.user2).exists())
        resp = self.client.post(
            f"{RUNS}{run2}/undo/", {"team_id": str(self.team.team_id)}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(ChannelMember.objects.filter(channel=ch, user=self.user2).exists())
        self.assertTrue(Message.objects.filter(channel=ch).exists())
        self.assertTrue(out["run_id"])

    def test_undo_while_locked_409(self):
        out = self.commit()
        cache.add(f"slack-import:{self.team.team_id}", "other", 600)
        resp = self.client.post(
            f"{RUNS}{out['run_id']}/undo/", {"team_id": str(self.team.team_id)}, format="json"
        )
        self.assertEqual(resp.status_code, 409)
