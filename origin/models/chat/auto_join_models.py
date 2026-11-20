"""Which open chats a person is put into when they join the team (MIP-1708).

A new team gets an open `general` chat, and each department the owner names gets an open chat
of its own (`services/team_setup`). Nothing used to say "this chat is for everybody who joins" or
"this chat belongs to that department", so nobody was added to them and a newcomer found them
by name in chat search.

One small table says it, instead of columns on `Channel`: the chat table is read by every list,
search and serializer, and "who joins it" is a rule ABOUT a chat, not a property every chat has.
A row with no `mention_group` is the team's default chat (everybody who joins the team is put
into it); a row with a `mention_group` is that department's chat (the members of the group are put
into it, when they join the team and when they are added to the group). Only the code that makes
the chats writes rows (`team_setup`), so the product's own paths cannot mark a chat by accident.
What a person who LEFT the chat gets, and who is never added: `services/team_default_chats`.
"""

from __future__ import annotations

from django.db import models

from origin.models.chat.unified_models import Channel
from origin.models.common.mention_group_models import MentionGroupMaster
from origin.models.common.team_models import TeamMaster


class ChannelAutoJoin(models.Model):
    id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="auto_join_channels",
        to_field="team_id",
    )
    # One rule per chat. Deleting the chat row for good takes its rule with it; a soft-deleted
    # chat keeps the row and is skipped when people are added.
    channel = models.OneToOneField(Channel, on_delete=models.CASCADE, related_name="auto_join")
    # NULL: the team's default chat. Otherwise the department's mention group.
    mention_group = models.ForeignKey(
        MentionGroupMaster,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="auto_join_channels",
        to_field="group_id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # At most one default chat per team: "the chat everybody joins" is a single chat.
            models.UniqueConstraint(
                fields=["team"],
                condition=models.Q(mention_group__isnull=True),
                name="uniq_default_chat_per_team",
            ),
        ]
