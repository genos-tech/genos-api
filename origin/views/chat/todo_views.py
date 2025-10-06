from datetime import date, timedelta

from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404
from django.db.models import F
from django.utils import timezone

from origin.models.chat.todo_models import ToDoFact
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.serializers.chat.reaction_serializers import *
from origin.serializers.chat.todo_serializers import *
from origin.views.utils.request_validators import validate_request_data, validate_request_user

# Get to-do items of the last <LAST_N_DAYS> days
LAST_N_DAYS = 30


def check_if_completed(todo_content):
    for content in todo_content:
        if content["type"] == "checkListItem":
            if content["content"] and content["props"].get("checked", False) == False:
                return False
        # Check if the content has children
        if len(content["children"]) > 0:
            return check_if_completed(content["children"])
    return True


class ToDoFactView(AuthenticatedAPIView):
    def post(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "user": request.data.get("user_id"),
            "todo_content": request.data.get("todo_content"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user"])):
            return res

        data["is_completed"] = check_if_completed(data["todo_content"])

        # Check if a ToDoFact for today already exists for this user and team
        today = date.today()
        exists = ToDoFact.objects.filter(
            team=data["team"], user=data["user"], dt_created_on=today
        ).exists()

        if exists:
            return Response(
                {"detail": "A ToDoFact for today already exists."}, status=status.HTTP_200_OK
            )

        serializer = ToDoFactSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            res = {
                "todoId": serializer.data["todo_id"],
                "todoContent": serializer.data["todo_content"],
                "isCompleted": serializer.data["is_completed"],
                "dtCreatedOn": serializer.data["dt_created_on"],
                "tsCreatedAt": serializer.data["ts_created_at"],
                "tsUpdatedAt": serializer.data["ts_updated_at"],
            }
            return Response(res, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "user": request.data.get("user_id"),
            "todo_id": request.data.get("todo_id"),
            "todo_content": request.data.get("todo_content"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user"])):
            return res

        todo = get_object_or_404(
            ToDoFact, team=data["team"], user=data["user"], todo_id=data["todo_id"]
        )

        update_data = request.data.copy()
        update_data["is_completed"] = check_if_completed(data["todo_content"])

        serializer = ToDoFactSerializer(todo, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            res = {
                "todoId": serializer.data["todo_id"],
                "todoContent": serializer.data["todo_content"],
                "isCompleted": serializer.data["is_completed"],
                "dtCreatedOn": serializer.data["dt_created_on"],
                "tsCreatedAt": serializer.data["ts_created_at"],
                "tsUpdatedAt": serializer.data["ts_updated_at"],
            }
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        data = {
            "team": request.GET.get("team_id"),
            "user": request.GET.get("user_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user"])):
            return res

        n_days_ago = timezone.now() - timedelta(days=LAST_N_DAYS)

        todos = ToDoFact.objects.filter(
            team=data["team"], user=data["user"], ts_created_at__gte=n_days_ago
        )
        todos = (
            todos.annotate(
                todoId=F("todo_id"),
                todoContent=F("todo_content"),
                isCompleted=F("is_completed"),
                dtCreatedOn=F("dt_created_on"),
                tsCreatedAt=F("ts_created_at"),
                tsUpdatedAt=F("ts_updated_at"),
            )
            .order_by("ts_created_at")
            .reverse()
            .values(
                "todoId",
                "todoContent",
                "isCompleted",
                "dtCreatedOn",
                "tsCreatedAt",
                "tsUpdatedAt",
            )
        )
        return Response(todos, status=status.HTTP_200_OK)
