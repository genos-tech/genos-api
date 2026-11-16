"""An Inbox row that is saved is news to its receiver's open app (MIP-1627).

The Inbox had one live path: a socket event the ACTING browser relays. Rows filed by a cron or by
the API itself (digests, reminders, reports, approved agent writes) have no acting browser, and a
read flag flipped on one device reached the member's others only at their next refocus or reload.
A `post_save` receiver covers every writer that goes through `.create()` / `.save()`, which is all
of them today, and any added later. It says THAT the inbox changed (`realtime_push`), never what is
in it.

`bulk_create` and `QuerySet.update()` send no `post_save`; a caller using one calls
`realtime_push.push_inbox_changed` itself (`InboxMarkReadView` does).
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from origin.models.common.inbox_models import InboxItems


@receiver(post_save, sender=InboxItems)
def _tell_the_receiver_their_inbox_changed(sender, instance, raw=False, **kwargs):
    # `raw` is a fixture load: nobody is listening for it.
    if raw or instance.receiver_id is None:
        return
    from origin.services.realtime_push import push_inbox_changed

    push_inbox_changed(instance.receiver_id, instance.team_id)
