"""Unit tests for the serializers under origin/serializers/{chat,common,note,project,task}/.

These tests favour pure ``serializer.is_valid()`` / ``serializer.data`` assertions.
Model instances are built via ``BaseAPITestCase`` fixtures only where a real
instance is required (representation / to_representation tests).

Key behaviours asserted:
    * Validation branches (required fields, invalid values -> errors).
    * to_representation field shaping (camelCase keys, computed fields).
    * FieldFile -> string handling for image fields.

NOTE on key style: camelCase renaming is NOT universal. Only the chat-unified,
todo, and a couple of note serializers rename to camelCase. The ``__all__``
ModelSerializers, ``UserSerializer`` and ``NotificationPreferenceSerializer``
keep the snake_case model field names. Each test asserts the actual style for
its serializer.
"""

from django.core.files.uploadedfile import SimpleUploadedFile

from origin.tests.test_base import BaseAPITestCase

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageReaction,
)
from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster

from origin.serializers.chat.todo_serializers import (
    ToDoCategorySerializer,
    ToDoGroupSerializer,
    ToDoItemSerializer,
)
from origin.serializers.chat.unified_serializers import (
    ChannelSerializer,
    DeltaEnvelopeSerializer,
    MessageReactionSerializer,
    MessageSerializer,
    UserLiteSerializer,
)
from origin.serializers.common.notification_serializers import (
    NotificationPreferenceSerializer,
)
from origin.serializers.common.user_serializers import (
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    UserCreateSerializer,
    UserSerializer,
)
from origin.serializers.note.note_serializers import (
    NoteRoleMemberSerializer,
    NoteVersionDetailSerializer,
    NoteVersionListItemSerializer,
)
from origin.serializers.project.prj_serializers import ProjectMasterSerializer
from origin.serializers.task.task_serializers import TaskMasterSerializer
from origin.serializers.common.team_serializers import (
    TeamMasterSerializer,
    TeamMembersSerializer,
)


# ======================================================================
# NotificationPreferenceSerializer.validate_muted_chats  (richest logic)
# ======================================================================
class NotificationPreferenceSerializerTests(BaseAPITestCase):
    """Covers every raise branch + the two non-error behaviours
    (dedup via ``continue`` and ``chat_name`` dropped when falsy)."""

    def _serializer(self, muted_chats):
        return NotificationPreferenceSerializer(data={"muted_chats": muted_chats})

    def test_valid_minimal_entry_normalizes(self):
        s = self._serializer([{"chat_type": 1, "chat_id": "abc"}])
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(
            s.validated_data["muted_chats"],
            [{"chat_type": 1, "chat_id": "abc"}],
        )

    def test_valid_entry_with_chat_name_kept(self):
        s = self._serializer(
            [{"chat_type": 2, "chat_id": "xyz", "chat_name": "General"}]
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(
            s.validated_data["muted_chats"],
            [{"chat_type": 2, "chat_id": "xyz", "chat_name": "General"}],
        )

    def test_falsy_chat_name_dropped_from_output(self):
        """``chat_name`` empty string is falsy -> dropped (only added when truthy)."""
        s = self._serializer([{"chat_type": 1, "chat_id": "abc", "chat_name": ""}])
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(
            s.validated_data["muted_chats"],
            [{"chat_type": 1, "chat_id": "abc"}],
        )
        self.assertNotIn("chat_name", s.validated_data["muted_chats"][0])

    def test_dedup_same_key_collapses(self):
        """Two entries with identical (chat_type, chat_id) -> second skipped."""
        s = self._serializer(
            [
                {"chat_type": 1, "chat_id": "abc", "chat_name": "First"},
                {"chat_type": 1, "chat_id": "abc", "chat_name": "Second"},
            ]
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(len(s.validated_data["muted_chats"]), 1)
        # The FIRST one wins (the second is dropped by `continue`).
        self.assertEqual(s.validated_data["muted_chats"][0]["chat_name"], "First")

    def test_different_keys_both_kept(self):
        s = self._serializer(
            [
                {"chat_type": 1, "chat_id": "abc"},
                {"chat_type": 2, "chat_id": "abc"},  # same id, different type
            ]
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(len(s.validated_data["muted_chats"]), 2)

    def test_empty_list_is_valid(self):
        s = self._serializer([])
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["muted_chats"], [])

    # ---- raise branches ----

    def test_non_list_rejected(self):
        s = self._serializer({"not": "a list"})
        self.assertFalse(s.is_valid())
        self.assertIn("muted_chats", s.errors)

    def test_non_dict_entry_rejected(self):
        s = self._serializer(["not-a-dict"])
        self.assertFalse(s.is_valid())
        self.assertIn("muted_chats", s.errors)

    def test_chat_type_not_int_rejected(self):
        s = self._serializer([{"chat_type": "1", "chat_id": "abc"}])
        self.assertFalse(s.is_valid())
        self.assertIn("muted_chats", s.errors)

    def test_chat_type_bool_is_treated_as_int(self):
        """Python ``bool`` is a subclass of ``int``; documents actual behaviour.

        ``isinstance(True, int)`` is True, so a boolean chat_type passes the
        int check (current behaviour, not necessarily intended)."""
        s = self._serializer([{"chat_type": True, "chat_id": "abc"}])
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["muted_chats"][0]["chat_type"], True)

    def test_chat_id_empty_string_rejected(self):
        s = self._serializer([{"chat_type": 1, "chat_id": ""}])
        self.assertFalse(s.is_valid())
        self.assertIn("muted_chats", s.errors)

    def test_chat_id_non_string_rejected(self):
        s = self._serializer([{"chat_type": 1, "chat_id": 5}])
        self.assertFalse(s.is_valid())
        self.assertIn("muted_chats", s.errors)

    def test_chat_id_missing_rejected(self):
        s = self._serializer([{"chat_type": 1}])
        self.assertFalse(s.is_valid())
        self.assertIn("muted_chats", s.errors)

    def test_chat_name_non_string_rejected(self):
        s = self._serializer(
            [{"chat_type": 1, "chat_id": "abc", "chat_name": 123}]
        )
        self.assertFalse(s.is_valid())
        self.assertIn("muted_chats", s.errors)

    def test_chat_name_none_allowed(self):
        """chat_name=None is explicitly accepted (optional metadata)."""
        s = self._serializer(
            [{"chat_type": 1, "chat_id": "abc", "chat_name": None}]
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertNotIn("chat_name", s.validated_data["muted_chats"][0])

    def test_representation_keeps_snake_case_and_read_only_fields(self):
        """to_representation: snake_case keys; ts_updated_at read-only present."""
        from origin.models.common.notification_models import NotificationPreference

        pref = NotificationPreference.objects.create(user=self.user)
        data = NotificationPreferenceSerializer(pref).data
        self.assertEqual(
            set(data.keys()),
            {
                "master_enabled",
                "enable_chats",
                "enable_thread_replies",
                "enable_mentions",
                "enable_task_comments",
                "enable_inbox",
                "muted_chats",
                "ts_updated_at",
            },
        )
        self.assertTrue(data["master_enabled"])
        self.assertEqual(data["muted_chats"], [])


# ======================================================================
# User serializers
# ======================================================================
class UserSerializerTests(BaseAPITestCase):
    def test_user_serializer_snake_case_keys(self):
        data = UserSerializer(self.user).data
        # snake_case is kept (ModelSerializer with explicit fields list).
        self.assertEqual(data["email"], "test@example.com")
        self.assertEqual(data["username"], "testuser")
        self.assertIn("profile_image_url", data)
        self.assertIn("ts_created_at", data)
        # id is a UUID rendered as a string.
        self.assertEqual(str(self.user.id), data["id"])

    def test_profile_image_url_empty_filefield_is_none(self):
        """FieldFile with no file -> DRF FileField renders None (empty is falsy)."""
        data = UserSerializer(self.user).data
        self.assertIsNone(data["profile_image_url"])

    def test_profile_image_url_with_file_renders_string(self):
        """When a real file is attached, the FileField renders its path/url string."""
        self.user.profile_image_url = SimpleUploadedFile(
            "avatar.png", b"fakeimagecontent", content_type="image/png"
        )
        self.user.save()
        data = UserSerializer(self.user).data
        self.assertIsInstance(data["profile_image_url"], str)
        self.assertIn("avatar", data["profile_image_url"])
        # Clean up the file written to MEDIA_ROOT.
        self.user.profile_image_url.delete(save=False)


class UserCreateSerializerTests(BaseAPITestCase):
    def test_valid_create_payload(self):
        s = UserCreateSerializer(
            data={
                "username": "newperson",
                "email": "brand-new@example.com",  # fresh email -> UniqueValidator OK
                "password": "supersecret",
            }
        )
        self.assertTrue(s.is_valid(), s.errors)

    def test_password_too_short_rejected(self):
        s = UserCreateSerializer(
            data={
                "username": "shorty",
                "email": "shorty@example.com",
                "password": "short",  # < 8 chars
            }
        )
        self.assertFalse(s.is_valid())
        self.assertIn("password", s.errors)

    def test_password_is_write_only(self):
        s = UserCreateSerializer(
            data={
                "username": "writeonly",
                "email": "writeonly@example.com",
                "password": "longenough123",
            }
        )
        self.assertTrue(s.is_valid(), s.errors)
        user = s.save()
        self.assertNotIn("password", s.data)
        # Password was hashed, not stored plaintext.
        self.assertTrue(user.check_password("longenough123"))

    def test_duplicate_email_rejected(self):
        """UniqueValidator on email hits the DB; existing email is rejected."""
        s = UserCreateSerializer(
            data={
                "username": "dupe",
                "email": "test@example.com",  # already exists (self.user)
                "password": "longenough123",
            }
        )
        self.assertFalse(s.is_valid())
        self.assertIn("email", s.errors)

    def test_missing_required_fields(self):
        s = UserCreateSerializer(data={})
        self.assertFalse(s.is_valid())
        self.assertIn("username", s.errors)
        self.assertIn("email", s.errors)
        self.assertIn("password", s.errors)


class PasswordSerializerTests(BaseAPITestCase):
    def test_password_reset_request_requires_valid_email(self):
        self.assertFalse(PasswordResetRequestSerializer(data={"email": "notanemail"}).is_valid())
        self.assertTrue(
            PasswordResetRequestSerializer(data={"email": "ok@example.com"}).is_valid()
        )

    def test_password_reset_confirm_min_length(self):
        s = PasswordResetConfirmSerializer(
            data={"token": "tok", "new_password": "short"}
        )
        self.assertFalse(s.is_valid())
        self.assertIn("new_password", s.errors)

    def test_password_reset_confirm_valid(self):
        s = PasswordResetConfirmSerializer(
            data={"token": "tok", "new_password": "longenough123"}
        )
        self.assertTrue(s.is_valid(), s.errors)
        # new_password is write_only.
        self.assertNotIn("new_password", s.data)


# ======================================================================
# Chat unified serializers
# ======================================================================
class UserLiteSerializerTests(BaseAPITestCase):
    def test_camelcase_shape(self):
        data = UserLiteSerializer(self.user).data
        self.assertEqual(
            set(data.keys()),
            {"userId", "userName", "userEmail", "avatarImgPath", "isSystemUser"},
        )
        self.assertEqual(data["userId"], str(self.user.id))
        self.assertEqual(data["userName"], "testuser")
        self.assertEqual(data["userEmail"], "test@example.com")
        self.assertFalse(data["isSystemUser"])

    def test_avatar_img_path_from_filename_field(self):
        """avatarImgPath maps to the CharField profile_image_file_name, not the FileField."""
        self.user.profile_image_file_name = "avatars/me.png"
        self.user.save()
        data = UserLiteSerializer(self.user).data
        self.assertEqual(data["avatarImgPath"], "avatars/me.png")


class MessageSerializerTests(BaseAPITestCase):
    def _channel(self, kind=ChannelKind.GM):
        return Channel.objects.create(team=self.team, kind=kind, title="C")

    def _message(self, channel, **overrides):
        defaults = dict(
            channel=channel,
            sender=self.user,
            seq=1,
            body={"blocks": [{"type": "p", "text": "hi"}]},
            body_text="hi",
            metadata={},
        )
        defaults.update(overrides)
        return Message.objects.create(**defaults)

    def test_message_camelcase_shape_and_null_task_fields(self):
        ch = self._channel(ChannelKind.GM)
        msg = self._message(ch)
        data = MessageSerializer(msg).data
        expected_keys = {
            "id", "channelId", "channelKind", "sender", "seq", "body",
            "bodyText", "parentId", "threadRootId", "isThreadReply",
            "replyCount", "reactions", "mentions", "attachments", "metadata",
            "taskId", "displayId", "taskStatus", "editedAt", "deletedAt",
            "tsSent", "tsUpdated",
        }
        self.assertEqual(set(data.keys()), expected_keys)
        self.assertEqual(data["id"], str(msg.id))
        self.assertEqual(data["channelId"], str(ch.id))
        self.assertEqual(data["channelKind"], ChannelKind.GM)
        self.assertEqual(data["bodyText"], "hi")
        self.assertEqual(data["sender"]["userId"], str(self.user.id))
        # No task -> the PM-only computed fields are all null.
        self.assertIsNone(data["taskId"])
        self.assertIsNone(data["displayId"])
        self.assertIsNone(data["taskStatus"])
        # No reactions/mentions/attachments yet.
        self.assertEqual(data["reactions"], [])
        self.assertEqual(data["mentions"], [])
        self.assertEqual(data["attachments"], [])
        self.assertIsNone(data["parentId"])
        self.assertIsNone(data["threadRootId"])

    def test_display_id_property_fallback(self):
        """TaskMaster.display_id falls back to '#<task_id>' with no project number."""
        task = TaskMaster.objects.create(
            team=self.team, title="A task", status="Open"
        )
        self.assertEqual(task.display_id, f"#{task.task_id}")

    def test_message_with_task_populates_display_id_and_status(self):
        task = TaskMaster.objects.create(
            team=self.team, title="A task", status="Open"
        )
        ch = self._channel(ChannelKind.PM)
        msg = self._message(ch, task=task, metadata={"taskId": task.task_id})
        data = MessageSerializer(msg).data
        self.assertEqual(data["taskId"], task.task_id)
        self.assertEqual(data["displayId"], f"#{task.task_id}")
        self.assertEqual(data["taskStatus"], "Open")
        self.assertEqual(data["metadata"], {"taskId": task.task_id})

    def test_display_id_with_project_code(self):
        """display_id uses '<code>-<number>' when project code + number present."""
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Display Project",
            owner=self.user,
            project_system_user=self.user,
            code="DIS",
        )
        task = TaskMaster.objects.create(
            team=self.team,
            project=project,
            title="Coded task",
            status="Open",
            project_task_number=7,
        )
        self.assertEqual(task.display_id, "DIS-7")
        ch = self._channel(ChannelKind.PM)
        msg = self._message(ch, task=task)
        self.assertEqual(MessageSerializer(msg).data["displayId"], "DIS-7")


class MessageReactionSerializerTests(BaseAPITestCase):
    def test_reaction_shape(self):
        ch = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="C")
        msg = Message.objects.create(
            channel=ch, sender=self.user, seq=1, body={}, body_text="", metadata={}
        )
        reaction = MessageReaction.objects.create(
            message=msg, user=self.user, emoji="👍"
        )
        data = MessageReactionSerializer(reaction).data
        self.assertEqual(set(data.keys()), {"id", "user", "emoji", "tsSent"})
        self.assertEqual(data["emoji"], "👍")
        self.assertEqual(data["user"]["userId"], str(self.user.id))
        self.assertEqual(data["id"], str(reaction.id))


class ChannelSerializerTests(BaseAPITestCase):
    def test_gm_channel_shape_no_context_needed(self):
        """Non-DM kind: dmPartner short-circuits to None before touching request."""
        ch = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Engineering",
            profile_image_url="channels/eng.png",
            is_private=False,
        )
        data = ChannelSerializer(ch).data
        expected = {
            "id", "kind", "title", "profileImageUrl", "projectId", "ownerId",
            "isPrivate", "legacyChatId", "latestMessage", "unreadCount",
            "members", "dmPartner", "tsCreated", "tsUpdated",
        }
        self.assertEqual(set(data.keys()), expected)
        self.assertEqual(data["title"], "Engineering")
        self.assertEqual(data["kind"], ChannelKind.GM)
        self.assertEqual(data["profileImageUrl"], "channels/eng.png")
        self.assertFalse(data["isPrivate"])
        # GM is not DM/MDM -> members is [] and dmPartner is None.
        self.assertEqual(data["members"], [])
        self.assertIsNone(data["dmPartner"])
        self.assertIsNone(data["latestMessage"])
        self.assertEqual(data["unreadCount"], 0)
        self.assertIsNone(data["projectId"])
        self.assertIsNone(data["legacyChatId"])

    def test_dm_channel_dm_partner_resolution(self):
        """dmPartner resolves the OTHER member relative to request.user."""
        ch = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="")
        ChannelMember.objects.create(channel=ch, user=self.user, role="member")
        ChannelMember.objects.create(channel=ch, user=self.user2, role="member")

        class _Req:
            pass

        req = _Req()
        req.user = self.user
        data = ChannelSerializer(ch, context={"request": req}).data
        # Partner is user2 (the member who isn't the viewer).
        self.assertIsNotNone(data["dmPartner"])
        self.assertEqual(data["dmPartner"]["userId"], str(self.user2.id))
        self.assertEqual(data["dmPartner"]["userName"], "otheruser")
        self.assertEqual(data["dmPartner"]["userEmail"], "other@example.com")
        # members list is populated for DM.
        member_ids = {m["userId"] for m in data["members"]}
        self.assertEqual(member_ids, {str(self.user.id), str(self.user2.id)})

    def test_dm_channel_no_members_dm_partner_none(self):
        ch = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="")

        class _Req:
            pass

        req = _Req()
        req.user = self.user
        data = ChannelSerializer(ch, context={"request": req}).data
        self.assertIsNone(data["dmPartner"])


class DeltaEnvelopeSerializerTests(BaseAPITestCase):
    def test_force_full_reload_defaults_false(self):
        from django.utils import timezone

        s = DeltaEnvelopeSerializer(
            data={"serverTime": timezone.now().isoformat(), "data": {"messages": []}}
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertFalse(s.validated_data["forceFullReload"])

    def test_missing_server_time_invalid(self):
        s = DeltaEnvelopeSerializer(data={"data": {}})
        self.assertFalse(s.is_valid())
        self.assertIn("serverTime", s.errors)


# ======================================================================
# ToDo serializers (camelCase + computed)
# ======================================================================
class ToDoSerializerTests(BaseAPITestCase):
    def _group(self):
        from datetime import date

        return ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=date(2026, 5, 31)
        )

    def test_category_shape(self):
        cat = ToDoCategory.objects.create(
            team=self.team, user=self.user, name="Work", sort_order=3
        )
        data = ToDoCategorySerializer(cat).data
        self.assertEqual(
            set(data.keys()),
            {"categoryId", "name", "sortOrder", "tsCreatedAt", "tsUpdatedAt"},
        )
        self.assertEqual(data["categoryId"], cat.category_id)
        self.assertEqual(data["name"], "Work")
        self.assertEqual(data["sortOrder"], 3)

    def test_item_shape_and_camelcase(self):
        group = self._group()
        item = ToDoItem.objects.create(
            group=group, title="Do the thing", sort_order=1
        )
        data = ToDoItemSerializer(item).data
        self.assertEqual(
            set(data.keys()),
            {
                "itemId", "groupId", "categoryId", "parentItemId", "title",
                "notes", "isCompleted", "sortOrder", "tsCreatedAt",
                "tsUpdatedAt", "tsCompletedAt",
            },
        )
        self.assertEqual(data["itemId"], item.item_id)
        self.assertEqual(data["groupId"], group.group_id)
        self.assertEqual(data["title"], "Do the thing")
        self.assertFalse(data["isCompleted"])
        self.assertIsNone(data["categoryId"])
        self.assertIsNone(data["parentItemId"])
        self.assertIsNone(data["tsCompletedAt"])

    def test_group_nested_items(self):
        group = self._group()
        ToDoItem.objects.create(group=group, title="Item A", sort_order=1)
        ToDoItem.objects.create(group=group, title="Item B", sort_order=2)
        data = ToDoGroupSerializer(group).data
        self.assertEqual(
            set(data.keys()),
            {"groupId", "localDate", "isCompleted", "items", "tsCreatedAt", "tsUpdatedAt"},
        )
        self.assertEqual(data["localDate"], "2026-05-31")
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(
            {it["title"] for it in data["items"]}, {"Item A", "Item B"}
        )

    def test_item_deserialization_camelcase_input(self):
        """Writable camelCase source fields map back to model field names."""
        group = self._group()
        s = ToDoItemSerializer(
            data={
                "title": "Created",
                "isCompleted": True,
                "sortOrder": 5,
            }
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["is_completed"], True)
        self.assertEqual(s.validated_data["sort_order"], 5)

    def test_item_missing_title_invalid(self):
        s = ToDoItemSerializer(data={"isCompleted": False})
        self.assertFalse(s.is_valid())
        self.assertIn("title", s.errors)


# ======================================================================
# Note serializers (camelCase + SerializerMethodField avatar)
# ======================================================================
class NoteRoleMemberSerializerTests(BaseAPITestCase):
    def _permission_row(self):
        # NotePermissionMaster: user + note_id + note_type + role_id are the
        # required non-null fields with no default.
        return NotePermissionMaster.objects.create(
            user=self.user,
            team=self.team,
            note_id=1,
            note_type=1,
            role_id=1,
        )

    def test_role_member_camelcase_and_avatar_none(self):
        row = self._permission_row()
        data = NoteRoleMemberSerializer(row).data
        self.assertEqual(
            set(data.keys()),
            {"userId", "userName", "avatarUrl", "roleId", "tsCreated"},
        )
        self.assertEqual(data["userId"], str(self.user.id))
        self.assertEqual(data["userName"], "testuser")
        # User has no profile image file -> avatarUrl is None.
        self.assertIsNone(data["avatarUrl"])


class NoteVersionSerializerTests(BaseAPITestCase):
    def _version_kwargs(self):
        # Required non-null fields without default: note_type, note_id,
        # version_no. editor is nullable but we populate it for the happy path.
        return {
            "editor": self.user,
            "team": self.team,
            "title": "V1 title",
            "body": {"blocks": []},
            "note_type": 1,
            "note_id": 1,
            "version_no": 1,
        }

    def test_list_item_excludes_body(self):
        ver = NoteVersionMaster.objects.create(**self._version_kwargs())
        data = NoteVersionListItemSerializer(ver).data
        self.assertEqual(
            set(data.keys()),
            {
                "versionNo", "editor", "title", "restoredFromVersionNo",
                "tsCreatedAt", "tsUpdatedAt",
            },
        )
        self.assertNotIn("body", data)
        self.assertEqual(data["title"], "V1 title")
        # editor payload is the embedded camelCase dict.
        self.assertEqual(data["editor"]["userId"], str(self.user.id))
        self.assertEqual(data["editor"]["userName"], "testuser")
        self.assertIsNone(data["editor"]["avatarUrl"])

    def test_detail_includes_body(self):
        ver = NoteVersionMaster.objects.create(**self._version_kwargs())
        data = NoteVersionDetailSerializer(ver).data
        self.assertIn("body", data)
        self.assertEqual(data["body"], {"blocks": []})

    def test_editor_payload_none_when_editor_null(self):
        kwargs = self._version_kwargs()
        # version_note editor may be nullable; if so, test the None branch.
        editor_field = NoteVersionMaster._meta.get_field("editor")
        if not editor_field.null:
            self.skipTest("editor is not nullable; None branch not reachable")
        kwargs["editor"] = None
        ver = NoteVersionMaster.objects.create(**kwargs)
        data = NoteVersionListItemSerializer(ver).data
        self.assertIsNone(data["editor"])


# ======================================================================
# __all__ ModelSerializers (snake_case, validation)
# ======================================================================
class AllFieldsModelSerializerTests(BaseAPITestCase):
    def test_task_master_serializer_snake_case_representation(self):
        task = TaskMaster.objects.create(
            team=self.team, title="My task", status="Open"
        )
        data = TaskMasterSerializer(task).data
        # __all__ keeps snake_case model field names.
        self.assertEqual(data["title"], "My task")
        self.assertEqual(data["status"], "Open")
        self.assertIn("task_id", data)
        self.assertIn("ts_created_at", data)
        # camelCase keys must NOT appear.
        self.assertNotIn("taskId", data)

    def test_task_master_missing_required_fields(self):
        s = TaskMasterSerializer(data={})
        self.assertFalse(s.is_valid())
        # title and status are required (no default, not null).
        self.assertIn("title", s.errors)
        self.assertIn("status", s.errors)

    def test_project_master_missing_required_fields(self):
        s = ProjectMasterSerializer(data={})
        self.assertFalse(s.is_valid())
        self.assertIn("project_name", s.errors)

    def test_team_master_serializer_representation_snake_case(self):
        data = TeamMasterSerializer(self.team).data
        self.assertEqual(data["team_name"], "Test Team")
        self.assertEqual(data["team_email"], "team@example.com")
        self.assertIn("team_id", data)

    def test_team_master_missing_required_fields(self):
        s = TeamMasterSerializer(data={})
        self.assertFalse(s.is_valid())
        self.assertIn("team_name", s.errors)
        self.assertIn("team_email", s.errors)

    def test_team_master_invalid_email(self):
        s = TeamMasterSerializer(
            data={"team_name": "X Team", "team_email": "not-an-email"}
        )
        self.assertFalse(s.is_valid())
        self.assertIn("team_email", s.errors)

    def test_team_members_serializer_valid(self):
        # self.user / self.user2 are already members of self.team (created in
        # BaseAPITestCase.setUp), and (team, attendee) is unique — so use a
        # fresh user to exercise the happy path.
        from django.contrib.auth import get_user_model

        fresh = get_user_model().objects.create_user(
            username="fresh", email="fresh@example.com", password="freshpass123"
        )
        s = TeamMembersSerializer(
            data={"team": str(self.team.team_id), "attendee": str(fresh.id)}
        )
        self.assertTrue(s.is_valid(), s.errors)

    def test_team_members_serializer_duplicate_rejected(self):
        """(team, attendee) unique constraint surfaces as a validation error."""
        s = TeamMembersSerializer(
            data={"team": str(self.team.team_id), "attendee": str(self.user.id)}
        )
        self.assertFalse(s.is_valid())
        self.assertIn("non_field_errors", s.errors)
