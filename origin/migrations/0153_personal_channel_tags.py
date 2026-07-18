# Personal (per-user, PRIVATE) tags on GM channels + their channel
# assignments. New tables only — zero-downtime, no backfill. The
# companion (sender, ts_sent_at) Message index that powers the
# "recently responded" default-chip ranking is split into 0154 so the
# heavier index build deploys/reverts independently of these tables.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0152_teamemojimaster_uniq_active_global_emoji_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="PersonalChannelTag",
            fields=[
                ("tag_id", models.BigAutoField(primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=30)),
                ("color", models.CharField(max_length=10)),
                ("text_color", models.CharField(max_length=10)),
                ("is_default_visible", models.BooleanField(default=False)),
                ("sort_order", models.IntegerField(default=0)),
                ("ts_created_at", models.DateTimeField(auto_now_add=True)),
                ("ts_updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="personal_channel_tags",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="PersonalChannelTagAssignment",
            fields=[
                ("assignment_id", models.BigAutoField(primary_key=True, serialize=False)),
                ("ts_created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "channel",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="personal_tag_assignments",
                        to="origin.channel",
                    ),
                ),
                (
                    "tag",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="assignments",
                        to="origin.personalchanneltag",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="personalchanneltag",
            constraint=models.UniqueConstraint(
                fields=("user", "name"), name="uniq_personal_channel_tag"
            ),
        ),
        migrations.AddConstraint(
            model_name="personalchanneltagassignment",
            constraint=models.UniqueConstraint(
                fields=("tag", "channel"), name="uniq_personal_tag_assignment"
            ),
        ),
    ]
