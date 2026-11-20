"""MIP-1799: drop the three legacy chat fact tables nothing reads or writes.

`ActivityFact` (0037) held, per activity, the first line of the message it was about
(`first_line_content`) and the chat name; `ReactionFact` and `MentionFact` held who reacted
with what and who was mentioned, keyed by the old composite chat ids. The v3 schema replaced
them (`Activity`, `MessageReaction`, `MessageMention`: 0129/0132) and their writers were
removed, so no code reads or writes any of the three; only the demo seeder's cleanup touched
them. Production read-only count on 2026-11-21: 581 / 12 / 705 rows, newest written 173+ days
ago, and the account-deletion and retention services never reached them. Counsel's principle is
not to keep personal data that is not necessary (MIP-1780), and a table nothing reads is the
clearest case.

Nothing is copied. Reversing recreates the three tables EMPTY: the dropped rows are not
restored by a rollback. Database backups taken before this migration still hold them until
their own retention ends.

`ReadStatus` (the fourth legacy table in the task) was already dropped in 0132.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0254_core_trial_notices"),
    ]

    operations = [
        migrations.DeleteModel(name="ActivityFact"),
        migrations.DeleteModel(name="ReactionFact"),
        migrations.DeleteModel(name="MentionFact"),
    ]
