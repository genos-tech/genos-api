"""Cache invalidation signal handlers.

Wires `post_save` / `post_delete` on the small set of models that back the
read-mostly endpoints cached in `views/`. The cache keys here MUST stay in sync
with the `cache.set(...)` keys in the corresponding views (grep for `cache_key =`
to find them).

Stale-on-write semantics: a write triggers a synchronous `cache.delete(...)` so
the next read repopulates. `IGNORE_EXCEPTIONS=True` in `settings.CACHES` means
a Redis outage downgrades this to a no-op instead of cascading 500s — keys
will just expire on their natural TTL (60s).
"""

from __future__ import annotations

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMembers

# NOTE: the legacy DM/GM/MDM-member cache-invalidation receivers were
# removed with the v3 cutover. They invalidated `dm:ids` / `gm:ids` /
# `mdm:ids` / `mdm:members` keys that no view sets or reads anymore (the
# legacy chat REST that populated them is gone), so they were dead no-ops
# keeping the legacy chat models alive. v3 channel membership lives in
# `ChannelMember`; if a cached v3 surface needs invalidation later, add a
# `ChannelMember` receiver here.


@receiver(post_save, sender=TeamMembers)
@receiver(post_delete, sender=TeamMembers)
def _invalidate_team_members(sender, instance, **kwargs):
    attendee_id = getattr(instance, "attendee_id", None)
    team_id = getattr(instance, "team_id", None)
    if attendee_id is not None:
        cache.delete(f"team:my_teams:{attendee_id}")
        if team_id is not None:
            cache.delete(f"team:member_info:{team_id}:{attendee_id}")


@receiver(post_save, sender=ProjectMembers)
@receiver(post_delete, sender=ProjectMembers)
def _invalidate_project_members(sender, instance, **kwargs):
    team_id = getattr(instance, "team_id", None)
    attendee_id = getattr(instance, "attendee_id", None)
    if team_id is not None and attendee_id is not None:
        cache.delete(f"project:list:{team_id}:{attendee_id}")


@receiver(post_save, sender=CustomUser)
def _invalidate_user_profile(sender, instance, **kwargs):
    """Profile update — wipe every `team:member_info:*:{user_id}` entry.

    Uses django-redis's `delete_pattern` (a SCAN-based op). Falls back to a
    no-op if the cache backend doesn't implement it (e.g. LocMemCache during
    tests). The 60-second natural TTL backstops correctness.
    """
    user_id = getattr(instance, "id", None)
    if user_id is None:
        return
    delete_pattern = getattr(cache, "delete_pattern", None)
    if callable(delete_pattern):
        try:
            delete_pattern(f"team:member_info:*:{user_id}")
        except Exception:
            pass
