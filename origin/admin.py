from django.contrib import admin
from django.utils import timezone

from origin.models.common.feature_models import UserFeatureAccess


@admin.register(UserFeatureAccess)
class UserFeatureAccessAdmin(admin.ModelAdmin):
    list_display = ("user", "feature", "is_active", "granted_at", "revoked_at", "note")
    list_filter = ("feature", "is_active")
    search_fields = ("user__email", "user__username", "note")
    readonly_fields = ("granted_at", "revoked_at")
    ordering = ("-granted_at",)

    actions = ["revoke_access", "restore_access"]

    @admin.action(description="Revoke selected access grants")
    def revoke_access(self, request, queryset):
        updated = queryset.filter(is_active=True).update(
            is_active=False,
            revoked_at=timezone.now(),
        )
        self.message_user(request, f"{updated} grant(s) revoked.")

    @admin.action(description="Restore selected access grants")
    def restore_access(self, request, queryset):
        updated = queryset.filter(is_active=False).update(
            is_active=True,
            revoked_at=None,
        )
        self.message_user(request, f"{updated} grant(s) restored.")
