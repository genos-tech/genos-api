from django.db import migrations


class Migration(migrations.Migration):
    """Project names stop being unique per team (MIP-1935): a refused name would confirm that a
    Hidden project uses it. The views refuse a duplicate only among projects the caller can see."""

    dependencies = [
        ("origin", "0263_is_hidden"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="projectmaster",
            name="project_name_unique_per_team",
        ),
    ]
