from django.db import models


from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


class ToDoFact(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    todo_id = models.BigAutoField(primary_key=True, unique=True)
    todo_content = models.JSONField(blank=False, null=False)
    is_completed = models.BooleanField(default=False)
    dt_created_on = models.DateField(auto_now_add=True, blank=False, null=False)
    ts_created_at = models.DateTimeField(auto_now_add=True, blank=False, null=False)
    ts_updated_at = models.DateTimeField(auto_now=True, blank=False, null=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "todo_id"], name="unique_todo")]
