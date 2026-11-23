from rest_framework import serializers

from origin.models.common.inbox_models import InboxItems
from origin.serializers.chat.unified_serializers import UserLiteSerializer
from origin.services.inbox_via import without_via


class InboxSenderSerializer(UserLiteSerializer):
    """The person an inbox card is about, as the avatar needs them: id, name, avatar path.

    `UserLiteSerializer` is the shape every other payload uses for a person, so the client's
    avatar directory reads this one unchanged. The email and the system flag are dropped: a card
    needs neither, and the sender of a join request may be in no team of the receiver's."""

    userEmail = None
    isSystemUser = None


def inbox_sender(item: InboxItems) -> dict | None:
    """`{userId, userName, avatarImgPath}` for the person who sent `item`, else None.

    None for a row nobody sent (the digest, a report, a trial notice: `sender` is NULL), for an
    account that no longer exists, and for a system account (the card then shows the type's own
    mark, as it does for Genos). Read `item.sender` from a queryset that did
    `select_related("sender")`: this makes no query of its own. A deleted person arrives already
    anonymised ("Deleted user", no avatar), which is what the rest of the product shows too."""
    sender = item.sender
    if sender is None or sender.is_system_user:
        return None
    return dict(InboxSenderSerializer(sender).data)


class InboxItemsSerializer(serializers.ModelSerializer):
    class Meta:
        model = InboxItems
        fields = "__all__"

    def validate_item_optionals(self, value):
        """A client's `item_optionals` never carries a `via` (MIP-1569): that key says which
        connected app made the item, and only the server's own producers may say it. Every body a
        client sends reaches the row through this serializer."""
        return without_via(value)
