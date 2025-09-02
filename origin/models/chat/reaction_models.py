from django.db import models


from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


class ReactionFact(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        to_field="team_id",
    )
    chat_type = models.IntegerField(blank=False, null=False)
    chat_id = models.IntegerField(blank=False, null=False)
    message_id = models.IntegerField(blank=False, null=False)
    is_thread = models.BooleanField(blank=False, null=False)
    thread_id = models.IntegerField(blank=False, null=False)
    reaction_id = models.IntegerField(blank=False, null=False)
    reaction_emoji = models.CharField(blank=False, null=False)
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)
    uid = models.CharField(primary_key=True, max_length=255, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chat_type", "chat_id", "thread_id", "message_id", "reaction_id"],
                name="unique_reaction",
            )
        ]

    def save(self, *args, **kwargs):
        self.uid = f"{self.chat_type}-{self.chat_id}-{self.thread_id}-{self.message_id}-{self.reaction_id}"
        super().save(*args, **kwargs)
