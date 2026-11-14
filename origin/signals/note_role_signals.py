"""A person's membership of a project ends the Owner row they wrote on its task notes (MIP-1493),
and joining it again writes that row back (MIP-1511).

`ProjectMembers` has no `is_deleted`: leaving, being removed, a team departure
(`drop_in_team_access`), a guest's withdrawal and a project delete all HARD-delete the row, and
`QuerySet.delete()` fires `post_delete` per row, so this one receiver covers every path. It is a
receiver of its own rather than part of `pm_channel_signals`' because that one returns early for
a project with no chat channel, which has nothing to do with whether the project has notes.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from origin.models.project.prj_models import ProjectMembers


@receiver(post_delete, sender=ProjectMembers)
def _end_author_roles_on_project_leave(sender, instance, **kwargs):
    from origin.services.note_role_revoke import revoke_task_author_roles

    revoke_task_author_roles(user_id=instance.attendee_id, project_id=instance.project_id)


@receiver(post_save, sender=ProjectMembers)
def _restore_author_roles_on_project_join(sender, instance, created, **kwargs):
    """A (re)joiner gets their Owner row back on the task notes that name them as author, so they
    can share and unshare them again. Only a new membership: re-saving one changes nothing."""
    if not created:
        return
    from origin.services.note_role_revoke import restore_task_author_roles

    restore_task_author_roles(user_id=instance.attendee_id, project_id=instance.project_id)
