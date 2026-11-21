"""A hard-deleted chat takes its cached thread summaries with it (MIP-1802).

`ThreadSummary` keys its chat by a bare uuid, so nothing cascades to it from `Channel`. The group
chat delete and the project delete remove the summaries by hand, in their own transaction; this is
the net under every other way a `Channel` row is deleted for good (the demo teardown, the eval
harness, a script, a path written later). `QuerySet.delete()` fires `post_delete` per row, so the
bulk `Channel.objects.filter(...).delete()` is covered.

The summaries go inside the delete's transaction. Their search chunks are not purged here: every
caller that cares (`channel_delete`, `project_delete`) purges the chat's chunks after its commit, and
`sweep_orphans` reads a chunk of a deleted chat as dead.
"""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from origin.models.chat.unified_models import Channel
from origin.services import thread_summary_cleanup


@receiver(post_delete, sender=Channel, dispatch_uid="forget_thread_summaries_of_deleted_channel")
def _forget_thread_summaries(sender, instance, **kwargs):
    thread_summary_cleanup.forget_channels([instance.pk])
