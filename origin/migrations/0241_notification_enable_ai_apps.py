from django.db import migrations, models


def carry_the_old_switch(apps, schema_editor):
    """MIP-1432 had one switch, `category_settings["ai_app"]`. A member who turned it off meant
    "none of it", so they start the new group with all three kinds off."""
    Pref = apps.get_model("origin", "NotificationPreference")
    for pref in Pref.objects.filter(category_settings__ai_app=False).iterator():
        settings = dict(pref.category_settings or {})
        settings.pop("ai_app", None)
        for key in ("ai_app_tasks", "ai_app_notes", "ai_app_todos"):
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
