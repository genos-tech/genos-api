from django.db import models

from origin.models.common.user_models import CustomUser


class NoteHtmlRevision(models.Model):
    """One saved version of an HTML note's page (MIP-2214, plan "HTML notes").

    An HTML note is an ordinary note row (`body_format="html"`) whose `body` is a short readable
    placeholder; the page itself lives HERE, keyed `(note_type, note_id, rev)` like the permission
    and version rows, with no foreign key to the note (`note_type` 1 personal, 2 task, 3 chat). The
    note's ACL is the note's: nothing here decides who may read a page.

    Rows die with their note (`services/note_satellites.forget_note_satellites`). There is no team
    column on purpose: a team's removal deletes its notes, which takes these rows with them, and a
    SET_NULL team key is what once broke demo teardown for the version table.
    """

    note_type = models.IntegerField()
    note_id = models.BigIntegerField()
    # Monotonic per (note_type, note_id), starts at 1.
    rev = models.IntegerField()
    html = models.TextField()
    # Hex SHA-256 of the UTF-8 bytes of `html`: a save of the identical page is recognised.
    sha256 = models.CharField(max_length=64)
    # Bytes of the UTF-8 page.
    size = models.IntegerField()
    # What the scan found, `[{"code", "message", "fix"}]`: kept so a later view can show it.
    warnings = models.JSONField(blank=True, default=list)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["note_type", "note_id", "rev"], name="unique_note_html_revision"
            ),
        ]
        indexes = [
            models.Index(fields=["note_type", "note_id", "-rev"], name="notehtmlrev_lookup_idx"),
        ]
