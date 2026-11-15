from django.db import migrations, models


def refuse_if_repeats(apps, schema_editor):
    """Stop, with the way out in the message, if a person's emoji repeats on a comment (MIP-1595).

    The constraint below cannot be created over such rows (Postgres would refuse it with a bare
    "could not create unique index"), so say what to do instead: nothing here deletes data.
    `manage.py dedupe_task_comment_reactions` (dry run by default) counts them and `--apply`
    removes the later copies; then migrate again. Rows with no sender (the account was deleted)
    are never a repeat of each other, as in the constraint's condition. Counts only in the message.
    """
    Reaction = apps.get_model("origin", "TaskCommentReactionFact")
    groups = (
        Reaction.objects.exclude(sender__isnull=True)
        .values("task_id", "comment_id", "sender_id", "reaction_emoji")
        .annotate(copies=models.Count("pk"))
        .filter(copies__gt=1)
        .count()
    )
    if groups:
        raise RuntimeError(
            f"{groups} (comment, person, emoji) group(s) of task comment reactions repeat, so the "
            "unique index cannot be built. Run `python manage.py dedupe_task_comment_reactions` "
            "(dry run), then with `--apply`, and migrate again."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0245_task_note_role_revoke"),
    ]

    operations = [
        migrations.RunPython(refuse_if_repeats, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="taskcommentreactionfact",
            constraint=models.UniqueConstraint(
                condition=models.Q(("sender__isnull", False)),
                fields=("task", "comment_id", "sender", "reaction_emoji"),
                name="unique_task_comment_reaction_per_sender_emoji",
            ),
        ),
    ]
