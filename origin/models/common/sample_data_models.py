"""Which rows the sample data made at team creation wrote (MIP-1706).

A team made with the sample-data box ticked gets two starter projects, an example department
(projects, team folders, a project label, a mention group, group chats), and a few to-dos.
Nothing on those rows says they are sample data, and a name such as "Sample dept" proves
nothing: a person can name their own project that. So each seeder writes one row here for every
object it makes, and "remove the sample data" reads this table and nothing else.

A row is a RECORD, not a foreign key: `object_id` is the object's id as text (the ids are
integers and UUIDs), so deleting the object by its own path never has to know about this table,
and a row whose object is already gone is simply stale and is swept by the next removal.

Teams made before this table existed have no rows, so for them nothing is recognised and every
sample item stays removable one at a time, from its own menu.
"""

from __future__ import annotations

from django.db import models

from origin.models.common.team_models import TeamMaster


class SampleDataMarker(models.Model):
    marker_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="sample_data_markers",
        to_field="team_id",
    )
    # Which seeder wrote it (`starter` or `organisation_examples`), for the preview and for
    # telling a later seeder's rows apart.
    seeder = models.CharField(max_length=40)
    # What kind of object (`project`, `task`, `milestone`, `note`, `note_folder`, `channel`,
    # `mention_group`, `project_label`, `todo_item`, `todo_category`) and its id as text.
    object_type = models.CharField(max_length=40)
    object_id = models.CharField(max_length=64)
    # sha256 of the text the seeder wrote (notes, tasks, to-dos), so "the person edited it" is a
    # comparison, not a guess. Empty for objects whose text is not what makes them the person's.
    fingerprint = models.CharField(max_length=64, blank=True, default="")
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "object_type", "object_id"], name="uniq_sample_data_marker"
            )
        ]
