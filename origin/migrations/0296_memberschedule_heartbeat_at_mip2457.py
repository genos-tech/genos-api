from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("origin", "0295_note_html_revision_server_rendered_mip2417")]

    operations = [
        migrations.AddField(
            model_name="memberschedule",
            name="heartbeat_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
