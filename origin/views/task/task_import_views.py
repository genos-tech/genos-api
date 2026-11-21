"""REST endpoints for the CSV task import (MIP-1835). The logic is `services/task_import.py`.

POST /api/v2/task/import/preview/        multipart: file, project_id, [mapping, status_map, priority_map]
POST /api/v2/task/import/                the same + create_tags: creates the tasks
GET  /api/v2/task/import/runs/?project_id=   the project's import runs
GET  /api/v2/task/import/runs/<run_id>/undo/ what an undo would remove and keep
POST /api/v2/task/import/runs/<run_id>/undo/ removes it

The preview is stateless: the file is sent again at commit, so nothing of it is kept between the
two calls. Guard order (the first that fails answers, so nothing is charged or leaked): the project
is the caller's (404 as for a missing one) -> the caller may write there (403: a viewer or a
guest) -> not archived (409) -> the file is small enough (413) -> readable and not too long (400)
-> the monthly task quota has room for the tasks (429, commit only).
"""

import hashlib
import json

from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_import_models import TaskImportRun
from origin.search_engine.quota import TASK_CREATE_KEY
from origin.services import task_import
from origin.services.archive import archived_project_response
from origin.services.member_roles import EDITOR, OWNER, resolve_project_role
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.quota_guards import check_monthly_creation_quota
from origin.views.utils.scope_guards import is_project_member
from origin.views.utils.upload_limits import check_upload_size


def _refuse(code, http_status, **extra):
    return Response({"error": code, "code": code, **extra}, status=http_status)


def _project_for_write(request, project_id):
    """(project, None) when the caller may import into it, else (None, Response)."""
    project = None
    if project_id not in (None, "") and is_project_member(
        project_id, request.user.id, live_only=True
    ):
        project = ProjectMaster.objects.filter(project_id=project_id).first()
    if project is None:
        return None, _refuse("project_not_found", status.HTTP_404_NOT_FOUND)
    if resolve_project_role(project, request.user.id) not in (OWNER, EDITOR):
        return None, _refuse("forbidden", status.HTTP_403_FORBIDDEN)
    if res := archived_project_response(project.project_id):
        return None, res
    return project, None


def _json_field(request, name):
    raw = request.data.get(name)
    if raw in (None, ""):
        return None
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return None


def _lower_keys(value):
    if not isinstance(value, dict):
        return None
    return {str(k).strip().lower(): v for k, v in value.items()}


def _prepare(request, project):
    """The parsed, resolved file, or a Response that refuses it."""
    upload = request.FILES.get("file")
    if upload is None:
        return None, _refuse("file_missing", status.HTTP_400_BAD_REQUEST)
    if upload.size > task_import.MAX_BYTES:
        return None, _refuse(
            "file_too_large",
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            limit_mb=task_import.MAX_BYTES // (1024 * 1024),
        )
    if res := check_upload_size(request.user, upload, fallback_bytes=task_import.MAX_BYTES):
        return None, res
    data = upload.read()
    try:
        headers, rows = task_import.parse(task_import.decode(data))
    except task_import.ImportError_ as exc:
        return None, _refuse(exc.code, exc.status)
    if len(rows) > task_import.MAX_ROWS:
        return None, _refuse(
            "too_many_rows", status.HTTP_400_BAD_REQUEST, limit=task_import.MAX_ROWS
        )
    profile = task_import.detect_profile(headers)
    mapping = task_import.propose_mapping(headers, profile)
    sent = _json_field(request, "mapping")
    if sent is not None:
        mapping = task_import.clean_mapping(sent, len(headers))
    resolved = task_import.resolve_rows(
        project,
        profile,
        rows,
        mapping,
        status_map=_lower_keys(_json_field(request, "status_map")),
        priority_map=_lower_keys(_json_field(request, "priority_map")),
    )
    return {
        "upload": upload,
        "sha": hashlib.sha256(data).hexdigest(),
        "headers": headers,
        "profile": profile,
        "mapping": mapping,
        "resolved": resolved,
    }, None


class TaskImportPreviewView(AuthenticatedAPIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        project, res = _project_for_write(request, request.data.get("project_id"))
        if res:
            return res
        prepared, res = _prepare(request, project)
        if res:
            return res
        resolved = prepared["resolved"]
        return Response(
            {
                "profile": prepared["profile"],
                "headers": prepared["headers"],
                "mapping": prepared["mapping"],
                "file_sha256": prepared["sha"],
                "counts": task_import.counts(resolved),
                "new_tags": resolved["new_tags"],
                "status_values": resolved["status_values"],
                "priority_values": resolved["priority_values"],
                "limits": {"max_rows": task_import.MAX_ROWS},
                "rows": [
                    task_import.public_row(r) for r in resolved["rows"][: task_import.PREVIEW_ROWS]
                ],
            }
        )


class TaskImportView(AuthenticatedAPIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        project, res = _project_for_write(request, request.data.get("project_id"))
        if res:
            return res
        prepared, res = _prepare(request, project)
        if res:
            return res
        if "title" not in prepared["mapping"]:
            return _refuse("title_not_mapped", status.HTTP_400_BAD_REQUEST)
        resolved = prepared["resolved"]
        to_create = task_import.counts(resolved)["to_create"]
        if to_create and (
            res := check_monthly_creation_quota(
                request.user.id, TASK_CREATE_KEY, "task_create", n=to_create
            )
        ):
            return res
        run = task_import.commit(
            project=project,
            user=request.user,
            profile=prepared["profile"],
            filename=getattr(prepared["upload"], "name", "") or "",
            file_sha256=prepared["sha"],
            mapping=prepared["mapping"],
            resolved=resolved,
            create_tags=str(request.data.get("create_tags", "")).lower() in ("1", "true", "yes"),
        )
        return Response(_run_payload(run, with_report=True), status=status.HTTP_201_CREATED)


def _run_payload(run, *, with_report=False):
    payload = {
        "run_id": str(run.run_id),
        "project_id": run.project_id,
        "profile": run.profile,
        "filename": run.filename,
        "created": run.created_count,
        "skipped": run.skipped_count,
        "failed": run.failed_count,
        "ts_created_at": run.ts_created_at,
        "undone_at": run.undone_at,
    }
    if with_report:
        payload["report"] = run.report
    return payload


class TaskImportRunsView(AuthenticatedAPIView):
    def get(self, request):
        project_id = request.query_params.get("project_id")
        if not is_project_member(project_id, request.user.id, live_only=True):
            return _refuse("project_not_found", status.HTTP_404_NOT_FOUND)
        runs = TaskImportRun.objects.filter(project_id=project_id)[:50]
        return Response([_run_payload(r) for r in runs])


class TaskImportUndoView(AuthenticatedAPIView):
    def _run(self, request, run_id):
        try:
            run = TaskImportRun.objects.filter(run_id=run_id).first()
        except (ValueError, TypeError):
            run = None
        if run is None:
            return None, _refuse("run_not_found", status.HTTP_404_NOT_FOUND)
        project, res = _project_for_write(request, run.project_id)
        if res:
            return None, res
        return run, None

    def get(self, request, run_id):
        run, res = self._run(request, run_id)
        if res:
            return res
        plan = task_import.undo_plan(run)
        return Response(
            {
                "run_id": str(run.run_id),
                "will_remove": sum(1 for m in plan["remove"] if m.task.status != "Deleted"),
                "kept": len(plan["kept"]),
            }
        )

    def post(self, request, run_id):
        run, res = self._run(request, run_id)
        if res:
            return res
        return Response(task_import.undo(run))
