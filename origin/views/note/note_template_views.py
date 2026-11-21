"""Team note templates (MIP-1697): list, read, create, update, delete, and set a folder default.

ACL: any team member lists, reads and creates; the creator (or the team owner, for a departed
creator) updates and deletes; setting a folder default also takes Editor on that folder. A caller
outside the team, or a bad team id, gets 404 (never 403), so a guessed id confirms nothing.
"""

import json

from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.note.note_template_models import NoteTemplate
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.services import change_feed
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.note_folder_role import get_folder_role, is_team_member
from origin.views.utils.note_role import ROLE_EDITOR
from origin.views.utils.request_validators import as_int, as_uuid

MAX_NAME = 100
MAX_TITLE = 255
MAX_TEMPLATE_BODY_BYTES = 256 * 1024


def _nf():
    return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)


def _bad(msg):
    return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)


def template_dict(t: NoteTemplate, *, with_body: bool, names=None) -> dict:
    out = {
        "templateId": t.template_id,
        "teamId": str(t.team_id) if t.team_id else None,
        "name": t.name,
        "title": t.title,
        "defaultFolderId": t.default_for_folder_id,
        "createdBy": str(t.created_by_id) if t.created_by_id else None,
        "createdByName": (names or {}).get(str(t.created_by_id)),
        "tsCreated": t.ts_created_at,
        "tsUpdated": t.ts_updated_at,
    }
    if with_body:
        out["body"] = t.body
    return out


def _member_team(request, team_id):
    """The team id as a UUID string if the caller is in it, else None (the caller answers 404)."""
    uid = as_uuid(team_id)
    if uid is None or not is_team_member(uid, request.user.id):
        return None
    return str(uid)


def _can_modify(t: NoteTemplate, team_id, user_id) -> bool:
    if str(t.created_by_id) == str(user_id):
        return True
    return TeamMaster.objects.filter(team_id=team_id, owner=user_id).exists()


def _get(template_id, team_id):
    tid = as_int(template_id)
    if tid is None:
        return None
    return NoteTemplate.objects.filter(template_id=tid, team=team_id).first()


def _validate_fields(data, *, partial):
    """Returns (clean dict, error Response)."""
    clean = {}
    if "name" in data or not partial:
        name = str(data.get("name") or "").strip()
        if not name or len(name) > MAX_NAME:
            return None, _bad(f"Name must be 1-{MAX_NAME} characters.")
        clean["name"] = name
    if "title" in data:
        title = data.get("title")
        title = "" if title is None else str(title)
        if len(title) > MAX_TITLE:
            return None, _bad(f"Title must be at most {MAX_TITLE} characters.")
        clean["title"] = title
    if "body" in data:
        body = data.get("body")
        if not isinstance(body, list):
            return None, _bad("Body must be a list of blocks.")
        if len(json.dumps(body)) > MAX_TEMPLATE_BODY_BYTES:
            return None, _bad("Template is too large.")
        clean["body"] = body
    return clean, None


def _resolve_default_folder(request, team_id, folder_id):
    """(folder, error Response). One answer for missing, foreign, personal-scope folders."""
    fid = as_int(folder_id)
    folder = (
        PersonalNoteFolder.objects.filter(
            folder_id=fid, team=team_id, scope=PersonalNoteFolder.SCOPE_TEAM
        ).first()
        if fid is not None
        else None
    )
    role = get_folder_role(request.user.id, fid, team_id) if folder else None
    if folder is None or role is None:
        return None, _bad("Invalid folder.")
    if role > ROLE_EDITOR:
        return None, Response(
            {"error": "You cannot edit this folder."}, status=status.HTTP_403_FORBIDDEN
        )
    return folder, None


def _apply_default(t, folder):
    """Make `t` the folder's default, taking it from any other template. Caller is atomic."""
    NoteTemplate.objects.filter(default_for_folder=folder).exclude(pk=t.pk).update(
        default_for_folder=None
    )
    t.default_for_folder = folder


class NoteTemplateView(AuthenticatedAPIView):
    def get(self, request):
        team_id = _member_team(request, request.GET.get("team_id"))
        if team_id is None:
            return _nf()
        if request.GET.get("template_id") not in (None, ""):
            t = _get(request.GET.get("template_id"), team_id)
            if t is None:
                return _nf()
            return Response(template_dict(t, with_body=True))
        rows = list(
            NoteTemplate.objects.filter(team=team_id).defer("body").order_by("name", "template_id")
        )
        names = {
            str(r["id"]): r["username"]
            for r in CustomUser.objects.filter(
                id__in=[t.created_by_id for t in rows if t.created_by_id]
            ).values("id", "username")
        }
        return Response([template_dict(t, with_body=False, names=names) for t in rows])

    def post(self, request):
        team_id = _member_team(request, request.data.get("team_id"))
        if team_id is None:
            return _nf()
        clean, err = _validate_fields(request.data, partial=False)
        if err:
            return err
        folder = None
        if request.data.get("default_folder_id") is not None:
            folder, err = _resolve_default_folder(
                request, team_id, request.data["default_folder_id"]
            )
            if err:
                return err
        try:
            with transaction.atomic():
                t = NoteTemplate(team_id=team_id, created_by_id=request.user.id, **clean)
                if folder is not None:
                    _apply_default(t, folder)
                t.save()
        except IntegrityError:
            return Response({"error": "Conflict."}, status=status.HTTP_409_CONFLICT)
        change_feed.bump_notes(team_id, personal=True)
        return Response(template_dict(t, with_body=True), status=status.HTTP_201_CREATED)

    def put(self, request):
        team_id = _member_team(request, request.data.get("team_id"))
        if team_id is None:
            return _nf()
        t = _get(request.data.get("template_id"), team_id)
        if t is None:
            return _nf()
        if not _can_modify(t, team_id, request.user.id):
            return Response(
                {"error": "Only the creator or the team owner can change this template."},
                status=status.HTTP_403_FORBIDDEN,
            )
        clean, err = _validate_fields(request.data, partial=True)
        if err:
            return err
        for k, v in clean.items():
            setattr(t, k, v)
        folder = None
        if "default_folder_id" in request.data:
            if request.data["default_folder_id"] is None:
                t.default_for_folder = None
            elif as_int(request.data["default_folder_id"]) != t.default_for_folder_id:
                # Only a CHANGE needs Editor on the folder: renaming a template whose default
                # folder you can no longer edit must still work.
                folder, err = _resolve_default_folder(
                    request, team_id, request.data["default_folder_id"]
                )
                if err:
                    return err
        try:
            with transaction.atomic():
                if folder is not None:
                    _apply_default(t, folder)
                t.save()
        except IntegrityError:
            return Response({"error": "Conflict."}, status=status.HTTP_409_CONFLICT)
        change_feed.bump_notes(team_id, personal=True)
        return Response(template_dict(t, with_body=True))

    def delete(self, request):
        data = request.data if request.data else request.GET
        team_id = _member_team(request, data.get("team_id"))
        if team_id is None:
            return _nf()
        t = _get(data.get("template_id"), team_id)
        if t is None:
            return _nf()
        if not _can_modify(t, team_id, request.user.id):
            return Response(
                {"error": "Only the creator or the team owner can delete this template."},
                status=status.HTTP_403_FORBIDDEN,
            )
        t.delete()
        change_feed.bump_notes(team_id, personal=True)
        return Response(status=status.HTTP_204_NO_CONTENT)
