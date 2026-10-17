"""`update_task` must push a rewritten body into the task's Yjs doc (MIP-694).

The editor renders the collab doc `task-body:<id>`, not `TaskMaster.content`,
and seeds from `content` only while that doc is empty. So an agent / MCP body
rewrite of a task that had ever been opened never reached the screen, and the
next keystroke autosaved the old body back. The tool now posts the new blocks
to genos-collab after commit; these tests pin when it does and what it sends.
"""

from unittest import mock

import jwt
import requests
from django.conf import settings
from django.db import transaction
from django.test import override_settings

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.services import collab_push
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase

COLLAB = "http://collab.internal:8891"


@override_settings(COLLAB_INTERNAL_URL=COLLAB)
class UpdateTaskCollabPushTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Implementation",
            status="Open",
            priority="Normal",
        )
        patcher = mock.patch.object(collab_push.requests, "post")
        self.post = patcher.start()
        self.post.return_value = mock.Mock(status_code=200, text='{"ok":true}')
        self.addCleanup(patcher.stop)

    def _update(self, **args):
        # The test case wraps each test in a transaction that never commits,
        # so run on_commit callbacks explicitly, as a real commit would.
        with self.captureOnCommitCallbacks(execute=True):
            return UPDATE_TASK.run({"task_id": self.task.task_id, **args}, self.ctx)

    def test_body_rewrite_pushes_the_stored_blocks(self):
        self._update(
            content_text="## Plan\n\nRun `make` with **care**.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
        )

        self.post.assert_called_once()
        url = self.post.call_args.args[0]
        payload = self.post.call_args.kwargs["json"]
        self.assertEqual(url, f"{COLLAB}/internal/documents/replace-blocks")
        self.assertEqual(payload["documentName"], f"task-body:{self.task.task_id}")
        self.task.refresh_from_db()
        # Exactly what was saved, so the doc and the row agree.
        self.assertEqual(payload["blocks"], self.task.content)
        self.assertEqual(
            [b["type"] for b in payload["blocks"]], ["heading", "paragraph", "table", "paragraph"]
        )

    def test_token_is_internal_audience_signed_with_shared_secret(self):
        self._update(content_text="new body")

        header = self.post.call_args.kwargs["headers"]["Authorization"]
        self.assertTrue(header.startswith("Bearer "))
        claims = jwt.decode(
            header.removeprefix("Bearer "),
            settings.SIMPLE_JWT["SIGNING_KEY"],
            algorithms=["HS256"],
            audience=collab_push.INTERNAL_AUDIENCE,
        )
        # Not a user token: no identity, short-lived.
        self.assertNotIn("user_id", claims)
        self.assertNotIn("token_type", claims)
        self.assertLessEqual(claims["exp"] - claims["iat"], 60)

    def test_clearing_the_body_pushes_an_empty_list(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(content=[{"type": "paragraph"}])
        self._update(content_text="")
        self.assertEqual(self.post.call_args.kwargs["json"]["blocks"], [])

    def test_non_body_updates_do_not_push(self):
        self._update(status="WIP", priority="High", title="Renamed")
        self.post.assert_not_called()

    def test_resending_the_same_body_pushes_again(self):
        """Block ids are fresh UUIDs per parse, so a resend is a content
        change. That is the repair path for a doc that went stale before
        this push existed: write the same body again and the editor
        catches up."""
        self._update(content_text="same body")
        self.post.reset_mock()
        self._update(content_text="same body")
        self.post.assert_called_once()

    def test_rolled_back_write_does_not_push(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            try:
                with transaction.atomic():
                    UPDATE_TASK.run(
                        {"task_id": self.task.task_id, "content_text": "doomed"}, self.ctx
                    )
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        # Django discards on_commit callbacks registered inside a rolled-back
        # atomic block, so none reached the outer transaction.
        self.assertEqual(callbacks, [])
        self.post.assert_not_called()

    def test_collab_outage_does_not_fail_the_write(self):
        self.post.side_effect = requests.ConnectionError("collab down")
        with self.assertLogs("origin.services.collab_push", level="WARNING"):
            result = self._update(content_text="saved anyway")
        self.assertEqual(result["changed_fields"], ["content"])
        self.task.refresh_from_db()
        self.assertEqual(self.task.content[0]["content"][0]["text"], "saved anyway")

    def test_collab_refusal_is_logged(self):
        self.post.return_value = mock.Mock(status_code=401, text='{"error":"invalid token"}')
        with self.assertLogs("origin.services.collab_push", level="WARNING") as logs:
            self._update(content_text="refused")
        self.assertIn("status=401", logs.output[0])

    @override_settings(COLLAB_INTERNAL_URL="")
    def test_unconfigured_collab_is_a_no_op(self):
        self._update(content_text="no collab here")
        self.post.assert_not_called()

    def test_milestone_body_pushes_what_the_backing_row_stored(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="v1.0",
            status="Open",
            priority="Normal",
        )
        backing = ensure_backing_task(milestone)
        with self.captureOnCommitCallbacks(execute=True):
            UPDATE_TASK.run(
                {"task_id": backing.task_id, "content_text": "# Goal\n\nShip it."}, self.ctx
            )

        payload = self.post.call_args.kwargs["json"]
        backing.refresh_from_db()
        self.assertEqual(payload["documentName"], f"task-body:{backing.task_id}")
        self.assertEqual(payload["blocks"], backing.content)
        self.assertEqual(payload["blocks"][0]["type"], "heading")
