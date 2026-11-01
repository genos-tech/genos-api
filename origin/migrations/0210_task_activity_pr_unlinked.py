# MIP-834: PR_UNLINKED, the history row for a removed delivery link.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0209_github_unregistered_delivery"),
    ]

    operations = [
        migrations.AlterField(
            model_name="taskactivity",
            name="action_type",
            field=models.CharField(
                choices=[
                    ("created", "Created"),
                    ("title_changed", "Title changed"),
                    ("status_changed", "Status changed"),
                    ("priority_changed", "Priority changed"),
                    ("effort_changed", "Effort changed"),
                    ("assignee_changed", "Assignee changed"),
                    ("reporter_changed", "Reporter changed"),
                    ("due_date_changed", "Due date changed"),
                    ("start_date_changed", "Start date changed"),
                    ("description_edited", "Description edited"),
                    ("tags_changed", "Tags changed"),
                    ("parent_changed", "Parent changed"),
                    ("milestone_changed", "Milestone changed"),
                    ("sprint_changed", "Sprint changed"),
                    ("project_changed", "Project changed"),
                    ("custom_field_changed", "Custom field changed"),
                    ("closed", "Closed"),
                    ("reopened", "Reopened"),
                    ("deleted", "Deleted"),
                    ("attachment_added", "Attachment added"),
                    ("attachment_removed", "Attachment removed"),
                    ("comment_added", "Comment added"),
                    ("comment_edited", "Comment edited"),
                    ("comment_deleted", "Comment deleted"),
                    ("milestone_assignee_added", "Milestone assignee added"),
                    ("milestone_assignee_removed", "Milestone assignee removed"),
                    ("pr_comment_added", "PR comment added"),
                    ("pr_linked", "PR linked"),
                    ("pr_unlinked", "PR unlinked"),
                ],
                max_length=48,
            ),
        ),
    ]
