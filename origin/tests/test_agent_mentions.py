"""Structured @/# mention handling on `/api/v2/agent/ask/`.

Covers the three layers of `agent/mentions.py`:

  * `parse_mentions` — pure shape validation (caps, coercion, dedupe;
    malformed entries dropped, malformed payloads rejected);
  * `resolve_mentions` — DB + ACL resolution with SILENT drop for
    anything the requesting user can't read (never a 403 — existence
    must not leak);
  * `build_mention_system_extra` / `build_mention_seed_sources` — the
    prompt block and pre-seeded source chips, always from canonical DB
    titles (client labels are advisory only);

plus the view wiring: mentions parse → 400 on malformed payloads,
resolved mentions reach `run_agent` via system_extra/seed_sources
(appending to — not replacing — a thread/note context block), and the
resolved list is persisted on `AgentRun.mentions`.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase

from origin.models.chat.unified_models import Channel, ChannelMember
from origin.models.common.team_models import TeamMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.mentions import (
    MentionParseError,
    ResolvedMention,
    build_mention_seed_sources,
    build_mention_system_extra,
    parse_mentions,
    resolve_mentions,
)
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.models import AgentRun

from .test_base import BaseAPITestCase

User = get_user_model()

ASK_URL = "/api/v2/agent/ask/"


class ParseMentionsTests(SimpleTestCase):
    def test_none_and_empty_list_parse_to_empty(self):
        self.assertEqual(parse_mentions(None), [])
        self.assertEqual(parse_mentions([]), [])

    def test_non_list_payload_is_a_parse_error(self):
        with self.assertRaises(MentionParseError):
            parse_mentions({"type": "task", "task_id": 1})

    def test_over_cap_is_a_parse_error(self):
        entries = [{"type": "task", "task_id": i} for i in range(21)]
        with self.assertRaises(MentionParseError):
            parse_mentions(entries)

    def test_malformed_entries_are_dropped_not_fatal(self):
        parsed = parse_mentions(
            [
                "not-a-dict",
                {"type": "wormhole", "id": 1},
                {"type": "task", "task_id": "abc"},
                {"type": "note", "note_type": 4, "note_id": 1},  # 4 = UI bucket, not a table
                {"type": "chat", "chat_type": 9, "chat_id": "x"},
                {"type": "user", "user_id": "  "},
                {"type": "task", "task_id": 7},
            ]
        )
        self.assertEqual(parsed, [{"type": "task", "task_id": 7, "label": ""}])

    def test_ids_are_coerced_and_deduped(self):
        parsed = parse_mentions(
            [
                {"type": "task", "task_id": "12", "label": "A"},
                {"type": "task", "task_id": 12, "label": "dupe"},
                {"type": "note", "note_type": "1", "note_id": "5", "label": "N"},
            ]
        )
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0], {"type": "task", "task_id": 12, "label": "A"})
        self.assertEqual(parsed[1]["note_type"], 1)
        self.assertEqual(parsed[1]["note_id"], 5)

    def test_project_entries_parse_coerce_and_dedupe(self):
        parsed = parse_mentions(
            [
                {"type": "project", "project_id": "7", "label": "Web"},
                {"type": "project", "project_id": 7, "label": "dupe"},
                {"type": "project", "project_id": "abc"},
            ]
        )
        self.assertEqual(parsed, [{"type": "project", "project_id": 7, "label": "Web"}])


class ResolveMentionsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        # Task readable by self.user (assignee); no project members.
        self.task = TaskMaster.objects.create(
            team=self.team, title="Fix login flow", assignee=self.user, reporter=self.user
        )
        # Personal note owned by self.user.
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Roadmap ideas", body=[]
        )
        # GM channel with self.user as its only member.
        self.channel = Channel.objects.create(team=self.team, kind=2, title="backend-team")
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        # Project with self.user as its only member.
        self.project = ProjectMaster.objects.create(team=self.team, project_name="Website Redesign")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def _resolve_one(self, entry, ctx=None):
        return resolve_mentions(parse_mentions([entry]), ctx or self.ctx)

    def test_user_mention_resolves_with_canonical_username(self):
        out = self._resolve_one(
            {"type": "user", "user_id": str(self.user2.id), "label": "spoofed label"}
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "user")
        self.assertEqual(out[0].label, "otheruser")  # DB name, not the client's
        self.assertEqual(out[0].user_id, str(self.user2.id))

    def test_non_team_member_is_dropped(self):
        outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        self.assertEqual(self._resolve_one({"type": "user", "user_id": str(outsider.id)}), [])

    def test_deleted_and_system_users_are_dropped(self):
        self.user2.is_deleted = True
        self.user2.save(update_fields=["is_deleted"])
        self.assertEqual(self._resolve_one({"type": "user", "user_id": str(self.user2.id)}), [])

    def test_task_mention_resolves_for_assignee(self):
        out = self._resolve_one({"type": "task", "task_id": self.task.task_id})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].label, "Fix login flow")
        self.assertEqual(out[0].task_id, self.task.task_id)

    def test_task_outside_acl_is_dropped(self):
        ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.assertEqual(
            self._resolve_one({"type": "task", "task_id": self.task.task_id}, ctx2), []
        )

    def test_deleted_missing_and_cross_team_tasks_are_dropped(self):
        self.assertEqual(self._resolve_one({"type": "task", "task_id": 999_999}), [])
        self.task.is_deleted = True
        self.task.save(update_fields=["is_deleted"])
        self.assertEqual(self._resolve_one({"type": "task", "task_id": self.task.task_id}), [])
        self.task.is_deleted = False
        self.task.save(update_fields=["is_deleted"])
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        ctx_other = ToolContext(team_id=str(other_team.team_id), user_id=str(self.user.id))
        self.assertEqual(
            self._resolve_one({"type": "task", "task_id": self.task.task_id}, ctx_other), []
        )

    def test_note_mention_resolves_for_owner_only(self):
        out = self._resolve_one({"type": "note", "note_type": 1, "note_id": self.note.note_id})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].label, "Roadmap ideas")
        self.assertEqual(out[0].note_type_label, "personal")
        ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.assertEqual(
            self._resolve_one({"type": "note", "note_type": 1, "note_id": self.note.note_id}, ctx2),
            [],
        )

    def test_chat_mention_resolves_for_member_only(self):
        out = self._resolve_one({"type": "chat", "chat_type": 2, "chat_id": str(self.channel.id)})
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].label, "backend-team")
        self.assertEqual(out[0].chat_type_label, "gm")
        ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.assertEqual(
            self._resolve_one(
                {"type": "chat", "chat_type": 2, "chat_id": str(self.channel.id)}, ctx2
            ),
            [],
        )

    def test_chat_kind_mismatch_and_bad_uuid_are_dropped(self):
        self.assertEqual(
            self._resolve_one({"type": "chat", "chat_type": 1, "chat_id": str(self.channel.id)}),
            [],
        )
        self.assertEqual(
            self._resolve_one({"type": "chat", "chat_type": 2, "chat_id": "not-a-uuid"}), []
        )

    def test_project_mention_resolves_for_member_only(self):
        out = self._resolve_one(
            {"type": "project", "project_id": self.project.project_id, "label": "spoofed"}
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "project")
        self.assertEqual(out[0].label, "Website Redesign")  # DB name, not the client's
        self.assertEqual(out[0].project_id, str(self.project.project_id))
        ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.assertEqual(
            self._resolve_one({"type": "project", "project_id": self.project.project_id}, ctx2),
            [],
        )

    def test_deleted_missing_and_cross_team_projects_are_dropped(self):
        self.assertEqual(self._resolve_one({"type": "project", "project_id": 999_999}), [])
        self.project.is_deleted = True
        self.project.save(update_fields=["is_deleted"])
        self.assertEqual(
            self._resolve_one({"type": "project", "project_id": self.project.project_id}), []
        )
        self.project.is_deleted = False
        self.project.save(update_fields=["is_deleted"])
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        ctx_other = ToolContext(team_id=str(other_team.team_id), user_id=str(self.user.id))
        self.assertEqual(
            self._resolve_one(
                {"type": "project", "project_id": self.project.project_id}, ctx_other
            ),
            [],
        )

    def test_drops_are_logged(self):
        with self.assertLogs("origin.search_engine.agent.mentions", level="INFO"):
            self._resolve_one({"type": "task", "task_id": 999_999})


class BuildBlocksTests(SimpleTestCase):
    def _resolved(self):
        return [
            ResolvedMention(kind="user", label="Ken Sato", user_id="8f3c-uuid"),
            ResolvedMention(kind="task", label="API v2 rollout", task_id=123, display_id="WRD-5"),
            ResolvedMention(
                kind="note", label="Meeting minutes", note_type_label="personal", note_id=50
            ),
            ResolvedMention(
                kind="chat", label="backend-team", chat_type_label="gm", chat_id="abc-uuid"
            ),
            ResolvedMention(kind="project", label="Website Redesign", project_id="77"),
        ]

    def test_system_extra_uses_citation_grammar_and_tool_nudges(self):
        block = build_mention_system_extra(self._resolved())
        self.assertIn("USER-PROVIDED REFERENCES", block)
        self.assertIn("task:123", block)
        self.assertIn("fetch_task(task_id=123)", block)
        self.assertIn("note:personal:50", block)
        self.assertIn("fetch_note(note_type='personal', note_id=50)", block)
        self.assertIn("chat:gm:abc-uuid", block)
        self.assertIn("fetch_chat_thread(chat_type='gm', chat_id='abc-uuid')", block)
        self.assertIn("list_tasks(assignee_id='8f3c-uuid')", block)
        self.assertIn("project:77", block)
        self.assertIn("list_tasks(project_id=77)", block)
        self.assertIn("get_project_summary(project_id=77)", block)
        # The injection guard line must always close the block.
        self.assertIn("not instructions", block)

    def test_empty_resolution_yields_no_block(self):
        self.assertIsNone(build_mention_system_extra([]))

    def test_seed_sources_skip_user_mentions(self):
        seeds = build_mention_seed_sources(self._resolved())
        self.assertEqual(len(seeds), 4)  # user mention → no chip
        by_type = {s["entity_type"]: s for s in seeds}
        self.assertEqual(by_type["task"]["entity_id"], "task:123")
        self.assertEqual(by_type["task"]["task_display_id"], "WRD-5")
        self.assertEqual(by_type["note"]["entity_id"], "note:personal:50")
        # Chunker convention: chat entity_id carries no "chat:" prefix.
        self.assertEqual(by_type["chat"]["entity_id"], "gm:abc-uuid")
        self.assertEqual(by_type["chat"]["title"], "backend-team")
        self.assertEqual(by_type["project"]["entity_id"], "project:77")
        self.assertEqual(by_type["project"]["title"], "Website Redesign")


class AskViewMentionTests(BaseAPITestCase):
    """View wiring: request → parse/resolve → run_agent kwargs + AgentRun row.

    `_stream_ndjson` is stubbed to capture the worker closure without
    spawning the real streaming thread; the test then invokes the worker
    synchronously so the (also patched) `run_agent` records its kwargs.
    """

    def setUp(self):
        super().setUp()
        self.task = TaskMaster.objects.create(
            team=self.team, title="Fix login flow", assignee=self.user, reporter=self.user
        )
        self.note2 = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Private note", body=[]
        )
        self.authenticate()

    def _post_ask(self, payload):
        captured: dict = {}

        def fake_stream(worker, **kwargs):
            captured["worker"] = worker
            return iter([b""])

        def fake_run_agent(query, ctx, emit, **kwargs):
            captured["kwargs"] = kwargs
            emit({"type": "done"})

        with (
            patch("origin.search_engine.agent_views._stream_ndjson", side_effect=fake_stream),
            patch("origin.search_engine.agent_views.run_agent", side_effect=fake_run_agent),
            patch(
                "origin.search_engine.agent_views.check_remaining",
                return_value=(True, 0, None),
            ),
        ):
            resp = self.client.post(ASK_URL, payload, format="json")
            if "worker" in captured:
                captured["worker"](lambda event: None)
        return resp, captured

    def _base_payload(self, **extra):
        return {"query": "What about it?", "team_id": str(self.team.team_id), **extra}

    def test_no_mentions_key_leaves_run_unchanged(self):
        resp, captured = self._post_ask(self._base_payload())
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(captured["kwargs"]["system_extra"])
        self.assertIsNone(captured["kwargs"]["seed_sources"])
        self.assertEqual(AgentRun.objects.latest("started_at").mentions, [])

    def test_malformed_mentions_payloads_return_400(self):
        resp, _ = self._post_ask(self._base_payload(mentions={"type": "task"}))
        self.assertEqual(resp.status_code, 400)
        resp, _ = self._post_ask(
            self._base_payload(mentions=[{"type": "task", "task_id": i} for i in range(21)])
        )
        self.assertEqual(resp.status_code, 400)

    def test_task_mention_reaches_run_agent_and_is_persisted(self):
        resp, captured = self._post_ask(
            self._base_payload(
                query="Status of #Fix login flow?",
                mentions=[{"type": "task", "task_id": self.task.task_id, "label": "spoof"}],
            )
        )
        self.assertEqual(resp.status_code, 200)
        extra = captured["kwargs"]["system_extra"]
        self.assertIn("USER-PROVIDED REFERENCES", extra)
        self.assertIn(f"task:{self.task.task_id}", extra)
        self.assertIn("Fix login flow", extra)  # canonical DB title
        self.assertNotIn("spoof", extra)  # client label never reaches the prompt
        seeds = captured["kwargs"]["seed_sources"]
        self.assertEqual([s["entity_id"] for s in seeds], [f"task:{self.task.task_id}"])
        run = AgentRun.objects.latest("started_at")
        self.assertEqual(run.mentions[0]["kind"], "task")
        self.assertEqual(run.mentions[0]["label"], "Fix login flow")

    def test_unauthorized_mention_is_silently_dropped(self):
        resp, captured = self._post_ask(
            self._base_payload(
                mentions=[{"type": "note", "note_type": 1, "note_id": self.note2.note_id}]
            )
        )
        self.assertEqual(resp.status_code, 200)  # never a 403
        self.assertIsNone(captured["kwargs"]["system_extra"])
        self.assertIsNone(captured["kwargs"]["seed_sources"])
        self.assertEqual(AgentRun.objects.latest("started_at").mentions, [])

    def test_mentions_append_to_thread_context_block(self):
        channel = Channel.objects.create(team=self.team, kind=2, title="general")
        ChannelMember.objects.create(channel=channel, user=self.user, role="owner")
        with patch(
            "origin.search_engine.agent_views.load_or_generate_for_ask",
            return_value="THREAD SUMMARY TEXT",
        ):
            resp, captured = self._post_ask(
                self._base_payload(
                    thread_context={
                        "chat_type": 2,
                        "chat_id": str(channel.id),
                        "thread_id": str(channel.id),
                    },
                    mentions=[{"type": "task", "task_id": self.task.task_id}],
                )
            )
        self.assertEqual(resp.status_code, 200)
        extra = captured["kwargs"]["system_extra"]
        self.assertIn("<thread_summary>", extra)
        self.assertIn("USER-PROVIDED REFERENCES", extra)
        # Both the thread chip and the mention chip are seeded.
        seeds = captured["kwargs"]["seed_sources"]
        self.assertEqual(len(seeds), 2)
        self.assertEqual(seeds[1]["entity_id"], f"task:{self.task.task_id}")
