"""Widen `preferred_llm_provider`'s help_text to mention `openai`.

Metadata-only — `help_text` is not a schema attribute, so this is a
no-op against the database. It exists because Django tracks help_text
in migration state and `makemigrations --check` (CI's migration-drift
gate) fails without it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0163_add_core_tier"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customuser",
            name="preferred_llm_provider",
            field=models.CharField(
                blank=True,
                default="",
                help_text="'gemini', 'claude', 'openai', or '' to use the server default.",
                max_length=32,
            ),
        ),
    ]
