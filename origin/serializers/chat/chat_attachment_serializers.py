from rest_framework import serializers
from origin.models.chat.chat_attachment_models import ChatAttachmentFact


class ChatAttachmentFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatAttachmentFact
        fields = "__all__"
