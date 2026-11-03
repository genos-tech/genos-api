"""`GET /api/v3/channels/`: a constant number of queries (MIP-966).

The chat sidebar loads this list on every boot and team switch. The latest
message of each channel rides along with its sender, reactions, mentions and
attachments, and a few of those were loaded one row at a time:

  * every attachment loaded its uploader on its own (the prefetch named
    `attachments` but not `attachments__uploader`);
  * the reactions and their users took two queries where one join does;
  * the team scope looked up shared chats and shared projects with two grant
    queries where one will do;
  * the channel rows joined `owner` (whose personal fields are decrypted for
    every row) and `project`, which this serializer never reads.

Two kinds of test:

  * query counts: N channels against 2N, each with a latest message that has
    attachments (two different uploaders), reactions and a mention, with the
    budget pinned so a creeping extra query is a visible failure;
  * the payload: the exact response for a fixed fixture, captured from the
    unmodified view first. Ids and timestamps change on every run, so they are
    replaced by placeholders (ids by order of first appearance) before the
    comparison; everything else is compared as it came.
"""

import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageAttachment,
    MessageMention,
    MessageReaction,
)
from origin.models.common.team_models import TeamMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

CHANNELS = "/api/v3/channels/"
GOLDEN = Path(__file__).parent / "golden" / "channel_list_response.json"

# Query budget for the owner of a team with no shared chats or projects,
# counted through the test client the way production `db_n` counts
# (authentication included):
#
#   1 auth
#   1 channels (membership join, team scope, unread count as a subquery)
#   1 team scope: shared chats AND shared projects in one grants query
#   1 latest message per channel (sender, channel, task, project joined)
#   1 reactions (user joined)   1 mentions   1 attachments (uploader joined)
#   1 members of the DM channels
CHANNEL_LIST_BUDGET = 8


def _message(channel, sender, seq, text, **extra):
    return Message.objects.create(
        channel=channel, sender=sender, seq=seq, body={"text": text}, body_text=text, **extra
    )


def _decorate(message, *, attachers, reactors, mentioned):
    """Two attachments, two reactions and a mention on one message."""
    for n, uploader in enumerate(attachers, start=1):
        MessageAttachment.objects.create(
            message=message,
            uploader=uploader,
            file=f"chats/{message.channel_id}/messages/{message.id}/pic{n}.png",
            original_name=f"pic{n}.png",
            mime="image/png",
            size_bytes=100 * n,
        )
    for emoji, user in zip(("thumbsup", "tada"), reactors):
        MessageReaction.objects.create(message=message, user=user, emoji=emoji)
    MessageMention.objects.create(message=message, mentioned_user=mentioned)


class ChannelListQueryCountTests(BaseAPITestCase):
    """N channels with decorated latest messages, against 2N."""

    N = 3

    def setUp(self):
        super().setUp()
        self.authenticate(self.user)
        self.made = 0

    def _add_channels(self, count):
        """`count` DMs and `count` groups, each with a decorated latest message."""
        for _ in range(count):
            self.made += 1
            partner = User.objects.create_user(
                username=f"partner{self.made}",
                email=f"partner{self.made}@example.com",
                password="pw",
            )
            TeamMembers.objects.create(team=self.team, attendee=partner)

            dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
            ChannelMember.objects.create(channel=dm, user=self.user)
            ChannelMember.objects.create(channel=dm, user=partner)

            room = Channel.objects.create(
                team=self.team, kind=ChannelKind.GM, title=f"Room {self.made}", owner=self.user
            )
            ChannelMember.objects.create(channel=room, user=self.user)
            ChannelMember.objects.create(channel=room, user=partner)

            for channel in (dm, room):
                _message(channel, partner, 1, "older")
                latest = _message(channel, partner, 2, "latest")
                _decorate(
                    latest,
                    attachers=(partner, self.user),
                    reactors=(self.user, partner),
                    mentioned=self.user,
                )

    def _measure(self):
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.get(CHANNELS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200, res.content[:300])
        return len(ctx), res.json()["channels"], [q["sql"] for q in ctx.captured_queries]

    @staticmethod
    def _show(sqls):
        return "\n".join(f"  {i + 1}. {sql[:130]}" for i, sql in enumerate(sqls))

    def test_the_count_does_not_grow_with_the_number_of_channels(self):
        self._add_channels(self.N)
        small, small_rows, _ = self._measure()
        self._add_channels(self.N)
        large, large_rows, sqls = self._measure()

        self.assertEqual((len(small_rows), len(large_rows)), (2 * self.N, 4 * self.N))
        # The fixture is only worth anything if the rows carry what drives the
        # per-row queries.
        message = large_rows[0]["latestMessage"]
        self.assertEqual(len(message["attachments"]), 2)
        self.assertEqual(len(message["reactions"]), 2)
        self.assertEqual(len(message["mentions"]), 1)
        self.assertEqual(small, large, f"grew with the data:\n{self._show(sqls)}")
        self.assertEqual(large, CHANNEL_LIST_BUDGET, f"budget:\n{self._show(sqls)}")

    def test_the_paged_list_costs_the_same(self):
        """Paging narrows the rows, not the per-page work."""
        self._add_channels(self.N)
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.get(CHANNELS, {"team_id": str(self.team.team_id), "limit": 4})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["channels"]), 4)
        self.assertEqual(len(ctx), CHANNEL_LIST_BUDGET)

    def test_the_channel_rows_do_not_join_the_owner_or_the_project(self):
        """Neither is read by the serializer; the owner join decrypts per row."""
        self._add_channels(1)
        _, _, sqls = self._measure()
        channel_query = next(sql for sql in sqls if 'FROM "origin_channel" ' in sql)
        self.assertNotIn('JOIN "origin_customuser"', channel_query)
        self.assertNotIn('JOIN "origin_projectmaster"', channel_query)


class ChannelListPayloadTests(CrossTeamTestCase):
    """The exact response for a fixed fixture."""

    def setUp(self):
        super().setUp()
        self.member = self.b_editor
        self.authenticate(self.b_owner)

        # Team B's own DM and group, each with a decorated latest message.
        self.dm = Channel.objects.create(team=self.team_b, kind=ChannelKind.DM, owner=self.b_owner)
        self.room = Channel.objects.create(
            team=self.team_b, kind=ChannelKind.GM, title="Room", owner=self.b_owner
        )
        for channel in (self.dm, self.room):
            ChannelMember.objects.create(channel=channel, user=self.b_owner)
            ChannelMember.objects.create(channel=channel, user=self.member)
            _message(channel, self.member, 1, "older")
            latest = _message(channel, self.member, 2, f"latest in {channel.title or 'dm'}")
            _decorate(
                latest,
                attachers=(self.member, self.b_owner),
                reactors=(self.b_owner, self.member),
                mentioned=self.b_owner,
            )

        # A chat team A shared with team B, and a project team A shared: the
        # two clauses of the team scope that read grants.
        self.shared, _ = self.shared_chat()
        _decorate(
            _message(self.shared, self.a_owner, 100, "from the host"),
            attachers=(self.a_owner, self.b_owner),
            reactors=(self.a_owner, self.b_owner),
            mentioned=self.b_owner,
        )

        self.active_project_grant()
        self.project.code = "HOST"
        self.project.save()
        self.task = TaskMaster.objects.create(
            team=self.team_a, project=self.project, title="Shared task", status="Open"
        )
        self.pm, _ = Channel.objects.get_or_create(
            project=self.project,
            kind=ChannelKind.PM,
            defaults={"team": self.team_a, "title": "Host Project", "owner": self.a_owner},
        )
        ChannelMember.objects.get_or_create(channel=self.pm, user=self.b_owner)
        _decorate(
            _message(self.pm, self.a_owner, 100, "task header", task=self.task),
            attachers=(self.b_owner, self.a_owner),
            reactors=(self.a_owner, self.b_owner),
            mentioned=self.b_owner,
        )

    UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")

    def _normalized(self, rows):
        """Rows in a fixed order, with this run's ids and clocks made stable."""
        rows = sorted(rows, key=lambda c: (c["kind"], c["title"]))
        for row in rows:
            message = row["latestMessage"]
            if message is None:
                continue
            message["reactions"].sort(key=lambda r: (r["emoji"], r["user"]["userName"]))
            message["attachments"].sort(key=lambda a: a["fileName"])
        text = json.dumps(rows, indent=1)
        seen = {}

        def uuid(match):
            return f"<uuid:{seen.setdefault(match.group(0), len(seen) + 1)}>"

        text = self.UUID.sub(uuid, text)
        text = self.STAMP.sub("<time>", text)
        # Auto-increment values (a PM channel's `legacyChatId` is its project's
        # id), which depend on how many rows earlier tests created.
        text = re.sub(r'("(?:projectId|taskId|legacyChatId)"): \d+', r'\1: "<int>"', text)
        return json.loads(text)

    def _list(self):
        cache.clear()
        # Creating the shared chat signed the client in as the host's owner.
        self.authenticate(self.b_owner)
        res = self.client.get(CHANNELS, {"team_id": str(self.team_b.team_id)})
        self.assertEqual(res.status_code, 200, res.content[:300])
        return res.json()["channels"]

    def test_the_response_is_exactly_what_it_was(self):
        """Byte for byte (keys in order too) against the response captured
        from the view before it was made cheaper, `golden/channel_list_response.json`.
        Reaction, attachment and channel order are sorted first: the database
        promises none of them, so the comparison must not either."""
        got = json.dumps(self._normalized(self._list()), indent=1)

        self.assertEqual(got, GOLDEN.read_text().strip())

    def test_all_four_kinds_of_scope_are_in_it(self):
        """The fixture reaches every clause of the team scope."""
        rows = self._list()
        self.assertEqual(
            sorted((c["kind"], c["isExternal"], c["hostTeamName"]) for c in rows),
            sorted(
                [
                    (ChannelKind.DM, False, None),
                    (ChannelKind.GM, False, None),
                    (ChannelKind.GM, True, self.team_a.team_name),
                    (ChannelKind.PM, False, self.team_a.team_name),
                ]
            ),
        )
