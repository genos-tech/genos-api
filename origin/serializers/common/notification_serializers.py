from origin.models.common.notification_models import NotificationPreference
from rest_framework import serializers


class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        fields = [
            "master_enabled",
            "enable_chats",
            "enable_thread_replies",
            "enable_mentions",
            "enable_task_comments",
            "enable_inbox",
            "muted_chats",
            "ts_updated_at",
        ]
        read_only_fields = ["ts_updated_at"]

    def validate_muted_chats(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("muted_chats must be a list.")

        normalized = []
        seen = set()
        for entry in value:
            if not isinstance(entry, dict):
                raise serializers.ValidationError("Each muted_chats entry must be an object.")

            chat_type = entry.get("chat_type")
            chat_id = entry.get("chat_id")
            chat_name = entry.get("chat_name")

            if not isinstance(chat_type, int):
                raise serializers.ValidationError("muted_chats[].chat_type must be an integer.")
            if not isinstance(chat_id, str) or not chat_id:
                raise serializers.ValidationError(
                    "muted_chats[].chat_id must be a non-empty string."
                )
            # chat_name is optional display metadata used by the settings
            # panel. Accept None / missing; reject non-string values.
            if chat_name is not None and not isinstance(chat_name, str):
                raise serializers.ValidationError(
                    "muted_chats[].chat_name must be a string when provided."
                )

            key = (chat_type, chat_id)
            if key in seen:
                continue
            seen.add(key)
            item = {"chat_type": chat_type, "chat_id": chat_id}
            if chat_name:
                item["chat_name"] = chat_name
            normalized.append(item)

        return normalized
