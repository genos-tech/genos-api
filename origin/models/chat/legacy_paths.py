"""Legacy chat-attachment `upload_to` path functions.

These were defined on the legacy DM/GM/MDM/PM model modules and are
referenced by historical migrations (0055, 0065, 0079) via `upload_to=`.
Django imports every migration module at load time, so the functions
must keep existing even after the legacy model files are deleted — they
were extracted here so those migrations stay importable. The legacy
chat tables themselves are dropped; these are only ever called when an
old migration is replayed against a fresh database.
"""

import os


def dm_message_attachment_path(instance, filename):
    return os.path.join("chats", "dm", str(instance.dm_id), filename)


def gm_message_attachment_path(instance, filename):
    return os.path.join("chats", "gm", str(instance.gm_id), filename)


def mdm_message_attachment_path(instance, filename):
    return os.path.join("chats", "mdm", str(instance.mdm_id), filename)


def project_message_attachment_path(instance, filename):
    return os.path.join("chats", "project", str(instance.project_id), filename)


def profile_image_path(instance, filename):
    return os.path.join("gm_profiles", str(instance.gm_id), filename)


def chat_attachment_path(instance, filename):
    return os.path.join(
        "chats",
        str(instance.chat_type),
        str(instance.chat_id),
        str(instance.message_id),
        str(instance.thread_id),
        filename,
    )
