"""Webhook event scoping, and the privacy rule at the centre of it.

Before this, an endpoint subscribed to an event *type* and received it
for the whole team. That is defensible for task metadata and indefensible
for chat, so the two axes deliberately do NOT behave the same way:

    project_ids = []   →  every project      (a filter, unset)
    channel_ids = []   →  NO channel         (an allow-list, empty)

`test_the_two_empty_lists_mean_opposite_things` pins that asymmetry,
because it is the kind of thing a later reader "tidies up" into
consistency and silently subscribes every conversation in the team.

DM channels cannot be named at all. A group channel has a shared,
visible membership; a private one-to-one does not, and the person
configuring the webhook is not usually in it.
"""

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.webhook_models import (
    EVENT_MESSAGE_CREATED,
    EVENT_TASK_COMMENT_CREATED,
    EVENT_TASK_CREATED,
    WebhookDelivery,
    WebhookEndpoint,
    generate_secret,
)
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

WEBHOOKS = "/api/v2/webhooks/"


def _endpoint(team, events, *, project_ids=None, channel_ids=None):
    e = WebhookEndpoint(
        team=team,
        url="https://example.invalid/hook",
        events=events,
        project_ids=project_ids or [],
        channel_ids=channel_ids or [],
    )
    e.set_secret(generate_secret())
    e.save()
    return e


class ScopeMatchingTests(BaseAPITestCase):
    """`subscribes_to` in isolation — the one place the rules live."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Mine", owner=self.user
        )
        self.other_project = ProjectMaster.objects.create(
            team=self.team, project_name="Theirs", owner=self.user
        )

    def test_no_project_filter_means_every_project(self):
        e = _endpoint(self.team, [EVENT_TASK_CREATED])
        self.assertTrue(
            e.subscribes_to(EVENT_TASK_CREATED, {"project_id": self.project.project_id})
        )
        self.assertTrue(
            e.subscribes_to(EVENT_TASK_CREATED, {"project_id": self.other_project.project_id})
        )

    def test_a_project_filter_excludes_the_others(self):
        e = _endpoint(self.team, [EVENT_TASK_CREATED], project_ids=[self.project.project_id])
        self.assertTrue(
            e.subscribes_to(EVENT_TASK_CREATED, {"project_id": self.project.project_id})
        )
        self.assertFalse(
            e.subscribes_to(EVENT_TASK_CREATED, {"project_id": self.other_project.project_id})
        )

    def test_an_unfiltered_endpoint_still_gets_an_orphaned_task(self):
        """`ProjectMaster` deletion is SET_NULL, so a task with no project
        is a real state — and `test_get_team_tasks_handles_null_fks`
        already caught one fix that made those vanish."""
        e = _endpoint(self.team, [EVENT_TASK_CREATED])
        self.assertTrue(e.subscribes_to(EVENT_TASK_CREATED, {"project_id": None}))

    def test_a_filtered_endpoint_does_not_get_an_orphaned_task(self):
        """The other side of the same case: the subscription names
        projects, and this object is in none of them."""
        e = _endpoint(self.team, [EVENT_TASK_CREATED], project_ids=[self.project.project_id])
        self.assertFalse(e.subscribes_to(EVENT_TASK_CREATED, {"project_id": None}))

    def test_the_two_empty_lists_mean_opposite_things(self):
        """The asymmetry, stated once so it cannot be tidied away.

        Empty `project_ids` is an unset filter. Empty `channel_ids` is an
        empty allow-list. Making them behave alike in either direction is
        a bug: one way silently subscribes every conversation, the other
        silently drops every task event.
        """
        e = _endpoint(self.team, [EVENT_TASK_CREATED, EVENT_MESSAGE_CREATED])
        self.assertTrue(e.subscribes_to(EVENT_TASK_CREATED, {"project_id": 999}))
        self.assertFalse(e.subscribes_to(EVENT_MESSAGE_CREATED, {"channel_id": "any"}))

    def test_an_inactive_endpoint_matches_nothing(self):
        e = _endpoint(self.team, [EVENT_TASK_CREATED])
        e.is_active = False
        e.save(update_fields=["is_active"])
        self.assertFalse(e.subscribes_to(EVENT_TASK_CREATED, {"project_id": None}))

    def test_an_unsubscribed_event_never_matches(self):
        e = _endpoint(self.team, [EVENT_TASK_CREATED], channel_ids=["c"])
        self.assertFalse(e.subscribes_to(EVENT_MESSAGE_CREATED, {"channel_id": "c"}))


class ChatSubscriptionValidationTests(BaseAPITestCase):
    """What the create endpoint will and will not accept."""

    def setUp(self):
        super().setUp()
        self.group = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Engineering"
        )
        self.dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="")
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Mine", owner=self.user
        )
        self.authenticate(self.user)

    def _create(self, **extra):
        body = {
            "team_id": str(self.team.team_id),
            "url": "https://example.com/hook",
            "events": [EVENT_TASK_CREATED],
        }
        body.update(extra)
        return self.client.post(WEBHOOKS, body, format="json")

    def test_chat_without_channels_is_refused(self):
        res = self._create(events=[EVENT_MESSAGE_CREATED])
        self.assertEqual(res.status_code, 400)
        self.assertIn("channel_ids", res.data["error"])
        self.assertEqual(WebhookEndpoint.objects.count(), 0)

    def test_a_dm_cannot_be_subscribed_to(self):
        res = self._create(events=[EVENT_MESSAGE_CREATED], channel_ids=[str(self.dm.id)])
        self.assertEqual(res.status_code, 400)
        self.assertIn("Direct messages", res.data["error"])
        self.assertEqual(WebhookEndpoint.objects.count(), 0)

    def test_a_dm_cannot_ride_along_with_a_group_channel(self):
        """The list is validated whole — one eligible channel must not
        launder an ineligible one."""
        res = self._create(
            events=[EVENT_MESSAGE_CREATED],
            channel_ids=[str(self.group.id), str(self.dm.id)],
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(WebhookEndpoint.objects.count(), 0)

    def test_a_group_channel_is_accepted(self):
        res = self._create(events=[EVENT_MESSAGE_CREATED], channel_ids=[str(self.group.id)])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["channel_ids"], [str(self.group.id)])

    def test_a_channel_from_another_team_is_refused(self):
        outsider = User.objects.create_user(
            username="wsout", email="wsout@example.com", password="pw"
        )
        other_team = TeamMaster.objects.create(
            team_name="WS Other", team_email="wsout@team.com", owner=outsider
        )
        TeamMembers.objects.create(team=other_team, attendee=outsider)
        foreign = Channel.objects.create(team=other_team, kind=ChannelKind.GM, title="Theirs")
        res = self._create(events=[EVENT_MESSAGE_CREATED], channel_ids=[str(foreign.id)])
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unknown channels", res.data["error"])

    def test_a_project_from_another_team_is_refused(self):
        outsider = User.objects.create_user(
            username="wsout2", email="wsout2@example.com", password="pw"
        )
        other_team = TeamMaster.objects.create(
            team_name="WS Other2", team_email="wsout2@team.com", owner=outsider
        )
        foreign = ProjectMaster.objects.create(
            team=other_team, project_name="Theirs", owner=outsider
        )
        res = self._create(project_ids=[foreign.project_id])
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unknown projects", res.data["error"])

    def test_channels_without_a_chat_event_is_refused(self):
        """Naming channels while subscribing only to task events is a
        mistake worth surfacing, not silently storing."""
        res = self._create(channel_ids=[str(self.group.id)])
        self.assertEqual(res.status_code, 400)

    def test_a_project_filter_is_accepted_and_stored(self):
        res = self._create(project_ids=[self.project.project_id])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["project_ids"], [self.project.project_id])

    def test_no_scope_at_all_still_works(self):
        """The pre-scoping shape — task events, whole team."""
        res = self._create()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["project_ids"], [])
        self.assertEqual(res.data["channel_ids"], [])


class ChatEmissionTests(BaseAPITestCase):
    """End-to-end: sending a message produces (or does not produce) a
    delivery row."""

    def setUp(self):
        super().setUp()
        self.group = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Eng")
        ChannelMember.objects.create(channel=self.group, user=self.user, role="owner")
        self.dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="")
        ChannelMember.objects.create(channel=self.dm, user=self.user, role="member")
        self.authenticate(self.user)

    def _send(self, channel):
        return self.client.post(
            f"/api/v3/channels/{channel.id}/messages/",
            {"body": [], "body_text": "hello there"},
            format="json",
        )

    def test_a_subscribed_group_message_is_queued(self):
        _endpoint(self.team, [EVENT_MESSAGE_CREATED], channel_ids=[str(self.group.id)])
        with self.captureOnCommitCallbacks(execute=True):
            res = self._send(self.group)
        self.assertIn(res.status_code, (200, 201))
        delivery = WebhookDelivery.objects.filter(event=EVENT_MESSAGE_CREATED).first()
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery.payload["body_text"], "hello there")
        self.assertEqual(delivery.payload["channel_id"], str(self.group.id))
        # The payload is built eagerly, before commit, from a
        # just-created instance — so the auto-populated fields have to be
        # present at that moment, not merely present after a refresh.
        self.assertIsNotNone(delivery.payload["created_at"])
        self.assertIn("thread_root_id", delivery.payload)
        self.assertFalse(delivery.payload["is_thread_reply"])

    def test_an_unnamed_channel_is_not_queued(self):
        other = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Other")
        ChannelMember.objects.create(channel=other, user=self.user, role="owner")
        _endpoint(self.team, [EVENT_MESSAGE_CREATED], channel_ids=[str(other.id)])
        with self.captureOnCommitCallbacks(execute=True):
            self._send(self.group)
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_a_dm_is_never_queued_even_if_the_id_is_forced_into_the_row(self):
        """Belt and braces. Validation refuses a DM at subscribe time, so
        this row can only exist if someone wrote it directly — and the
        emit path still refuses, because the two checks guard different
        mistakes.
        """
        _endpoint(self.team, [EVENT_MESSAGE_CREATED], channel_ids=[str(self.dm.id)])
        with self.captureOnCommitCallbacks(execute=True):
            self._send(self.dm)
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_one_message_is_one_delivery(self):
        _endpoint(self.team, [EVENT_MESSAGE_CREATED], channel_ids=[str(self.group.id)])
        with self.captureOnCommitCallbacks(execute=True):
            self._send(self.group)
        self.assertEqual(WebhookDelivery.objects.count(), 1)

    def test_nothing_is_queued_without_a_subscriber(self):
        with self.captureOnCommitCallbacks(execute=True):
            res = self._send(self.group)
        self.assertIn(res.status_code, (200, 201))
        self.assertEqual(WebhookDelivery.objects.count(), 0)


class CommentEmissionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Mine", owner=self.user, code="MI"
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="T",
            status="Open",
            reporter=self.user,
            project_task_number=1,
        )
        self.authenticate(self.user)

    def _comment(self):
        return self.client.post(
            "/api/v2/task/comment/",
            {
                "task_id": self.task.task_id,
                "sender_id": str(self.user.id),
                "comment_body": {"text": "looks good"},
            },
            format="json",
        )

    def test_a_comment_is_queued_with_its_project(self):
        _endpoint(self.team, [EVENT_TASK_COMMENT_CREATED])
        with self.captureOnCommitCallbacks(execute=True):
            self._comment()
        delivery = WebhookDelivery.objects.filter(event=EVENT_TASK_COMMENT_CREATED).first()
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery.payload["task_id"], self.task.task_id)
        self.assertEqual(delivery.payload["project_id"], self.project.project_id)
        # Was silently null: the model field is `ts_sent_at`, and a
        # `getattr(..., "ts_created_at", None)` published the miss.
        self.assertIsNotNone(delivery.payload["created_at"])

    def test_a_comment_outside_the_project_filter_is_not_queued(self):
        elsewhere = ProjectMaster.objects.create(
            team=self.team, project_name="Elsewhere", owner=self.user
        )
        _endpoint(self.team, [EVENT_TASK_COMMENT_CREATED], project_ids=[elsewhere.project_id])
        with self.captureOnCommitCallbacks(execute=True):
            self._comment()
        self.assertEqual(WebhookDelivery.objects.count(), 0)

    def test_the_pm_thread_mirror_does_not_also_emit_a_chat_event(self):
        """A comment is mirrored into the PM channel as a Message. That is
        internal plumbing — an integrator subscribed to both must see one
        `task.comment_created`, not a second `message.created` revealing
        how comments happen to be stored.
        """
        # `pm_channel_signals` already created this when the project was
        # created — `uniq_pm_channel_per_project` makes a second one an
        # IntegrityError, which is how the fixture found out.
        pm = Channel.objects.get(project=self.project, kind=ChannelKind.PM)
        _endpoint(
            self.team,
            [EVENT_TASK_COMMENT_CREATED, EVENT_MESSAGE_CREATED],
            channel_ids=[str(pm.id)],
        )
        with self.captureOnCommitCallbacks(execute=True):
            self._comment()
        self.assertEqual(WebhookDelivery.objects.filter(event=EVENT_MESSAGE_CREATED).count(), 0)


class ChannelListTeamFilterTests(BaseAPITestCase):
    """`GET /api/v3/channels/?team_id=` — enabling the scope picker.

    The serialized channel row carries no team of its own, and the chat
    sidebar deliberately wants every team at once. But a picker choosing
    channels *within* one team cannot tell them apart, so a user in two
    teams would be offered — and could name — a channel the webhook's
    team does not contain.
    """

    def setUp(self):
        super().setUp()
        self.mine = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Ours")
        ChannelMember.objects.create(channel=self.mine, user=self.user, role="owner")

        self.second_team = TeamMaster.objects.create(
            team_name="Second", team_email="second@team.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.second_team, attendee=self.user)
        self.theirs = Channel.objects.create(
            team=self.second_team, kind=ChannelKind.GM, title="Other team"
        )
        ChannelMember.objects.create(channel=self.theirs, user=self.user, role="owner")
        self.authenticate(self.user)

    def test_without_a_team_id_every_team_is_returned(self):
        """The chat sidebar depends on this — the filter must stay
        opt-in, not become the default."""
        res = self.client.get("/api/v3/channels/")
        titles = {c["title"] for c in res.data["channels"]}
        self.assertIn("Ours", titles)
        self.assertIn("Other team", titles)

    def test_a_team_id_narrows_to_that_team(self):
        res = self.client.get("/api/v3/channels/", {"team_id": str(self.team.team_id)})
        titles = {c["title"] for c in res.data["channels"]}
        self.assertEqual(titles, {"Ours"})

    def test_naming_a_team_you_are_not_in_returns_nothing(self):
        """Narrowing only. The queryset is already restricted to the
        caller's memberships, so this can never widen access."""
        outsider = User.objects.create_user(
            username="cltfout", email="cltfout@example.com", password="pw"
        )
        foreign_team = TeamMaster.objects.create(
            team_name="Foreign", team_email="cltfout@team.com", owner=outsider
        )
        Channel.objects.create(team=foreign_team, kind=ChannelKind.GM, title="Not yours")
        res = self.client.get("/api/v3/channels/", {"team_id": str(foreign_team.team_id)})
        self.assertEqual(res.data["channels"], [])


class ScopeInputShapeTests(BaseAPITestCase):
    """Ids arrive from JSON, so their Python type is not ours to assume.

    Both of these were real: comparing raw request input against ids read
    back from the database mixes types, and the membership test itself
    raises on a value that is not hashable.
    """

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Mine", owner=self.user
        )
        self.authenticate(self.user)

    def _create(self, project_ids):
        return self.client.post(
            WEBHOOKS,
            {
                "team_id": str(self.team.team_id),
                "url": "https://example.com/hook",
                "events": [EVENT_TASK_CREATED],
                "project_ids": project_ids,
            },
            format="json",
        )

    def test_a_project_id_sent_as_a_string_is_accepted(self):
        """JSON clients commonly send ids as strings. `"12" not in {12}`
        reported the caller's own project as unknown."""
        res = self._create([str(self.project.project_id)])
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["project_ids"], [self.project.project_id])

    def test_an_integer_project_id_still_works(self):
        res = self._create([self.project.project_id])
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["project_ids"], [self.project.project_id])

    def test_a_non_scalar_project_id_is_400_not_500(self):
        """`p not in valid` raises `unhashable type: 'dict'` — a 500 on
        request input, reachable by anyone who can create a webhook."""
        res = self._create([{"id": 1}])
        self.assertEqual(res.status_code, 400)

    def test_a_non_numeric_project_id_is_400(self):
        res = self._create(["not-a-number"])
        self.assertEqual(res.status_code, 400)

    def test_a_project_id_from_another_team_is_still_refused_as_a_string(self):
        """Parsing must not become a way past the ownership check."""
        outsider = User.objects.create_user(
            username="sistout", email="sistout@example.com", password="pw"
        )
        other_team = TeamMaster.objects.create(
            team_name="SIST Other", team_email="sistout@team.com", owner=outsider
        )
        foreign = ProjectMaster.objects.create(
            team=other_team, project_name="Theirs", owner=outsider
        )
        res = self._create([str(foreign.project_id)])
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unknown projects", res.data["error"])


class ChannelMemberIdShapeTests(BaseAPITestCase):
    """The counterparty check parses UUIDs; casing must not matter.

    `str(uuid.UUID(x))` canonicalises, so an uppercase id resolves
    normally — checked here rather than assumed, because the same
    normalisation makes two *spellings of one id* collapse, which is
    refused on purpose.
    """

    def test_an_uppercase_uuid_is_accepted(self):
        self.authenticate(self.user)
        res = self.client.post(
            "/api/v3/channels/",
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team.team_id),
                "title": "Upper",
                "member_user_ids": [str(self.user2.id).upper()],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

    def test_the_same_id_twice_in_two_spellings_is_refused(self):
        """Deliberate: the request asks for two members and names one
        person, so it is malformed rather than something to quietly
        deduplicate into a different channel than was requested."""
        self.authenticate(self.user)
        res = self.client.post(
            "/api/v3/channels/",
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team.team_id),
                "title": "Dup",
                "member_user_ids": [str(self.user2.id), str(self.user2.id).upper()],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
