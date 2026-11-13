from django.db import migrations, models

# One switch per action (see services/ai_app_switches.py). Spelled out here: a migration must
# not change when that table does.
AI_APP_KEYS = (
    "ai_app_task_created",
    "ai_app_task_status",
    "ai_app_task_commented",
    "ai_app_comment_deleted",
    "ai_app_milestone_created",
    "ai_app_milestone_status",
    "ai_app_milestone_deleted",
    "ai_app_note_created",
    "ai_app_note_edited",
    "ai_app_note_deleted",
    "ai_app_todo_created",
    "ai_app_todo_deleted",
)


def carry_the_old_switch(apps, schema_editor):
    """MIP-1432 had one switch, `category_settings["ai_app"]`. A member who turned it off meant
    "none of it", so they start the new group with every switch off."""
    Pref = apps.get_model("origin", "NotificationPreference")
    for pref in Pref.objects.filter(category_settings__ai_app=False).iterator():
        settings = dict(pref.category_settings or {})
        settings.pop("ai_app", None)
        for key in AI_APP_KEYS:
            settings[key] = False
        pref.category_settings = settings
        pref.save(update_fields=["category_settings"])


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0240_activity_ai_app_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationpreference",
            name="enable_ai_apps",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(carry_the_old_switch, migrations.RunPython.noop),
    ]
