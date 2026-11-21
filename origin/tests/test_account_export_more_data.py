"""MIP-1795: the export carries the rest of "the data you created" as ADDITIONAL keys.

Pinned: the keys that existed before are unchanged, only the person's own rows appear (a
reaction they made, a task-history row they caused, never one on or by somebody else), and
no token, secret or hash is ever written.
"""

import json
from unittest import mock

from oauth2_provider.models import Application

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageAttachment,
    MessageReaction,
)
from origin.models.common.api_key_models import ApiKey
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.security_event_models import SecurityAction, SecurityEvent, SecuritySource
from origin.models.common.user_models import ConnectedAccount
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskBodyAttachmentFact, TaskMaster
from origin.search_engine.models import AgentRun, AgentRunFeedback
from origin.services import account_export
from origin.services.account_export import build_export
from origin.tests.test_base import BaseAPITestCase

OLD_KEYS = [
    "export_format_version",
    "generated_at",
    "scope",
    "account",
    "notification_preferences",
    "teams",
    "personal_notes",
    "todos",
    "schedules",
    "schedule_defaults_seeded",
    "schedule_reports",
    "mcp_use",
    "activity",
    "ai_app_notices",
    "item_activity",
    "importance",
    "tasks",
    "messages",
    "task_comments",
    "ai_conversations",
    "counts",
]
OLD_COUNT_KEYS = [
    "teams",
    "personal_notes",
    "todo_days",
    "schedules",
    "schedule_reports",
    "mcp_use",
    "activity",
    "ai_app_notices",
    "item_activity",
    "importance_marks",
    "tasks",
    "messages",
    "task_comments",
    "ai_conversations",
]
NEW_KEYS = [
    "ai_questions",
    "ai_feedback",
    "message_reactions",
    "message_attachments",
    "task_body_attachments",
    "task_history",
    "security_events",
    "api_keys",
    "connected_apps",
    "connected_accounts",
    "more_counts",
    "export_limits",
]


class MoreDataBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="PP",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="t",
            status="Open",
            assignee=self.user,
            reporter=self.user,
        )
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="gm", owner=self.user
        )
        for u in (self.user, self.user2):
            ChannelMember.objects.create(channel=self.channel, user=u, role="member")
        self.mine = Message.objects.create(
            channel=self.channel, sender=self.user, seq=1, body={"text": "a"}, body_text="a"
        )
        self.theirs = Message.objects.create(
            channel=self.channel, sender=self.user2, seq=2, body={"text": "b"}, body_text="b"
        )


class ExistingKeysPinnedTests(MoreDataBase):
    def test_old_keys_and_counts_are_unchanged_and_new_ones_only_added(self):
        doc = build_export(self.user)
        self.assertEqual(list(doc)[: len(OLD_KEYS)], OLD_KEYS)
        self.assertEqual(list(doc)[len(OLD_KEYS) :], NEW_KEYS)
        self.assertEqual(list(doc["counts"]), OLD_COUNT_KEYS)
        json.dumps(doc)

    def test_empty_account_has_empty_sets(self):
        doc = build_export(self.user)
        for key in NEW_KEYS[:-2]:
            self.assertEqual(doc[key], [], key)


class AiTests(MoreDataBase):
    def test_own_questions_answers_and_feedback_only(self):
        run = AgentRun.objects.create(
            team_id=str(self.team.pk),
            user_id=str(self.user.id),
            query="what is due?",
            final_answer_text="two tasks",
            status="done",
        )
        other = AgentRun.objects.create(
            team_id=str(self.team.pk),
            user_id=str(self.user2.id),
            query="secret q",
            final_answer_text="secret a",
            status="done",
        )
        AgentRunFeedback.objects.create(
            run=run, team_id=str(self.team.pk), user_id=str(self.user.id), rating=1, comment="nice"
        )
        AgentRunFeedback.objects.create(
            run=run, team_id=str(self.team.pk), user_id=str(self.user2.id), rating=-1, comment="bad"
        )
        AgentRunFeedback.objects.create(
            run=other, team_id=str(self.team.pk), user_id=str(self.user2.id), rating=1
        )
        doc = build_export(self.user)
        self.assertEqual(
            [(r["question"], r["answer"]) for r in doc["ai_questions"]],
            [("what is due?", "two tasks")],
        )
        self.assertEqual([(f["rating"], f["comment"]) for f in doc["ai_feedback"]], [(1, "nice")])
        self.assertNotIn("secret", json.dumps(doc))
        self.assertEqual(doc["more_counts"]["ai_questions"], 1)


class ReactionAttachmentTests(MoreDataBase):
    def test_reactions_the_person_made_not_reactions_on_their_messages(self):
        MessageReaction.objects.create(message=self.theirs, user=self.user, emoji="👍")
        MessageReaction.objects.create(message=self.mine, user=self.user2, emoji="🎉")
        doc = build_export(self.user)
        self.assertEqual([r["emoji"] for r in doc["message_reactions"]], ["👍"])
        self.assertNotIn("🎉", json.dumps(doc, ensure_ascii=False))

    def test_attachment_metadata_without_file_or_path(self):
        MessageAttachment.objects.create(
            message=self.mine,
            uploader=self.user,
            file="chats/x/secret-path.png",
            original_name="plan.png",
            mime="image/png",
            size_bytes=12,
        )
        MessageAttachment.objects.create(
            message=self.theirs,
            uploader=self.user2,
            file="chats/x/other.png",
            original_name="other.png",
            size_bytes=5,
        )
        TaskBodyAttachmentFact.objects.create(
            task=self.task, uploader=self.user, body_attachment_url="tasks/1/img.png"
        )
        TaskBodyAttachmentFact.objects.create(
            task=self.task, uploader=self.user2, body_attachment_url="tasks/1/other.png"
        )
        doc = build_export(self.user)
        att = doc["message_attachments"]
        self.assertEqual(
            [(a["name"], a["size_bytes"], a["mime"]) for a in att], [("plan.png", 12, "image/png")]
        )
        self.assertEqual(att[0]["message_id"], str(self.mine.id))
        self.assertEqual([a["task_id"] for a in doc["task_body_attachments"]], [self.task.task_id])
        blob = json.dumps(doc)
        self.assertNotIn("secret-path", blob)
        self.assertNotIn("other.png", blob)


class TaskHistoryTests(MoreDataBase):
    def test_only_rows_the_person_caused_and_never_another_persons_identity(self):
        TaskActivity.objects.all().delete()
        TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=self.user,
            action_type="status_changed",
            field_name="status",
            old_value="Open",
            new_value="WIP",
        )
        TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=self.user,
            action_type="assignee_changed",
            field_name="assignee",
            old_value=str(self.user.id),
            new_value="someone-elses-name",
        )
        TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=self.user2,
            action_type="status_changed",
            field_name="status",
            old_value="WIP",
            new_value="Closed",
        )
        doc = build_export(self.user)
        rows = doc["task_history"]
        self.assertEqual([r["action"] for r in rows], ["status_changed", "assignee_changed"])
        self.assertEqual((rows[0]["old_value"], rows[0]["new_value"]), ("Open", "WIP"))
        self.assertNotIn("old_value", rows[1])
        self.assertNotIn("someone-elses-name", json.dumps(doc))


class SecurityKeysAppsTests(MoreDataBase):
    def test_security_events_are_the_persons_own(self):
        SecurityEvent.objects.create(
            action=SecurityAction.SIGNIN_SUCCEEDED,
            actor_id=self.user.id,
            source=SecuritySource.APP,
            ip="203.0.113.5",
            user_agent="UA-mine",
            target_label="x@other.example",
        )
        SecurityEvent.objects.create(
            action=SecurityAction.SIGNIN_SUCCEEDED,
            actor_id=self.user2.id,
            source=SecuritySource.APP,
            ip="198.51.100.9",
            user_agent="UA-theirs",
        )
        doc = build_export(self.user)
        self.assertEqual([e["ip"] for e in doc["security_events"]], ["203.0.113.5"])
        blob = json.dumps(doc)
        self.assertNotIn("UA-theirs", blob)
        self.assertNotIn("x@other.example", blob)

    def test_api_keys_metadata_never_the_secret_or_hash(self):
        ApiKey.objects.create(
            user=self.user, name="ci", key_hash="h" * 64, prefix="gnk_abcd", scope="read"
        )
        ApiKey.objects.create(
            user=self.user2, name="theirs", key_hash="g" * 64, prefix="gnk_zzzz", scope="read"
        )
        doc = build_export(self.user)
        self.assertEqual([(k["name"], k["prefix"]) for k in doc["api_keys"]], [("ci", "gnk_abcd")])
        blob = json.dumps(doc)
        self.assertNotIn("h" * 64, blob)
        self.assertNotIn("key_hash", blob)
        self.assertNotIn("theirs", blob)

    def test_connected_apps_and_accounts_have_no_tokens(self):
        app = Application.objects.create(
            client_id="claude-x",
            name="Claude",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://example.com/cb",
        )
        McpConnection.objects.create(
            user=self.user,
            application=app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            scope="write",
        )
        McpConnection.objects.create(
            user=self.user2,
            application=app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            scope="read",
        )
        ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="42",
            scopes=["repo"],
            access_token_encrypted="TOKEN-SECRET",
            refresh_token_encrypted="REFRESH-SECRET",
        )
        doc = build_export(self.user)
        self.assertEqual(
            [(c["app"], c["scope"]) for c in doc["connected_apps"]], [("Claude", "write")]
        )
        self.assertEqual(
            [(c["provider"], c["scopes"]) for c in doc["connected_accounts"]],
            [("github", ["repo"])],
        )
        blob = json.dumps(doc)
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("token", blob.replace("export_limits", ""))


class SizeGuardTests(MoreDataBase):
    def test_a_huge_set_is_capped_and_says_so(self):
        for i in range(5):
            MessageReaction.objects.create(message=self.theirs, user=self.user, emoji=f"e{i}")
        with mock.patch.dict(account_export.ROW_LIMITS, {"message_reactions": 3}):
            doc = build_export(self.user)
        self.assertEqual(len(doc["message_reactions"]), 3)
        self.assertEqual(doc["more_counts"]["message_reactions"], 3)
        self.assertEqual(doc["export_limits"]["message_reactions"], {"limit": 3, "truncated": True})
        self.assertFalse(doc["export_limits"]["api_keys"]["truncated"])


class DeletedAccountTests(MoreDataBase):
    def test_a_deleted_account_still_builds(self):
        from django.contrib.auth import get_user_model

        get_user_model().objects.filter(pk=self.user.pk).update(is_active=False)
        self.user.refresh_from_db()
        doc = build_export(self.user)
        self.assertEqual(list(doc)[: len(OLD_KEYS)], OLD_KEYS)
