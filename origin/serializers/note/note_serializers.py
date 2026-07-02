from rest_framework import serializers

from origin.models.note.chat_note_models import *
from origin.models.note.common_note_models import *
from origin.models.note.favorite_note_models import *
from origin.models.note.personal_note_models import *
from origin.models.note.recent_note_models import *
from origin.models.note.task_note_models import *
from origin.models.note.version_note_models import NoteVersionMaster


class PersonalNoteMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = PersonalNoteMaster
        fields = "__all__"


class TaskNoteMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskNoteMaster
        fields = "__all__"


class ChatNoteMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatNoteMaster
        fields = "__all__"


class PersonalNoteAttachmentFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = PersonalNoteAttachmentFact
        fields = "__all__"


class TaskNoteAttachmentFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskNoteAttachmentFact
        fields = "__all__"


class ChatNoteAttachmentFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatNoteAttachmentFact
        fields = "__all__"


class NotePermissionMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotePermissionMaster
        fields = "__all__"


class NoteRoleMemberSerializer(serializers.ModelSerializer):
    userId = serializers.UUIDField(source="user.id", read_only=True)
    userName = serializers.CharField(source="user.username", read_only=True)
    avatarUrl = serializers.SerializerMethodField()
    roleId = serializers.IntegerField(source="role_id", read_only=True)
    tsCreated = serializers.DateTimeField(source="ts_created_at", read_only=True)

    class Meta:
        model = NotePermissionMaster
        fields = ["userId", "userName", "avatarUrl", "roleId", "tsCreated"]

    def get_avatarUrl(self, obj):
        if obj.user and obj.user.profile_image_url:
            try:
                return obj.user.profile_image_url.url
            except ValueError:
                return None
        return None


def _editor_payload(user):
    """Shared editor dict for the version serializers."""
    if user is None:
        return None
    avatar_url = None
    if user.profile_image_url:
        try:
            avatar_url = user.profile_image_url.url
        except ValueError:
            avatar_url = None
    return {
        "userId": str(user.id),
        "userName": user.username,
        "avatarUrl": avatar_url,
    }


class NoteVersionListItemSerializer(serializers.ModelSerializer):
    """Lightweight shape for the history list — no body."""

    versionNo = serializers.IntegerField(source="version_no", read_only=True)
    editor = serializers.SerializerMethodField()
    restoredFromVersionNo = serializers.IntegerField(
        source="restored_from_version_no", read_only=True
    )
    tsCreatedAt = serializers.DateTimeField(source="ts_created_at", read_only=True)
    tsUpdatedAt = serializers.DateTimeField(source="ts_updated_at", read_only=True)

    class Meta:
        model = NoteVersionMaster
        fields = [
            "versionNo",
            "editor",
            "title",
            "restoredFromVersionNo",
            "tsCreatedAt",
            "tsUpdatedAt",
        ]

    def get_editor(self, obj):
        return _editor_payload(obj.editor)


class NoteVersionDetailSerializer(NoteVersionListItemSerializer):
    """Same as the list shape plus the full `body`."""

    class Meta(NoteVersionListItemSerializer.Meta):
        fields = NoteVersionListItemSerializer.Meta.fields + ["body"]


class NoteFavoriteMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = NoteFavoriteMaster
        fields = "__all__"


class NoteRecentMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = NoteRecentMaster
        fields = "__all__"
