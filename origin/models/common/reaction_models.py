from django.db import models

from origin.models.common.user_models import CustomUser


class ReactionFact(models.Model):
    chat_type = models.IntegerField(blank=False, null=False)
    chat_id = models.IntegerField(blank=False, null=False)
    message_id = models.IntegerField(blank=False, null=False)
    is_thread = models.BooleanField(blank=False, null=False)
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
                fields=["chat_type", "chat_id", "message_id", "is_thread", "reaction_id"],
                name="unique_reaction",
            )
        ]

    def save(self, *args, **kwargs):
        self.uid = f"{self.chat_type}-{self.chat_id}-{self.message_id}-{1 if self.is_thread else 0}-{self.reaction_id}"
        super().save(*args, **kwargs)
