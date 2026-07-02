from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.task.task_models import *


class ActivityFact(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    activity_id = models.CharField(primary_key=True)
    activity_type = models.IntegerField(blank=False, null=False)
    chat_type = models.IntegerField(blank=False, null=False)
    chat_id = models.IntegerField(blank=False, null=False)
    chat_name = models.CharField(blank=True, null=True)
    dm_partner_user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="dm_partners_reactions",
        to_field="id",
    )
    is_thread = models.BooleanField(blank=False, null=False)
    thread_id = models.IntegerField(blank=False, null=False)
    message_id = models.IntegerField(blank=False, null=False)
    message_unique_key = models.CharField(blank=False, null=False)
    thread_message_unique_key = models.CharField(blank=True, null=True)
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="task_id",
        blank=True,
    )
    project = models.ForeignKey(
        ProjectMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="project_id",
    )
    first_line_content = models.CharField(blank=False, null=False)
    # Sender is the user who sent the message, not the user who did the corresponding activity.
    # E.g., "message-A" is sent by user-A. user-B reacted with an emoji to the message-A.
    #       In this case, the "sender" is user-A because the message is sent by use-A.
    sender = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="senders_activities",
        to_field="id",
    )
    latest_reaction = models.JSONField(blank=False, null=False)
    # User who reacted, not the user who sent the reacted message.
    # (But, there is a case that an user sends a message and reacted the message by himself)
    latest_reaction_user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="reacted_users_activity",
        to_field="id",
    )
    reactions = models.JSONField(blank=False, null=False)
    mentioned_user_ids = models.JSONField(blank=False, null=False)
    # { user_id_str: [group_id_int, ...] } — records which mention-groups
    # caused each user to be included in `mentioned_user_ids`. Direct
    # @user mentions don't appear in this map. Used by the sidebar's
    # "Mention" filter to surface "show me only mentions that came in
    # through @group-X". Empty `{}` for non-mention activities.
    mentioned_via_groups = models.JSONField(blank=True, null=True, default=dict)
    is_deleted = models.BooleanField(default=False, db_index=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)
