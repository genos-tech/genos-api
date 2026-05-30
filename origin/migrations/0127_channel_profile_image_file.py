# Adds `Channel.profile_image_file` so v3 channel avatars can be
# uploaded via `ChannelProfileImageView` (PUT /api/v3/channels/{id}/
# profile/image/). The existing `profile_image_url` CharField keeps its
# meaning as the public URL fragment; the FileField is the binary
# write target. After save, `ChannelProfileImageView` sets
# `profile_image_url` from the resolved storage path so FE callers only
# need to read one field.
#
# Additive-only. Safe to roll back by reverting this migration alone —
# no data movement; existing rows simply default to NULL.

from django.db import migrations, models

import origin.models.chat.unified_models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0126_channel_legacy_chat_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="channel",
            name="profile_image_file",
            field=models.FileField(
                blank=True,
                null=True,
                upload_to=origin.models.chat.unified_models._channel_profile_image_path,
            ),
        ),
    ]
