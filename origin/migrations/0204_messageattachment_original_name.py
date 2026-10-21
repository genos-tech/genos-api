# A message attachment keeps the uploader's filename for display (MIP-726):
# the file itself is now stored under a generated name.
#
# An addition the previous release ignores. `db_default` keeps the empty
# string as the column's database default, so that release's inserts,
# which don't name the column, still succeed while this deploys.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('origin', '0203_pii_encrypt_rows'),
    ]

    operations = [
        migrations.AddField(
            model_name='messageattachment',
            name='original_name',
            field=models.CharField(blank=True, db_default='', default='', max_length=255),
        ),
    ]
