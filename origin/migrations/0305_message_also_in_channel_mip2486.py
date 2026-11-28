from django.db import migrations, models


class Migration(migrations.Migration):
    """`Message.also_in_channel`: a thread reply also shown in the main chat (MIP-2486).

    A constant default, so Postgres adds the column without rewriting the table.
    """

    dependencies = [
        ("origin", "0304_message_pin_mip2487"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="also_in_channel",
            field=models.BooleanField(default=False),
        ),
    ]
