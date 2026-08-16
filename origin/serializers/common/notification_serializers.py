from rest_framework import serializers

from origin.models.common.notification_models import (
    NotificationPreference,
    PushSubscription,
)

# Recognised per-object mute target types. Kept here (not imported from
# the frontend) so the backend can reject obviously malformed entries
# without coupling to the FE category registry.
_MUTED_TARGET_TYPES = {"chat", "thread", "task", "note"}


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
            "push_enabled",
            "email_enabled",
            "category_settings",
            "muted_chats",
            "muted_targets",
            "snooze_until",
            "snooze_schedule",
            "ts_updated_at",
        ]
        read_only_fields = ["ts_updated_at"]

    def validate_category_settings(self, value):
        # Free-form `{fine_category_key: bool}` map. Keys are intentionally
        # NOT validated against a fixed allowlist so a newer client can add
        # a category key without 400ing against an older backend; only the
        # shape (str -> bool) is enforced.
        if not isinstance(value, dict):
            raise serializers.ValidationError("category_settings must be an object.")
        normalized = {}
        for key, enabled in value.items():
            if not isinstance(key, str) or not key:
                raise serializers.ValidationError(
                    "category_settings keys must be non-empty strings."
                )
            if not isinstance(enabled, bool):
                raise serializers.ValidationError("category_settings values must be booleans.")
            normalized[key] = enabled
        return normalized

    def validate_snooze_schedule(self, value):
        # Recurring daily quiet window, or None to clear it. Shape:
        # `{"enabled": bool, "start": "HH:MM", "end": "HH:MM"}`. The times are
        # local wall-clock (see `notification_snooze.resolve_snooze_zone`); an
        # overnight window (start > end) is valid and intended. `start == end`
        # is accepted here but read as "never paused" by the predicate.
        if value is None:
            return None
        if not isinstance(value, dict):
            raise serializers.ValidationError("snooze_schedule must be an object or null.")

        enabled = value.get("enabled")
        if not isinstance(enabled, bool):
            raise serializers.ValidationError("snooze_schedule.enabled must be a boolean.")

        def _valid_hhmm(v):
            if not isinstance(v, str):
                return False
            parts = v.split(":")
            if len(parts) != 2:
                return False
            try:
                hours, minutes = int(parts[0]), int(parts[1])
            except (TypeError, ValueError):
                return False
            return 0 <= hours <= 23 and 0 <= minutes <= 59

        start = value.get("start")
        end = value.get("end")
        if not _valid_hhmm(start) or not _valid_hhmm(end):
            raise serializers.ValidationError(
                "snooze_schedule.start and .end must be 'HH:MM' strings (00:00–23:59)."
            )

        # Normalize to just the recognized keys so stray fields don't persist.
        return {"enabled": enabled, "start": start, "end": end}

    def validate_muted_targets(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("muted_targets must be a list.")

        normalized = []
        seen = set()
        for entry in value:
            if not isinstance(entry, dict):
                raise serializers.ValidationError("Each muted_targets entry must be an object.")

            target_type = entry.get("target_type")
            target_id = entry.get("target_id")
            chat_type = entry.get("chat_type")
            note_type = entry.get("note_type")
            categories = entry.get("categories")
            label = entry.get("label")

            if target_type not in _MUTED_TARGET_TYPES:
                raise serializers.ValidationError(
                    "muted_targets[].target_type must be one of " f"{sorted(_MUTED_TARGET_TYPES)}."
                )
            if not isinstance(target_id, str) or not target_id:
                raise serializers.ValidationError(
                    "muted_targets[].target_id must be a non-empty string."
                )
            # bool is a subclass of int — exclude it explicitly.
            if chat_type is not None and (
                not isinstance(chat_type, int) or isinstance(chat_type, bool)
            ):
                raise serializers.ValidationError(
                    "muted_targets[].chat_type must be an integer when provided."
                )
            # Which of the three note tables `target_id` belongs to (1
            # personal / 2 task / 3 chat). Required for a `note` entry to
            # mute anything server-side — the three tables have independent
            # primary keys, so a bare note id is ambiguous. Optional in the
            # shape so entries stored before this field existed still
            # validate; see `webpush_gating._NOTE_TYPE_BY_SURFACE`.
            if note_type is not None and (
                not isinstance(note_type, int) or isinstance(note_type, bool)
            ):
                raise serializers.ValidationError(
                    "muted_targets[].note_type must be an integer when provided."
                )
            if categories is not None:
                if not isinstance(categories, list) or not all(
                    isinstance(c, str) and c for c in categories
                ):
                    raise serializers.ValidationError(
                        "muted_targets[].categories must be a list of non-empty strings."
                    )
            if label is not None and not isinstance(label, str):
                raise serializers.ValidationError(
                    "muted_targets[].label must be a string when provided."
                )

            key = (target_type, target_id)
            if key in seen:
                continue
            seen.add(key)
            item = {"target_type": target_type, "target_id": target_id}
            if chat_type is not None:
                item["chat_type"] = chat_type
            if note_type is not None:
                item["note_type"] = note_type
            if categories:
                item["categories"] = list(categories)
            if label:
                item["label"] = label
            normalized.append(item)

        return normalized

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


class PushSubscriptionSerializer(serializers.ModelSerializer):
    """Validates an incoming Web Push subscription.

    The client flattens the browser `PushSubscription` (`{endpoint,
    keys:{p256dh, auth}}`) into `{endpoint, p256dh, auth, user_agent?}`.
    `user` is taken from `request.user` in the view, never the payload.
    """

    class Meta:
        model = PushSubscription
        fields = ["endpoint", "p256dh", "auth", "user_agent", "device_id"]
        # `endpoint` is `unique=True` on the model, so DRF auto-attaches a
        # UniqueValidator — which would 400 every re-registration of the same
        # browser (the FE re-POSTs on each mount). The view deliberately
        # upserts via `update_or_create`, so drop the auto validators here.
        # The `validate_endpoint` method below still runs (method-level
        # validators are independent of the field's `validators` list).
        extra_kwargs = {"endpoint": {"validators": []}}

    def validate_endpoint(self, value):
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            raise serializers.ValidationError("endpoint must be an http(s) URL.")
        return value

    def validate_p256dh(self, value):
        if not isinstance(value, str) or not value:
            raise serializers.ValidationError("p256dh is required.")
        return value

    def validate_auth(self, value):
        if not isinstance(value, str) or not value:
            raise serializers.ValidationError("auth is required.")
        return value
