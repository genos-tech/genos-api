
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('origin', '0211_todocategory_color'),
    ]

    operations = [
        migrations.CreateModel(
            name='MilestoneFolder',
            fields=[
                ('folder_id', models.BigAutoField(primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=100)),
                ('sort_order', models.IntegerField(default=0)),
                ('ts_created_at', models.DateTimeField(auto_now_add=True)),
                ('ts_updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='project_milestone_folders', to='origin.projectmaster')),
                ('team', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='team_milestone_folders', to='origin.teammaster')),
            ],
        ),
        migrations.AddField(
            model_name='milestonemaster',
            name='folder',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='folder_milestones', to='origin.milestonefolder'),
        ),
        migrations.AddConstraint(
            model_name='milestonefolder',
            constraint=models.UniqueConstraint(fields=('project', 'name'), name='uniq_milestone_folder_per_project'),
        ),
    ]
