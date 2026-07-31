# Convert per-note grants on TEAM-folder notes into folder grants.
#
# The inbox access-request approval used to write a per-note
# `NotePermissionMaster` row for every note kind. For a note in a team
# folder that shape is invisible to the whole Team Notes UI: the folder
# members dialog, the sidebar, and the header all resolve access from
# `NoteFolderPermission`. The requester could open the note, but nothing
# else knew they had it.
#
# Forward: every non-owner note_type=1 grant whose note sits in a team
# folder becomes a folder grant (same role, get-or-create so an existing
# folder role is never downgraded), and the per-note row is removed so
# the note stops surfacing under the grantee's Shared Notes bucket.
#
# Owner rows are left alone — they're what `_note_and_owner` uses to
# route access requests to the note's creator.
#
# Irreversible by design: reversing would destroy folder grants made
# legitimately through the members dialog since.

from django.db import migrations

ROLE_OWNER = 1


def forward(apps, schema_editor):
    NotePermissionMaster = apps.get_model("origin", "NotePermissionMaster")
    NoteFolderPermission = apps.get_model("origin", "NoteFolderPermission")
    PersonalNoteFolder = apps.get_model("origin", "PersonalNoteFolder")
    PersonalNoteMaster = apps.get_model("origin", "PersonalNoteMaster")

    team_folder_ids = set(
        PersonalNoteFolder.objects.filter(scope="team").values_list("folder_id", flat=True)
    )
    if not team_folder_ids:
        return

    note_to_folder = dict(
        PersonalNoteMaster.objects.filter(folder_id__in=team_folder_ids).values_list(
            "note_id", "folder_id"
        )
    )
    if not note_to_folder:
        return

    stale = NotePermissionMaster.objects.filter(
        note_type=1, note_id__in=list(note_to_folder.keys())
    ).exclude(role_id=ROLE_OWNER)

    for row in stale.iterator():
        NoteFolderPermission.objects.get_or_create(
            folder_id=note_to_folder[row.note_id],
            user_id=row.user_id,
            defaults={
                "team_id": row.team_id,
                "role_id": row.role_id,
            },
        )
    stale.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0171_note_folder_tag_text_color"),
    ]

    operations = [
        migrations.RunPython(forward, migrations.RunPython.noop),
    ]
