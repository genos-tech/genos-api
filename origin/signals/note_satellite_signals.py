"""A note row's removal ends the rows that point at it by number (MIP-1517).

The permission, version, summary, favorite and recent rows of a note carry `(note_type,
note_id)` and no foreign key, so a project, task or account delete that takes the note row with
it left them behind (the version rows holding the note's body). `QuerySet.delete()` and the
`on_delete=CASCADE` paths both go through Django's collector, which sends `post_delete` per
row, so this one receiver covers every path. See `services/note_satellites.py`.

Only a HARD delete of the note row fires `post_delete`. The note tables have no soft delete,
so there is no history of a note that still exists to lose: an edit, a restore
(`restore_note_version`) or a soft-deleted team/channel/account never reaches this receiver.
"""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.views.utils.note_role import NOTE_TYPE_CHAT, NOTE_TYPE_PERSONAL, NOTE_TYPE_TASK

_CODE = {
    PersonalNoteMaster: NOTE_TYPE_PERSONAL,
    TaskNoteMaster: NOTE_TYPE_TASK,
    ChatNoteMaster: NOTE_TYPE_CHAT,
}


@receiver(post_delete, sender=PersonalNoteMaster)
@receiver(post_delete, sender=TaskNoteMaster)
@receiver(post_delete, sender=ChatNoteMaster)
def _forget_note_satellites(sender, instance, **kwargs):
    from origin.services.note_satellites import forget_note_satellites

    forget_note_satellites(_CODE[sender], [instance.pk])
