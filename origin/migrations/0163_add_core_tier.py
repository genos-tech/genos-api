"""Add the `core` subscription tier to CustomUser.tier / TeamMaster.plan.

Choices-only change — no column alteration, no data migration. Existing
rows keep their values; `core` simply becomes assignable.

Ladder is now free < core < pro < max < enterprise
(`origin.search_engine.quota._TIER_RANK`). `core` is the cheapest
self-serve paid plan; see genos-docs operations/SUBSCRIPTION_TIERS.md.
"""

from django.db import migrations, models

_TIER_CHOICES = [
    ("free", "Free"),
    ("core", "Core"),
    ("pro", "Pro"),
    ("max", "Max"),
    ("enterprise", "Enterprise"),
]


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0162_taskmaster_collaborators"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customuser",
            name="tier",
            field=models.CharField(
                choices=_TIER_CHOICES, db_index=True, default="free", max_length=16
            ),
        ),
        migrations.AlterField(
            model_name="teammaster",
            name="plan",
            field=models.CharField(
                choices=_TIER_CHOICES, db_index=True, default="free", max_length=16
            ),
        ),
    ]
