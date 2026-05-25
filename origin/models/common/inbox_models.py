from django.db import models

from origin.models.common.user_models import CustomUser
from origin.models.common.team_models import TeamMaster


class InboxItems(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="sender_inboxes",
        to_field="id",
    )
    receiver = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="receiver_inboxes",
        to_field="id",
    )
    item_id = models.AutoField(primary_key=True)
    item_body = models.JSONField(blank=True, null=True)
    #########################################################
    # item_type = {
    #    0: "Activity message",
    #    1: "join team request",
    #    2: "join project request",
    #    3: "join gm request"
    # }
    #########################################################
    item_type = models.IntegerField(blank=False)
    item_optionals = models.JSONField(blank=True, null=True)
    is_read = models.BooleanField(default=False)
    #########################################################
    # request_status: only relevant for request items (item_type 1-3)
    #   "pending"  = waiting for action
    #   "approved" = approved by owner
    #   "rejected" = rejected by owner
    #########################################################
    request_status = models.CharField(max_length=10, default="pending", blank=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)
