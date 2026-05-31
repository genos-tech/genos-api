from origin.models.common.inbox_models import InboxItems
from rest_framework import serializers


class InboxItemsSerializer(serializers.ModelSerializer):
    class Meta:
        model = InboxItems
        fields = "__all__"
