"""Force-delete endpoints (MIP-1584). The rules, and what each removes, are in
`origin.services.force_delete`; these views only authenticate, hide what the caller cannot see
(404), require the typed confirmation (400) and map a refusal to its status.

    POST /api/v3/messages/<uuid>/force-delete/
    POST /api/v3/channels/<uuid>/my-messages/force-delete/
    POST /api/v3/tasks/<id>/force-delete/
    POST /api/v3/tasks/<id>/comments/<comment_id>/force-delete/
    POST /api/v3/notes/<personal|task|chat>/<id>/force-delete/

Every body is `{"confirm": "delete-permanently"}`.
"""

from __future__ import annotations

from django.http import Http404
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import Channel, Message
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import force_delete as svc
from origin.services.channel_scope import is_chat_member
from origin.services.note_delete import FAMILIES
from origin.services.share_roles import view_only_task_response
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.note_role import get_effective_role
from origin.views.utils.request_validators import body_object
from origin.views.utils.scope_guards import can_access_task


def _needs_confirmation(request):
    if svc.confirmed(body_object(request)):
        return None
    return Response(
        {"error": "Send the typed confirmation.", "code": "confirmation_required"},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _run(call):
    try:
        report = call()
    except svc.ForceDeleteRefused as exc:
        return Response({"error": exc.message, "code": exc.code, **exc.extra}, status=exc.status)
    return Response(report.as_response(), status=status.HTTP_200_OK)


class MessageForceDeleteView(AuthenticatedAPIView):
    def post(self, request, message_id):
        message = Message.objects.select_related("channel").filter(id=message_id).first()
        if message is None or not is_chat_member(message.channel_id, request.user.id):
            raise Http404("Message not found.")
        if (refused := _needs_confirmation(request)) is not None:
            return refused
        return _run(lambda: svc.force_delete_message(message, actor=request.user, request=request))


class MyMessagesForceDeleteView(AuthenticatedAPIView):
    def post(self, request, channel_id):
        channel = Channel.objects.filter(id=channel_id).first()
        if channel is None or not is_chat_member(channel.id, request.user.id):
            raise Http404("Chat not found.")
        if (refused := _needs_confirmation(request)) is not None:
            return refused
        return _run(
            lambda: svc.force_delete_my_messages(channel, actor=request.user, request=request)
        )


def _visible_task(request, task_id):
    # A task already soft-deleted can still be force-deleted: that is the point.
    if not can_access_task(task_id, request.user.id, include_deleted=True):
        raise Http404("Task not found.")
    task = TaskMaster.objects.filter(task_id=task_id).first()
    if task is None:
        raise Http404("Task not found.")
    return task


class TaskForceDeleteView(AuthenticatedAPIView):
    def post(self, request, task_id):
        task = _visible_task(request, task_id)
        # Removing a whole task changes the project (MIP-2571). Erasing your own message, comment
        # or note stays open.
        if refused := view_only_task_response(task.task_id, request.user.id):
            return refused
        if (refused := _needs_confirmation(request)) is not None:
            return refused
        return _run(lambda: svc.force_delete_task(task, actor=request.user, request=request))


class TaskCommentForceDeleteView(AuthenticatedAPIView):
    def post(self, request, task_id, comment_id):
        _visible_task(request, task_id)
        comment = TaskComments.objects.filter(task_id=task_id, comment_id=comment_id).first()
        if comment is None:
            raise Http404("Comment not found.")
        if (refused := _needs_confirmation(request)) is not None:
            return refused
        return _run(lambda: svc.force_delete_comment(comment, actor=request.user, request=request))


class NoteForceDeleteView(AuthenticatedAPIView):
    def post(self, request, family, note_id):
        if family not in FAMILIES:
            raise Http404("Note not found.")
        model, code = FAMILIES[family]
        note = model.objects.filter(note_id=note_id).first()
        if note is None or get_effective_role(request.user.id, code, note_id) is None:
            raise Http404("Note not found.")
        if (refused := _needs_confirmation(request)) is not None:
            return refused
        return _run(
            lambda: svc.force_delete_note(family, note, actor=request.user, request=request)
        )
