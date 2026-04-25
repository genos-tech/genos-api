from rest_framework import serializers
from origin.models.chat.mdm_models import MDMMaster, MDMMembers, MDMMessages, MDMThreadMessages


class MDMMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = MDMMaster
        fields = "__all__"


class MDMMembersSerializer(serializers.ModelSerializer):
    class Meta:
        model = MDMMembers
        fields = "__all__"


class MDMMessagesSerializer(serializers.ModelSerializer):
    class Meta:
        model = MDMMessages
        fields = "__all__"


class MDMThreadMessagesSerializer(serializers.ModelSerializer):
    class Meta:
        model = MDMThreadMessages
        fields = "__all__"
