"""REST endpoints for the Slack export import (MIP-1836). Logic: `services/slack_import*.py`.

POST /api/v2/team/import/slack/preview/             multipart: file, team_id: writes nothing
POST /api/v2/team/import/slack/                     the same + optional channel_ids (JSON list): imports
GET  /api/v2/team/import/slack/runs/?team_id=       the team's import runs
GET  /api/v2/team/import/slack/runs/<run_id>/undo/?team_id=   what an undo would remove and keep
POST /api/v2/team/import/slack/runs/<run_id>/undo/  {team_id}: removes it

Owner only (the import writes messages under other members' names). A non-member and an unknown
team get the same 404; a member who is not the owner gets 403. The preview is stateless: the file
is sent again at commit. Guard order: team (404) -> owner (403) -> one import at a time (409,
commit only) -> file present and small (400/413) -> safe, within caps (400/413).
"""

from __future__ import annotations

import hashlib
import json
import uuid

from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from origin.models.common.team_models import TeamMaster
from origin.services import slack_import
from origin.services import slack_import_zip as z
from origin.services.member_roles import OWNER, resolve_team_role
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import is_team_member
from origin.views.utils.upload_limits import check_upload_size

LOCK_SECONDS = 600


def _refuse(code, http_status, **extra):
    return Response({"error": code, "code": code, **extra}, status=http_status)


def _team_for_import(request, team_id):
    """`(team, None)` for the team's owner, else `(None, Response)`."""
    if not team_id:
        return None, _refuse("team_id_required", status.HTTP_400_BAD_REQUEST)
    try:
        team = TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()
    except (DjangoValidationError, ValueError):
        team = None
    if team is None or not is_team_member(team.team_id, request.user.id):
        return None, _refuse("team_not_found", status.HTTP_404_NOT_FOUND)
    if resolve_team_role(team, request.user.id) != OWNER:
        return None, _refuse("forbidden", status.HTTP_403_FORBIDDEN)
    return team, None


def _limits():
    return {
        "max_zip_mb": z.MAX_ZIP_BYTES // (1024 * 1024),
        "max_channels": z.MAX_CHANNELS,
        "max_messages": z.MAX_MESSAGES,
    }


def _prepare(request, team):
    upload = request.FILES.get("file")
    if upload is None:
        return None, _refuse("file_missing", status.HTTP_400_BAD_REQUEST)
    if upload.size > z.MAX_ZIP_BYTES:
        return None, _refuse(
            "file_too_large",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            limit_mb=z.MAX_ZIP_BYTES // (1024 * 1024),
        )
    if res := check_upload_size(request.user, upload, fallback_bytes=z.MAX_ZIP_BYTES):
        return None, res
    sha = hashlib.sha256()
    for chunk in upload.chunks():
        sha.update(chunk)
    upload.seek(0)
    channel_ids = None
    raw = request.data.get("channel_ids")
    if raw not in (None, ""):
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            value = None
        if not isinstance(value, list):
            return None, _refuse("bad_channel_ids", status.HTTP_400_BAD_REQUEST)
        channel_ids = [str(v) for v in value]
    try:
        export = z.read_export(upload)
        resolved = slack_import.resolve(team, export, channel_ids=channel_ids)
    except z.SlackImportError as exc:
        return None, _refuse(exc.code, exc.status, **exc.extra)
    return {"upload": upload, "sha": sha.hexdigest(), "export": export, "resolved": resolved}, None


class SlackImportPreviewView(AuthenticatedAPIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        team, res = _team_for_import(request, request.data.get("team_id"))
        if res:
            return res
        prepared, res = _prepare(request, team)
        if res:
            return res
        return Response(
            slack_import.public_preview(prepared["resolved"], prepared["sha"], _limits())
        )


class SlackImportView(AuthenticatedAPIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        team, res = _team_for_import(request, request.data.get("team_id"))
        if res:
            return res
        lock = f"slack-import:{team.team_id}"
        if not cache.add(lock, 1, LOCK_SECONDS):
            return _refuse("import_in_progress", status.HTTP_409_CONFLICT)
        try:
            prepared, res = _prepare(request, team)
            if res:
                return res
            out = slack_import.commit(
                team=team,
                user=request.user,
                export=prepared["export"],
                resolved=prepared["resolved"],
                filename=getattr(prepared["upload"], "name", ""),
                file_sha256=prepared["sha"],
                request=request,
            )
        finally:
            cache.delete(lock)
        return Response(out, status=status.HTTP_201_CREATED)


class SlackImportRunsView(AuthenticatedAPIView):
    def get(self, request):
        team, res = _team_for_import(request, request.query_params.get("team_id"))
        if res:
            return res
        return Response({"runs": slack_import.runs(team)})


class SlackImportUndoView(AuthenticatedAPIView):
    def _go(self, request, team_id, run_id, fn):
        team, res = _team_for_import(request, team_id)
        if res:
            return res
        try:
            return Response(fn(team, run_id))
        except z.SlackImportError as exc:
            return _refuse(exc.code, exc.status, **exc.extra)

    def get(self, request, run_id: uuid.UUID):
        return self._go(
            request, request.query_params.get("team_id"), run_id, slack_import.undo_plan
        )

    def post(self, request, run_id: uuid.UUID):
        return self._go(
            request,
            request.data.get("team_id"),
            run_id,
            lambda team, rid: slack_import.undo(team, rid, request=request),
        )
