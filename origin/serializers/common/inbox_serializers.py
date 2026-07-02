from rest_framework import serializers

from origin.models.common.inbox_models import InboxItems


class InboxItemsSerializer(serializers.ModelSerializer):
    class Meta:
        model = InboxItems
        fields = "__all__"
