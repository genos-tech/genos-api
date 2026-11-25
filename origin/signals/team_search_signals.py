"""A hard-deleted team takes its search chunks with it (MIP-2384).

`purge_team` (the closed-team cron) and the demo teardown already remove a team's chunks by hand.
This is the net under every other way a `TeamMaster` row is deleted for good (a script, the eval
harness, a path written later): once the delete commits, the team's tracked chunks are purged from
OpenSearch and `RagChunk`. Best-effort (`purge_team_chunks` never raises); `sweep_orphans` and
`manage.py purge_deleted_team_chunks` remove whatever this misses, because they read a team with no
row as dead. `QuerySet.delete()` fires `post_delete` per row, so a bulk delete is covered.
"""

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from origin.models.common.team_models import TeamMaster


@receiver(post_delete, sender=TeamMaster, dispatch_uid="purge_search_chunks_of_deleted_team")
def _purge_search_chunks(sender, instance, **kwargs):
    from origin.search_engine import purge

    team_id = instance.pk
    transaction.on_commit(lambda: purge.purge_team_chunks(team_id), robust=True)
