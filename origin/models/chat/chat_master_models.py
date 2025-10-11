from django.db import models


from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.task.task_models import *


# This model is for user's chat level master.
# Not chat_type(DM, GM, PM) or message or thread level master.
class UserChatMaster(models.Model):
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
    # The list of flagged messages.
    # {
    #     "chat_type": <1: DM, 2: GM, 3: PM>,
    #     "chat_id": <chat_id>,
    #     "thread_id": <thread_id>,
    #     "message_id": <message_id>,
    # }
    flagged_messages = models.JSONField(blank=True, null=True)
    # The list of pinned chats.
    # {
    #     "chat_type": <1: DM, 2: GM, 3: PM>,
    #     "chat_id": <chat_id>,
    # }
    pinned_chats = models.JSONField(blank=True, null=True)
    ts_last_all_read_activity = models.DateTimeField(blank=True, null=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)
