from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser


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
    #    3: "join gm request",
    #    4: "note access request"
    #    5: "team ownership claim" (break-glass recovery for an absent
    #       owner — see origin/services/ownership_claim.py; `receiver` is
    #       the CURRENT owner, `sender` the editor claiming)
    #    6: "Genos digest" (proactive tier digest, UX tier model §8 —
    #       system-authored: `sender` is NULL, `item_body` carries
    #       {title, text}; not a request, `request_status` is "")
    #    7: "team connection request" (another TEAM asks to connect — see
    #       origin/services/team_connection.py; `receiver` is the asked
    #       team's owner, `team` is the asked team, `item_optionals`
    #       carries {connection_id, requesting_team_id,
    #       requesting_team_name}. The only request item whose subject is
    #       a team rather than a person)
    #    8: "external share offer" (a connected team offers access to one
    #       object — origin/services/external_grants.py; `receiver` is
    #       the GUEST team's owner, `team` is the guest team, and
    #       `item_optionals` carries {grant_id, object_type, object_id,
    #       owner_team_name})
    #    9: "message reminder" (a reminder the user set on a chat message
    #       came due — origin/services/message_reminders.py; `receiver` is
    #       the person who asked for it, `sender` the message's author,
    #       `item_body` carries {title, text} and `item_optionals` the
    #       facts the client needs to phrase and link it: {kind,
    #       message_id, channel_id, chat_kind, thread_root_id,
    #       sender_name, preview, href, remind_at}. Not a request, so
    #       `request_status` is "". Lives in the Activities half of the
    #       inbox with 0 and 6)
    # }
    #########################################################
    item_type = models.IntegerField(blank=False)
    item_optionals = models.JSONField(blank=True, null=True)
    is_read = models.BooleanField(default=False)
    #########################################################
    # request_status: only relevant for request items (item_type 1-5, 7-8)
    #   "pending"  = waiting for action
    #   "approved" = approved by owner
    #   "rejected" = rejected by owner
    #########################################################
    request_status = models.CharField(max_length=10, default="pending", blank=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)
