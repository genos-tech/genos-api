from datetime import date

from rest_framework.response import Response
from rest_framework import status
from django.shortcuts import get_object_or_404

from origin.models.chat.todo_models import ToDoFact
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.serializers.chat.reaction_serializers import *
from origin.serializers.chat.todo_serializers import *
from origin.views.utils.request_validators import validate_request_data, validate_request_user


def check_if_completed(todo_content):
    for content in todo_content:
        if content["props"].get("checked", False) == False:
            return False
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
            return Response(serializer.data, status=status.HTTP_201_CREATED)
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
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
