from rest_framework import serializers

from origin.models.common.inbox_models import InboxItems
from origin.services.inbox_via import without_via


class InboxItemsSerializer(serializers.ModelSerializer):
    class Meta:
        model = InboxItems
        fields = "__all__"

    def validate_item_optionals(self, value):
        """A client's `item_optionals` never carries a `via` (MIP-1569): that key says which
        connected app made the item, and only the server's own producers may say it. Every body a
        client sends reaches the row through this serializer."""
        return without_via(value)
