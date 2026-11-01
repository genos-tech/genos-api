# Merges the two 0210 leaves (operator_security_actions from MIP-865 and
# task_activity_pr_unlinked from MIP-834) that landed on main in the same batch.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('origin', '0210_operator_security_actions'),
        ('origin', '0212_milestone_folder'),
    ]

    operations = [
    ]
