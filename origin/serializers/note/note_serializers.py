from rest_framework import serializers
from origin.models.note.common_note_models import *
from origin.models.note.personal_note_models import *
from origin.models.note.task_note_models import *
from origin.models.note.chat_note_models import *


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


class PersonalNoteAttachmentViewSerializer(serializers.ModelSerializer):
    class Meta:
        model = PersonalNoteAttachmentFact
        fields = "__all__"


class TaskNoteAttachmentViewSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskNoteAttachmentFact
        fields = "__all__"


class ChatNoteAttachmentViewSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatNoteAttachmentFact
        fields = "__all__"


class NotePermissionMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotePermissionMaster
        fields = "__all__"
