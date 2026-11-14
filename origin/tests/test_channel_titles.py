"""`channel_titles` (services/channel_scope.py) names a chat exactly as `list_my_chats._titles`
did before the rule moved (MIP-1499). `old_titles` below is that function, copied unchanged, so
the pin is a comparison and not a re-typed expectation: over a DM, a chat with yourself, a DM
with a stored title, a group chat with and without a title, a project chat, and a multi-person
DM with fewer, exactly and more than `MDM_NAMES` other people."""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster
from origin.search_engine.agent.tools import list_my_chats
from origin.services import channel_scope, item_lines
from origin.services.channel_scope import MDM_NAMES, channel_titles

User = get_user_model()


def old_titles(channels: list, user_id: str) -> dict[Any, str]:
    """Readable name per channel id. A stored title wins; a DM or multi-person
    DM has none and is named after the other people, as the app does."""
    unnamed = [
        c.id for c in channels if not c.title and c.kind in (ChannelKind.DM, ChannelKind.MDM)
    ]
    people: dict[Any, list[tuple[Any, str]]] = {}
    if unnamed:
        for m in (
            ChannelMember.objects.filter(channel_id__in=unnamed, is_deleted=False)
            .select_related("user")
            .order_by("ts_joined_at")
        ):
            people.setdefault(m.channel_id, []).append((m.user_id, (m.user.username or "")))
    out: dict[Any, str] = {}
    for c in channels:
        if c.title:
            out[c.id] = c.title
            continue
        members = people.get(c.id, [])
        others = [name for uid, name in members if str(uid) != user_id and name]
        if c.kind == ChannelKind.DM:
            # The partner; in a chat with yourself, you.
            out[c.id] = others[0] if others else (members[0][1] if members else "")
        elif c.kind == ChannelKind.MDM:
            shown = ", ".join(others[:MDM_NAMES])
            more = len(others) - MDM_NAMES
            out[c.id] = f"{shown} +{more} more" if more > 0 else shown
        else:
            out[c.id] = ""
    return out


class TestChannelTitles(TestCase):
    def setUp(self):
        self.team = TeamMaster.objects.create(team_name="T")
        self.me = self.person("me")

    def person(self, name):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password="x")

    def channel(self, kind, members, title=""):
        channel = Channel.objects.create(team=self.team, kind=kind, title=title)
        for user in members:
            ChannelMember.objects.create(channel=channel, user=user)
        return channel

    def world(self):
        ann, bob = self.person("ann"), self.person("bob")
        crowd = [self.person(f"p{i}") for i in range(MDM_NAMES + 3)]
        return [
            self.channel(ChannelKind.DM, [self.me, ann]),
            self.channel(ChannelKind.DM, [ann, self.me]),
            self.channel(ChannelKind.DM, [self.me]),  # a chat with yourself
            self.channel(ChannelKind.DM, [self.me, bob], title="Pairing"),
            self.channel(ChannelKind.DM, []),
            self.channel(ChannelKind.GM, [self.me, ann], title="Design"),
            self.channel(ChannelKind.GM, [self.me, ann]),
            self.channel(ChannelKind.PM, [self.me, ann], title="Beta"),
            self.channel(ChannelKind.PM, [self.me, ann]),
            self.channel(ChannelKind.MDM, [self.me, ann, bob]),
            self.channel(ChannelKind.MDM, [self.me, *crowd[:MDM_NAMES]]),
            self.channel(ChannelKind.MDM, [self.me, *crowd]),
            self.channel(ChannelKind.MDM, [self.me, bob], title="Lunch"),
            self.channel(ChannelKind.MDM, [self.me]),
        ]

    def test_it_names_every_kind_of_chat_as_the_old_rule_did(self):
        channels = self.world()
        for name in ("me", "ann", "bob"):
            viewer = str(User.objects.get(username=name).id)
            got = channel_titles(channels, viewer)
            self.assertEqual(got, old_titles(channels, viewer), name)
            self.assertEqual(len(got), len(channels))

    def test_the_world_really_holds_the_cases_the_pin_compares(self):
        channels = self.world()
        got = channel_titles(channels, str(self.me.id))
        by_index = [got[c.id] for c in channels]
        self.assertEqual(by_index[0], "ann")
        self.assertEqual(by_index[2], "me")  # a chat with yourself is named after you
        self.assertEqual(by_index[3], "Pairing")
        self.assertEqual(by_index[4], "")
        self.assertEqual(by_index[6], "")  # a group chat with no title has no name
        self.assertEqual(by_index[9], "ann, bob")
        self.assertEqual(by_index[10], "p0, p1, p2, p3, p4")
        self.assertEqual(by_index[11], "p0, p1, p2, p3, p4 +3 more")
        self.assertEqual(by_index[12], "Lunch")

    def test_it_costs_one_query_for_all_the_unnamed_chats(self):
        channels = self.world()
        with CaptureQueriesContext(connection) as ctx:
            channel_titles(channels, str(self.me.id))
        self.assertEqual(len(ctx), 1)
        with CaptureQueriesContext(connection) as ctx:
            channel_titles([c for c in channels if c.title], str(self.me.id))
        self.assertEqual(len(ctx), 0)

    def test_no_channels_is_no_names(self):
        self.assertEqual(channel_titles([], str(self.me.id)), {})

    def test_the_chat_list_tool_and_the_item_line_use_the_one_rule(self):
        self.assertIs(list_my_chats.channel_titles, channel_titles)
        self.assertIs(item_lines.channel_titles, channel_titles)
        self.assertFalse(hasattr(list_my_chats, "_titles"))
        self.assertTrue(hasattr(channel_scope, "MDM_NAMES"))
