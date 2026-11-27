from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0294_opsalert_opensearch_capacity_kind_mip2385"),
    ]

    operations = [
        migrations.AddField(
            model_name="notehtmlrevision",
            name="server_rendered",
            field=models.BooleanField(db_default=False, default=False),
        ),
    ]
