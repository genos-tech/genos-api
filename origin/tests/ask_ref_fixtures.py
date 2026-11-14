"""Chats, messages and an `/agent/ask/` harness for the MIP-1058 tests (a message and a selected passage as context).

Not a test module. `AskRefWorld` builds every kind of chat the asker (`a_owner`) may or may not read, on top of
`CrossTeamTestCase` (a host team A, a guest team B, a stranger team C), and `ask()` posts to the real ask view with
the model faked, returning what the view handed the agent loop.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.db.models import Max
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine import quota
from origin.search_engine.agent.parity import PARITY_FLAG
from origin.tests.cross_team_fixtures import CrossTeamTestCase

ASK_URL = "/api/v2/agent/ask/"
MISSING = "11111111-2222-4333-8444-555555555555"


def paragraph(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class AskRefWorld(CrossTeamTestCase):
    """The asker is `a_owner`; `a_editor` and `a_viewer` are the other members of team A."""

    flag = True

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: self.flag})
        patcher.start()
        self.addCleanup(patcher.stop)
        # PRO: the free tier appends the read-level block to every ask, which is a different subject.
        self.a_owner.tier = "pro"
        self.a_owner.save(update_fields=["tier"])
        quota.invalidate_effective_tier([self.a_owner.id])
        self.addCleanup(quota.invalidate_effective_tier, [self.a_owner.id])
        self.authenticate(self.a_owner)
        # A 30-day history window for everyone, as a Free plan has: `unreadable()` includes a message older
        # than that. Tests that want no window patch it again.
        self.enterContext(self.with_retention(30))

    # -- chats --------------------------------------------------------------

    def group(self, title, members, team=None, **kw):
        channel = Channel.objects.create(
            team=team or self.team_a, kind=ChannelKind.GM, title=title, **kw
        )
        for user in members:
            ChannelMember.objects.create(channel=channel, user=user)
        return channel

    def direct(self, a, b, team=None):
        channel = Channel.objects.create(team=team or self.team_a, kind=ChannelKind.DM, title="")
        for user in {a.id: a, b.id: b}.values():
            ChannelMember.objects.create(channel=channel, user=user)
        return channel

    def multi(self, users, team=None, title=""):
        channel = Channel.objects.create(
            team=team or self.team_a, kind=ChannelKind.MDM, title=title
        )
        for user in users:
            ChannelMember.objects.create(channel=channel, user=user)
        return channel

    def project_chat(self, project=None, members=()):
        """The PM chat of `project` (the host project by default); `members` join the PROJECT."""
        project = project or self.project
        for user in members:
            ProjectMembers.objects.get_or_create(team=project.team, project=project, attendee=user)
        return Channel.objects.get(project=project, kind=ChannelKind.PM)

    def say(self, channel, text, sender=None, *, at=None, reply_to=None, empty=False, body=None):
        seq = (Message.objects.filter(channel=channel).aggregate(m=Max("seq"))["m"] or 0) + 1
        message = Message.objects.create(
            channel=channel,
            sender=sender or self.a_editor,
            seq=seq,
            body=body if body is not None else ([] if empty else paragraph(text)),
            thread_root=reply_to,
            parent=reply_to,
            is_thread_reply=reply_to is not None,
        )
        if at is not None:
            Message.objects.filter(pk=message.pk).update(ts_sent_at=at)
            message.refresh_from_db()
        return message

    # -- the unreadable ones, one per way a message can be refused ----------

    def unreadable(self, text="SECRET-WORDS"):
        """`{name: message id}`: every way the asker cannot read a message. All hold `text`."""
        out = {}
        out["missing"] = MISSING
        theirs = self.group("Theirs", [self.a_editor, self.a_viewer])
        out["not a member"] = str(self.say(theirs, text).id)
        dm = self.direct(self.a_editor, self.a_viewer)
        out["a dm between two others"] = str(self.say(dm, text).id)
        left = self.group("Left", [self.a_editor])
        ChannelMember.objects.create(channel=left, user=self.a_owner, is_deleted=True)
        out["left the chat"] = str(self.say(left, text).id)
        gone = self.group("Gone", [self.a_owner, self.a_editor])
        out["chat deleted"] = str(self.say(gone, text).id)
        Channel.objects.filter(pk=gone.pk).update(is_deleted=True)
        mine = self.group("Mine", [self.a_owner, self.a_editor])
        deleted = self.say(mine, text)
        Message.objects.filter(pk=deleted.pk).update(deleted_at=timezone.now())
        out["message deleted"] = str(deleted.id)
        TeamMembers.objects.get_or_create(team=self.team_b, attendee=self.a_owner)
        other_team = self.group("Other team", [self.a_owner, self.b_owner], team=self.team_b)
        out["another team's chat"] = str(self.say(other_team, text, sender=self.b_owner).id)
        private = ProjectMaster.objects.create(
            team=self.team_a,
            project_name="Private",
            owner=self.a_editor,
            project_system_user=self.a_editor,
        )
        pm = self.project_chat(private, members=[self.a_editor])
        out["project chat of a project they are not on"] = str(self.say(pm, text).id)
        stray = self.project_chat(self.project, members=[self.a_editor])
        ChannelMember.objects.update_or_create(
            channel=stray, user=self.a_owner, defaults={"is_deleted": False}
        )
        out["a stray channel row on a project chat"] = str(self.say(stray, text).id)
        old = self.say(mine, text, at=timezone.now() - timedelta(days=400))
        out["older than their history window"] = str(old.id)
        return out

    def with_retention(self, days):
        return mock.patch(
            "origin.search_engine.quota.get_message_retention_days", return_value=days
        )

    # -- the ask view -------------------------------------------------------

    def ask(self, *, mentions=None, query="What does this mean?", flag=None, **extra):
        """POST to the real view with the model faked. `(response, captured)`; `captured["kwargs"]` is
        what `run_agent` got, `captured["stream"]` what `_stream_ndjson` got."""
        captured: dict = {}

        def fake_stream(worker, **kwargs):
            captured["worker"] = worker
            captured["stream"] = kwargs
            return iter([b""])

        def fake_run_agent(query, ctx, emit, **kwargs):
            captured["kwargs"] = kwargs
            captured["ctx"] = ctx
            emit({"type": "done"})

        payload = {"query": query, "team_id": str(self.team_a.team_id), **extra}
        if mentions is not None:
            payload["mentions"] = mentions
        patches = [
            mock.patch("origin.search_engine.agent_views._stream_ndjson", side_effect=fake_stream),
            mock.patch("origin.search_engine.agent_views.run_agent", side_effect=fake_run_agent),
            mock.patch(
                "origin.search_engine.agent_views.check_remaining", return_value=(True, 0, None)
            ),
        ]
        if flag is not None:
            patches.append(mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: flag}))
        for p in patches:
            p.start()
        try:
            resp = self.client.post(ASK_URL, payload, format="json")
            if "worker" in captured:
                captured["worker"](lambda event: None, threading.Event())
        finally:
            for p in reversed(patches):
                p.stop()
        return resp, captured

    def done_event(self, **ask_kwargs):
        """The `done` event of a real stream (only `run_agent` faked): `(status, event)`."""

        def fake_run_agent(query, ctx, emit, **kwargs):
            emit({"type": "answer_delta", "text": "ok"})
            emit({"type": "done"})

        with (
            mock.patch("origin.search_engine.agent_views.run_agent", side_effect=fake_run_agent),
            mock.patch(
                "origin.search_engine.agent_views.check_remaining", return_value=(True, 0, None)
            ),
            # The run closes after the stream: no embedding call to a provider from a test.
            mock.patch(
                "origin.search_engine.ingestion.ingest_conversation_run", return_value=False
            ),
        ):
            payload = {
                "query": "What does this mean?",
                "team_id": str(self.team_a.team_id),
                **ask_kwargs,
            }
            resp = self.client.post(ASK_URL, payload, format="json")
            if resp.status_code != 200:
                return resp.status_code, None
            raw = b"".join(resp.streaming_content).decode("utf-8")
        events = [json.loads(line) for line in raw.splitlines() if line.strip()]
        return resp.status_code, next(e for e in events if e["type"] == "done")


def new_uuid():
    return str(uuid.uuid4())
