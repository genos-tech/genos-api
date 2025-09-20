from rest_framework import serializers
from origin.models.note.note_models import *


class PersonalNoteMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = PersonalNoteMaster
        fields = "__all__"


class NotePermissionMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotePermissionMaster
        fields = "__all__"
