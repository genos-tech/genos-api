# Generated manually 2026-05-29 — bump MessageAttachment.file max_length.
#
# Default Django `max_length=100` is too short for our path layout
# `chats/<channel-uuid>/messages/<message-uuid>/<filename>` (~89 chars
# of fixed prefix), causing `SuspiciousFileOperation` when Django tries
# to find an available filename on collision. Bumping to 500 leaves
# room for long user-supplied filenames + the random uniqueness suffix.

from django.db import migrations, models

import origin.models.chat.unified_models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0124_message_thread_root_cascade"),
    ]

    operations = [
        migrations.AlterField(
            model_name="messageattachment",
            name="file",
            field=models.FileField(
                max_length=500,
                upload_to=origin.models.chat.unified_models._message_attachment_path,
            ),
        ),
    ]
