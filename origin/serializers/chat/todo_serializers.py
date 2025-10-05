from rest_framework import serializers
from origin.models.chat.todo_models import ToDoFact


class ToDoFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ToDoFact
        fields = "__all__"
