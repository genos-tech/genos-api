from django.db import migrations

# The twelve per-action switches that existed before "note commented" (MIP-1669). Spelled out: a
# migration must not change when `services/ai_app_switches.py` does.
EARLIER_KEYS = (
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
NEW_KEY = "ai_app_note_commented"


def carry_the_opt_out(apps, schema_editor):
    """A member who switched EVERY earlier AI-app notice off meant "none of it" (0241 even turned them
    all off for anyone who had switched the original single switch off). A switch added after that
    has no stored value for them and would start on, so it starts off for exactly them. Anyone who
    left even one on keeps the default for the new one (on), and a stored value is never touched."""
    Pref = apps.get_model("origin", "NotificationPreference")
    candidates = Pref.objects.filter(category_settings__ai_app_task_created=False)
    for pref in candidates.iterator():
        settings = dict(pref.category_settings or {})
        if NEW_KEY in settings:
            continue
        if all(settings.get(key) is False for key in EARLIER_KEYS):
            settings[NEW_KEY] = False
            pref.category_settings = settings
            pref.save(update_fields=["category_settings"])


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0247_inbox_activities_read_backfill"),
    ]

    operations = [
        migrations.RunPython(carry_the_opt_out, migrations.RunPython.noop),
    ]
