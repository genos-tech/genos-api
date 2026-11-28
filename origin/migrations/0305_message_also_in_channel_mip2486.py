from django.db import migrations, models


class Migration(migrations.Migration):
    """`Message.also_in_channel`: a thread reply also shown in the main chat (MIP-2486).

    A constant default, so Postgres adds the column without rewriting the table.
    """

    dependencies = [
        ("origin", "0303_board_columns_per_status_mip2495"),
    ]

    operations = [
        migrations.AddField(
            model_name="message",
            name="also_in_channel",
            field=models.BooleanField(default=False),
        ),
    ]
