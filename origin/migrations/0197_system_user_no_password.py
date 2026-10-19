# Project system users never sign in, but every one made in the app has a
# password anyone can compute. The client signs them up as
# `<team id>-<project name>@genos.tech` with the password
# `<project name>-Bad-Password-Need-Secure-One`, and the account is
# pre-verified and a team member. Knowing a team id and a project name
# was a sign-in to that team.
#
# New system users get no usable password (`UserCreateSerializer`). This
# disables the existing ones:
#
# - An unusable password. That also kills every token already issued to
#   them: `CHECK_REVOKE_TOKEN` puts a hash of the password in each JWT,
#   and both `JWTAuthentication` and the refresh view reject a token
#   whose hash no longer matches. No blacklist rows are needed.
# - Any pending password-reset token is cleared, since a reset is the
#   one way to set a usable password again.
# - Their API keys are revoked. Nothing should have made one, but a key
#   doesn't carry the password hash, so the password change alone would
#   leave it working.
#
# One-way on purpose: the old passwords are the vulnerability.

from django.contrib.auth.hashers import make_password
from django.db import migrations
from django.utils import timezone


def disable_system_user_credentials(apps, schema_editor):
    CustomUser = apps.get_model("origin", "CustomUser")
    ApiKey = apps.get_model("origin", "ApiKey")

    system_users = CustomUser.objects.filter(is_system_user=True)
    # One unusable hash per row, not one shared value. `make_password(None)`
    # returns "!" plus random characters, which no password checks
    # against, and a distinct value per user keeps the token hash claim
    # from matching across accounts.
    for user in system_users.only("pk").iterator():
        CustomUser.objects.filter(pk=user.pk).update(
            password=make_password(None),
            password_reset_token_hash=None,
            password_reset_token_expires_at=None,
        )
    now = timezone.now()
    ApiKey.objects.filter(user__is_system_user=True, revoked_at__isnull=True).update(
        revoked_at=now, ts_updated_at=now
    )


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0196_api_key_allow_destructive"),
    ]

    operations = [
        migrations.RunPython(disable_system_user_credentials, migrations.RunPython.noop),
    ]
