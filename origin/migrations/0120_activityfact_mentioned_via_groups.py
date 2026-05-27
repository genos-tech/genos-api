# Phase: per-user "by group" filter for the activity sidebar.
# Add a JSON map { user_id_str: [group_id_int, ...] } to ActivityFact so each
# activity row records which mention-groups led to each user's inclusion.
# Direct @user mentions don't appear in the map; only the group-driven
# fan-out members do. The frontend reads `mentioned_via_groups[myUserId]`
# to filter the feed down to mentions sourced from a specific group.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("origin", "0119_customuser_tier_alter_userfeatureaccess_feature"),
    ]

    operations = [
        migrations.AddField(
            model_name="activityfact",
            name="mentioned_via_groups",
            field=models.JSONField(blank=True, null=True, default=dict),
        ),
    ]
