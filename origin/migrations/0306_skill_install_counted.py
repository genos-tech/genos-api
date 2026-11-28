from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("origin", "0305_message_also_in_channel_mip2486")]

    operations = [
        migrations.AddField(
            model_name="skillinstall",
            name="counted",
            field=models.BooleanField(db_default=True, default=True),
        ),
    ]
