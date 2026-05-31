from django.db import transaction
from django.db.models import Case, CharField, F, IntegerField, Q, Value, When
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.user_models import CustomUser
from origin.serializers.note.note_serializers import *
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.mention_handler import extractMentionedUsers, resolve_group_members
from origin.views.utils.note_role import (
    ROLE_OWNER,
    ROLE_VIEWER,
    delete_note_permissions,
    get_effective_role,
    require_read_role,
    require_write_role,
)
from origin.views.utils.note_version import (
    delete_note_versions,
    snapshot_note_version,
)
from origin.views.utils.request_validators import validate_request_data, validate_request_user
from rest_framework import status
from rest_framework.response import Response

NOTE_TYPE = 3  # Chat Notes


def _chat_membership_filters(team_id, user_id):
    """Build a Q expression matching chat notes the user has implicit access to.

    Chat notes are keyed on the v3 `Channel` UUID (`channel_id`). A user
    can see a note implicitly iff they're an active member of its channel,
    so we match every channel the user belongs to in this team — one query
    covering all four kinds (DM/GM/PM/MDM). PM channels carry a
    `ChannelMember` per `ProjectMembers` row via
    `pm_channel_signals._sync_pm_channel_member`, so they're covered here
    too; the channel UUID is the single source of truth (no defensive
    ProjectMembers UNION).
    """
    member_channel_ids = list(
        ChannelMember.objects.filter(
            user_id=user_id,
            is_deleted=False,
            channel__is_deleted=False,
            channel__team_id=team_id,
        ).values_list("channel_id", flat=True)
    )
    return Q(channel_id__in=member_channel_ids)


def _accessible_chat_note_ids(team_id, user_id):
    """Notes the user can see: chat-member notes + explicitly granted notes."""
    membership_q = _chat_membership_filters(team_id, user_id)
    if membership_q:
        member_note_ids = set(
            ChatNoteMaster.objects.filter(team=team_id)
            .filter(membership_q)
            .values_list("note_id", flat=True)
        )
    else:
        member_note_ids = set()

    explicit_note_ids = set(
        NotePermissionMaster.objects.filter(
            team=team_id, user=user_id, note_type=NOTE_TYPE
        ).values_list("note_id", flat=True)
    )
    return member_note_ids | explicit_note_ids


def _role_map(user_id, note_ids):
    """Map note_id -> role_id from explicit NotePermissionMaster rows."""
    return {
        row["note_id"]: row["role_id"]
        for row in NotePermissionMaster.objects.filter(
            user=user_id, note_type=NOTE_TYPE, note_id__in=list(note_ids)
        ).values("note_id", "role_id")
    }


class AllChatNotesView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        accessible = _accessible_chat_note_ids(data["team_id"], request_user_id)
        role_map = _role_map(request_user_id, accessible)

        notes = list(
            ChatNoteMaster.objects.filter(team=data["team_id"], note_id__in=accessible)
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                chatType=F("chat_type"),
                chatId=F("channel_id"),
                isThread=F("is_thread"),
                threadId=F("thread_root_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("-tsUpdated")
            .values(
                "noteType",
                "teamId",
                "ownerId",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        for n in notes:
            n["roleId"] = role_map.get(n["noteId"], ROLE_VIEWER)

        return Response(notes, status=status.HTTP_200_OK)


class AllChatNoteMetaView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        accessible = _accessible_chat_note_ids(data["team_id"], request_user_id)

        chat_notes = (
            ChatNoteMaster.objects.filter(team=data["team_id"], note_id__in=accessible)
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                chatType=F("chat_type"),
                chatId=F("channel_id"),
                isThread=F("is_thread"),
                threadId=F("thread_root_id"),
                tsUpdated=F("ts_updated_at"),
                chatTypeName=Case(
                    When(chat_type=1, then=Value("DM")),
                    When(chat_type=2, then=Value("GM")),
                    When(chat_type=3, then=Value("PM")),
                    When(chat_type=4, then=Value("DM")),
                    default=Value("Chat"),
                    output_field=CharField(),
                ),
            )
            .order_by("-tsUpdated")
            .values(
                "noteType",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatTypeName",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "tsUpdated",
            )
        )

        # Convert to list and add chat names based on chat type
        notes_list = list(chat_notes)

        # `note["chatId"]` is now the v3 `Channel` UUID for every kind, so
        # all four name maps are keyed directly by channel UUID. No legacy
        # `legacy_chat_id` indirection — the channel id IS the key.
        dm_ids = [n["chatId"] for n in notes_list if n["chatType"] == 1]
        gm_ids = [n["chatId"] for n in notes_list if n["chatType"] == 2]
        pm_ids = [n["chatId"] for n in notes_list if n["chatType"] == 3]
        mdm_ids = [n["chatId"] for n in notes_list if n["chatType"] == 4]

        # Resolve chat display names off the v3 unified schema, keyed by
        # `Channel.id`: DM partner from `ChannelMember`; GM/MDM from
        # `Channel.title`; PM from the channel's `project__project_name`.
        dm_partner_names = {}
        gm_names = {}
        mdm_names = {}

        # GM + MDM display names from Channel.title, keyed by Channel.id.
        if gm_ids or mdm_ids:
            for ch in Channel.objects.filter(
                id__in=(gm_ids + mdm_ids),
                kind__in=[ChannelKind.GM, ChannelKind.MDM],
                is_deleted=False,
            ).values("id", "kind", "title"):
                if ch["kind"] == ChannelKind.GM:
                    gm_names[ch["id"]] = ch["title"]
                else:
                    mdm_names[ch["id"]] = ch["title"]

        # DM partner usernames: the *other* member of each DM channel,
        # keyed by Channel.id.
        if dm_ids:
            members_by_chan: dict = {}
            for m in ChannelMember.objects.filter(
                channel_id__in=dm_ids, is_deleted=False
            ).values("channel_id", "user_id"):
                members_by_chan.setdefault(m["channel_id"], []).append(m["user_id"])

            partner_by_chan = {}
            partner_user_ids = set()
            for chan_id, uids in members_by_chan.items():
                partner = next(
                    (u for u in uids if str(u) != str(request_user_id)),
                    uids[0] if uids else None,
                )
                if partner:
                    partner_by_chan[chan_id] = partner
                    partner_user_ids.add(partner)

            user_id_to_name = {
                str(u["id"]): u["username"]
                for u in CustomUser.objects.filter(id__in=partner_user_ids).values(
                    "id", "username"
                )
            }
            for chan_id, partner_id in partner_by_chan.items():
                dm_partner_names[chan_id] = user_id_to_name.get(
                    str(partner_id), "Direct Message"
                )

        # PM project names resolved THROUGH the PM channel's project FK
        # (pm_ids are channel UUIDs, not project ids).
        project_names = {}
        if pm_ids:
            project_names = dict(
                Channel.objects.filter(
                    id__in=pm_ids, kind=ChannelKind.PM, is_deleted=False
                ).values_list("id", "project__project_name")
            )

        # Add chat names to notes
        for note in notes_list:
            if note["chatType"] == 1:
                note["chatName"] = dm_partner_names.get(note["chatId"], "Direct Message")
            elif note["chatType"] == 2:
                note["chatName"] = gm_names.get(note["chatId"], f"Group {note['chatId']}")
            elif note["chatType"] == 3:
                note["chatName"] = project_names.get(note["chatId"], f"Project {note['chatId']}")
            elif note["chatType"] == 4:
                note["chatName"] = mdm_names.get(note["chatId"], f"DM {note['chatId']}")
            else:
                note["chatName"] = f"Chat {note['chatId']}"

        return Response(notes_list, status=status.HTTP_200_OK)


class ChatNoteMasterView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "owner": request.GET.get("user_id"),
            "chat_type": request.GET.get("chat_type"),
            "chat_id": request.GET.get("chat_id"),
            "is_thread": request.GET.get("is_thread"),
            "thread_id": request.GET.get("thread_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["owner"])):
            return res

        # `chat_id` is the v3 `Channel` UUID; `thread_id` is the thread-root
        # `Message` UUID. The FE sends `is_thread` as the literal string
        # "True"/"False" in the querystring, so coerce it explicitly —
        # Python's `bool("False")` is True. Only filter `thread_root_id`
        # against the UUID on the thread branch; non-thread notes match
        # `thread_root_id IS NULL` (never filter a UUIDField against
        # "0"/"False"/"null").
        is_thread_bool = str(data["is_thread"]).lower() in ("true", "1")

        chat_notes = ChatNoteMaster.objects.filter(
            team=data["team"],
            owner=data["owner"],
            chat_type=data["chat_type"],
            channel_id=data["chat_id"],
            is_thread=is_thread_bool,
        )
        if is_thread_bool:
            chat_notes = chat_notes.filter(thread_root_id=data["thread_id"])
        else:
            chat_notes = chat_notes.filter(thread_root_id__isnull=True)

        chat_notes = (
            chat_notes.annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                chatType=F("chat_type"),
                chatId=F("channel_id"),
                isThread=F("is_thread"),
                threadId=F("thread_root_id"),
                parentNoteId=F("parent_note_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsCreated")  # ASC by created at
            .values(
                "noteType",
                "teamId",
                "ownerId",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        return Response(list(chat_notes), status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        request_user_id = request.user.id

        # `chat_id` (request) is the v3 `Channel` UUID; DRF's
        # PrimaryKeyRelatedField on the `channel` FK accepts the UUID
        # string. `channel` stays in the validated dict because a missing
        # channel would silently save NULL and create an invisible note.
        data = {
            "team": request.data.get("team_id"),
            "owner": request.data.get("user_id"),
            "chat_type": request.data.get("chat_type"),
            "channel": request.data.get("chat_id"),
            "title": request.data.get("title"),
            "body": request.data.get("body"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["owner"])):
            return res

        # Assigned AFTER validate_request_data: `is_thread` coerces to a
        # Python bool and `thread_root_id` is None for non-thread notes —
        # validate_request_data 400s on any None value, so these must not
        # be in the validated dict (same pattern as parent_note_id below).
        is_thread_bool = str(request.data.get("is_thread")).lower() in ("true", "1")
        thread_id_raw = request.data.get("thread_id")
        data["is_thread"] = is_thread_bool
        data["thread_root_id"] = (
            thread_id_raw
            if (
                is_thread_bool
                and str(thread_id_raw).lower()
                not in ("0", "false", "null", "none", "undefined", "")
            )
            else None
        )
        data["parent_note_id"] = request.data.get("parent_note_id")

        serializer = ChatNoteMasterSerializer(data=data)
        if serializer.is_valid():
            try:
                # Wrap the database operations in a transaction
                with transaction.atomic():
                    # First, create the main note
                    # The serializer's save() method will call its .create() method
                    serializer.save()

                    note = {
                        "teamId": serializer.data["team"],
                        "ownerId": serializer.data["owner"],
                        "noteType": NOTE_TYPE,
                        "noteId": serializer.data["note_id"],
                        "parentNoteId": serializer.data["parent_note_id"],
                        "chatType": serializer.data["chat_type"],
                        "chatId": serializer.data["channel"],
                        "isThread": serializer.data["is_thread"],
                        "threadId": serializer.data["thread_root_id"],
                        "title": serializer.data["title"],
                        "body": serializer.data["body"],
                        "tsCreated": serializer.data["ts_created_at"],
                        "tsUpdated": serializer.data["ts_updated_at"],
                    }

                    # Second, create the associated role for that note
                    team_obj = TeamMaster.objects.get(team_id=data["team"])
                    NotePermissionMaster.objects.create(
                        team=team_obj,
                        user=CustomUser.objects.get(id=request_user_id),
                        note_id=note["noteId"],
                        note_type=NOTE_TYPE,
                        role_id=ROLE_OWNER,
                    )

                    # Third, write the initial version snapshot (v1).
                    snapshot_note_version(
                        team=team_obj,
                        editor=request.user,
                        note_type=NOTE_TYPE,
                        note_id=note["noteId"],
                        title=note["title"],
                        body=note["body"],
                    )

            except Exception as e:
                # If anything fails, return a server error
                return Response(
                    {"error": "Failed to create note and role.", "details": str(e)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            # If the transaction is successful, return the created note data
            return Response(note, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        request_user_id = request.user.id

        data = {
            "note_id": request.data.get("note_id"),
            "title": request.data.get("title"),
            "body": request.data.get("body"),
        }

        if res := validate_request_data(data):
            return res

        if res := require_write_role(request_user_id, NOTE_TYPE, data["note_id"]):
            return res

        try:
            note = ChatNoteMaster.objects.get(note_id=data["note_id"])
        except ChatNoteMaster.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        update_data = request.data.copy()

        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        # Same mention-extraction pattern as the other note PUTs.
        newly_mentioned_user_ids = []
        all_mentioned_user_ids = []
        removed_user_ids = []
        if "body" in update_data:
            extract_user_handler = extractMentionedUsers()
            extract_user_handler.extract(update_data["body"])
            full_mentioned = set(extract_user_handler.mentioned_user_ids)
            full_mentioned |= resolve_group_members(extract_user_handler.mentioned_group_ids)
            update_data["mentioned_user_ids"] = list(full_mentioned)

            prev_set = set(note.mentioned_user_ids or [])
            newly_mentioned_user_ids = list(full_mentioned - prev_set)
            removed_user_ids = list(prev_set - full_mentioned)
            all_mentioned_user_ids = list(full_mentioned)

        serializer = ChatNoteMasterSerializer(note, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            try:
                snapshot_note_version(
                    team=note.team,
                    editor=request.user,
                    note_type=NOTE_TYPE,
                    note_id=note.note_id,
                    title=note.title,
                    body=note.body,
                )
            except Exception as e:
                print(f"NoteVersion snapshot failed for chat note {note.note_id}: {e}")
            return Response(
                {
                    **serializer.data,
                    "newly_mentioned_user_ids": newly_mentioned_user_ids,
                    "all_mentioned_user_ids": all_mentioned_user_ids,
                    "removed_user_ids": removed_user_ids,
                },
                status=status.HTTP_200_OK,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "note_id": request.GET.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := require_write_role(request_user_id, NOTE_TYPE, data["note_id"]):
            return res

        try:
            with transaction.atomic():
                note = ChatNoteMaster.objects.get(team=data["team"], note_id=data["note_id"])
                note.delete()
                delete_note_permissions(NOTE_TYPE, data["note_id"])
                delete_note_versions(NOTE_TYPE, data["note_id"])
            return Response(
                {"message": "Note deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except ChatNoteMaster.DoesNotExist:
            return Response(
                {"error": "Note not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class SingleChatNoteView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "note_id": request.GET.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := require_read_role(request_user_id, NOTE_TYPE, data["note_id"], data["team"]):
            return res

        role = get_effective_role(request_user_id, NOTE_TYPE, data["note_id"], data["team"])

        chat_notes = (
            ChatNoteMaster.objects.filter(
                team=data["team"],
                note_id=data["note_id"],
            )
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                roleId=Value(role, output_field=IntegerField()),
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                chatType=F("chat_type"),
                chatId=F("channel_id"),
                isThread=F("is_thread"),
                threadId=F("thread_root_id"),
                parentNoteId=F("parent_note_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .values(
                "noteType",
                "teamId",
                "ownerId",
                "roleId",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        if len(chat_notes) == 0:
            return Response(
                {"error": "Note not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(chat_notes[0], status=status.HTTP_200_OK)


class ChatNoteAttachmentView(AuthenticatedAPIView):
    def post(self, request):
        request_user_id = request.user.id

        # ChatNoteAttachmentFact mirrors the chat note's v3 routing
        # (chat_type / channel UUID / is_thread / thread-root UUID)
        # alongside the FK to the note. The frontend sends these from
        # `currentChatNote` (chat_id = channel UUID, thread_id = thread-
        # root Message UUID); we forward them through here with the SAME
        # coercion as the parent-note create so the attachment row is
        # routing-symmetric with its note. `channel` stays in the
        # validated dict; `is_thread`/`thread_root_id` are assigned after
        # validate_request_data (it 400s on any None value).
        data = {
            "note": request.data.get("note_id"),
            "uploader": request.data.get("uploader"),
            "note_attachment_url": request.FILES.get("note_attachment_file"),
            "chat_type": request.data.get("chat_type"),
            "channel": request.data.get("chat_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["uploader"])):
            return res

        if res := require_write_role(request_user_id, NOTE_TYPE, data["note"]):
            return res

        is_thread_bool = str(request.data.get("is_thread")).lower() in ("true", "1")
        thread_id_raw = request.data.get("thread_id")
        data["is_thread"] = is_thread_bool
        data["thread_root_id"] = (
            thread_id_raw
            if (
                is_thread_bool
                and str(thread_id_raw).lower()
                not in ("0", "false", "null", "none", "undefined", "")
            )
            else None
        )

        serializer = ChatNoteAttachmentFactSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            res = {
                "noteType": NOTE_TYPE,
                "noteId": serializer.data["note"],
                "uploader": serializer.data["uploader"],
                "attachmentId": serializer.data["attachment_id"],
                "noteAttachmentUrl": serializer.data["note_attachment_url"],
                "tsCreated": serializer.data["ts_created_at"],
                "tsUpdated": serializer.data["ts_updated_at"],
            }
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ChatSubNotesView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "owner": request.GET.get("user_id"),
            "note_id": request.GET.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["owner"])):
            return res

        # Gate by access to the parent note.
        parent_role = get_effective_role(request_user_id, NOTE_TYPE, data["note_id"], data["team"])
        if parent_role is None:
            return Response(
                {"error": "You do not have access to this note."},
                status=status.HTTP_403_FORBIDDEN,
            )

        sub_notes = (
            ChatNoteMaster.objects.filter(
                team=data["team"],
                parent_note_id=data["note_id"],
            )
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                chatType=F("chat_type"),
                chatId=F("channel_id"),
                isThread=F("is_thread"),
                threadId=F("thread_root_id"),
                parentNoteId=F("parent_note_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .values(
                "noteType",
                "teamId",
                "ownerId",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        return Response(list(sub_notes), status=status.HTTP_200_OK)
