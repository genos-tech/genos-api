"""A to-do written by anything but the open page is announced to its owner (MIP-1626).

The To-do page keeps its list in memory and re-reads it only when the app itself writes. A to-do
made over MCP, by the in-app agent, or by another REST client was saved and announced to nobody:
the page showed it only after a reload (measured before the change: 0 pushes for every write
below). After such a write commits, the API now posts `{userId, teamId}` to genos-sockets
(`POST /internal/broadcast/todo`), which sends `todo.changed` to that person's own `/v3` room.

These pin: every write path pushes (REST create / edit / tick / untick / reorder / move / tag /
delete / reminder, the three agent tools, MCP); the push goes to the OWNER of the to-do and never
to the caller; it carries no to-do content; it is sent once per owner per commit however many rows
changed; nothing is sent before the commit or after a rollback; a failure never costs the write;
and a delete that cascades does not cost a query per row.
"""

import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

import requests
from django.db import connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.models.common.api_key_models import SCOPE_WRITE
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.create_todo_item import CREATE_TODO_ITEM
from origin.search_engine.agent.tools.delete_todo_item import DELETE_TODO_ITEM
from origin.search_engine.agent.tools.update_todo_item import UPDATE_TODO_ITEM
from origin.services import realtime_push, todo_push, todo_reminders

from .test_base import BaseAPITestCase
from .test_mcp_endpoint import McpBase

SOCKETS = "http://sockets.internal:8889"
SECRET = "test-only-sockets-internal-secret"
URL = f"{SOCKETS}/internal/broadcast/todo"
PURGE = "origin.services.todo_items.purge_todo_item"


class PushRecorder:
    """Mocks the one outgoing call and reads what was sent."""

    def start(self, test):
        patcher = mock.patch.object(realtime_push.requests, "post")
        self.post = patcher.start()
        self.post.return_value = mock.Mock(status_code=200)
        test.addCleanup(patcher.stop)

    def sent(self):
        return [(c.args[0], json.loads(c.kwargs["data"])) for c in self.post.call_args_list]

    def bodies(self):
        return [body for url, body in self.sent() if url == URL]

    def reset(self):
        self.post.reset_mock()


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class TodoPushTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.push = PushRecorder()
        self.push.start(self)
        # What an earlier test left queued on this thread (a transaction that never committed).
        todo_push.reset_pending()
        self.addCleanup(todo_push.reset_pending)
        self.today = timezone.localdate()
        self.me = {"userId": str(self.user.id), "teamId": str(self.team.team_id)}
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.authenticate()

    # The test case wraps each test in a transaction that never commits, so run the on_commit
    # callbacks explicitly, as a real commit would.
    def commit(self, fn, *args, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            result = fn(*args, **kwargs)
        return result

    def seed(self, title="Call the lawyer", *, user=None, on=None, **kw):
        """A to-do made directly, with the push it caused dropped from the record."""
        user = user or self.user
        group, _ = ToDoGroup.objects.get_or_create(
            team=self.team, user=user, local_date=on or self.today
        )
        item = ToDoItem.objects.create(group=group, title=title, **kw)
        self.push.reset()
        return item

    def rest(self, method, path, body=None, expect=None):
        res = self.commit(getattr(self.client, method), path, body, format="json")
        if expect is not None:
            self.assertEqual(res.status_code, expect, res.data)
        return res

    def assertPushedOnceToMe(self):
        self.assertEqual(self.push.bodies(), [self.me])

    # --- the gap: every writer is covered -------------------------------------------------

    def test_a_rest_create_pushes_to_its_owner(self):
        self.rest(
            "post",
            "/api/v2/todo/items/",
            {
                "team_id": str(self.team.team_id),
                "local_date": self.today.isoformat(),
                "title": "Pay rent",
            },
            expect=201,
        )
        self.assertPushedOnceToMe()

    def test_a_rest_edit_pushes(self):
        item = self.seed()
        self.rest("patch", f"/api/v2/todo/items/{item.item_id}/", {"title": "New"}, expect=200)
        self.assertPushedOnceToMe()

    def test_ticking_and_unticking_push(self):
        item = self.seed()
        url = f"/api/v2/todo/items/{item.item_id}/"
        self.rest("patch", url, {"is_completed": True}, expect=200)
        self.assertPushedOnceToMe()
        self.push.reset()
        self.rest("patch", url, {"is_completed": False}, expect=200)
        self.assertPushedOnceToMe()

    def test_a_reorder_pushes(self):
        item = self.seed()
        self.rest("patch", f"/api/v2/todo/items/{item.item_id}/", {"sort_order": 5}, expect=200)
        self.assertPushedOnceToMe()

    def test_moving_to_another_day_pushes_once_for_the_parent_and_its_children(self):
        parent = self.seed("parent")
        for n in range(3):
            self.seed(f"kid {n}", parent_item=parent)
        with mock.patch(PURGE):
            self.rest(
                "patch",
                f"/api/v2/todo/items/{parent.item_id}/",
                {"local_date": (self.today + timedelta(days=1)).isoformat()},
                expect=200,
            )
        self.assertPushedOnceToMe()

    def test_a_tag_change_pushes(self):
        item = self.seed()
        cat = ToDoCategory.objects.create(team=self.team, user=self.user, name="Home")
        self.push.reset()
        self.rest("patch", f"/api/v2/todo/items/{item.item_id}/", {"category_id": cat.category_id})
        self.assertPushedOnceToMe()

    def test_a_rest_delete_pushes(self):
        item = self.seed()
        with mock.patch(PURGE):
            self.rest("delete", f"/api/v2/todo/items/{item.item_id}/", expect=204)
        self.assertPushedOnceToMe()

    def test_setting_and_cancelling_a_reminder_push(self):
        item = self.seed()
        at = (timezone.now() + timedelta(hours=3)).isoformat()
        self.rest("post", f"/api/v2/todo/items/{item.item_id}/reminder/", {"remindAt": at}, 201)
        self.assertPushedOnceToMe()
        self.push.reset()
        # A queryset `.update()` fires no signal: this one is announced by hand.
        self.rest("delete", f"/api/v2/todo/items/{item.item_id}/reminder/", expect=204)
        self.assertPushedOnceToMe()

    def test_cancelling_when_nothing_is_pending_pushes_nothing(self):
        item = self.seed()
        self.rest("delete", f"/api/v2/todo/items/{item.item_id}/reminder/", expect=204)
        self.assertEqual(self.push.bodies(), [])

    def test_a_tag_create_rename_and_delete_push(self):
        res = self.rest(
            "post",
            "/api/v2/todo/categories/",
            {"team_id": str(self.team.team_id), "name": "Work"},
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertPushedOnceToMe()
        cid = ToDoCategory.objects.get(name="Work").category_id
        self.push.reset()
        self.rest("patch", f"/api/v2/todo/categories/{cid}/", {"name": "Job"}, expect=200)
        self.assertPushedOnceToMe()
        self.push.reset()
        self.rest("delete", f"/api/v2/todo/categories/{cid}/", expect=204)
        self.assertPushedOnceToMe()

    def test_the_agents_create_pushes(self):
        out = self.commit(
            CREATE_TODO_ITEM.run,
            {"title": "From the agent", "category": "Ops", "remind_in_minutes": 60},
            self.ctx,
        )
        self.assertTrue(ToDoItem.objects.filter(item_id=out["item_id"]).exists())
        # The item, its new tag and its reminder are three writes and one push.
        self.assertPushedOnceToMe()

    def test_the_agents_update_pushes(self):
        item = self.seed()
        self.commit(UPDATE_TODO_ITEM.run, {"item_id": item.item_id, "is_completed": True}, self.ctx)
        self.assertPushedOnceToMe()

    def test_the_agents_reminder_cancel_pushes(self):
        item = self.seed()
        todo_reminders.set_reminder(item=item, remind_at=timezone.now() + timedelta(hours=2))
        self.push.reset()
        self.commit(
            UPDATE_TODO_ITEM.run, {"item_id": item.item_id, "cancel_reminder": True}, self.ctx
        )
        self.assertPushedOnceToMe()

    def test_the_agents_delete_pushes(self):
        item = self.seed()
        with mock.patch(PURGE):
            self.commit(DELETE_TODO_ITEM.run, {"item_id": item.item_id}, self.ctx)
        self.assertPushedOnceToMe()

    # --- what is sent ---------------------------------------------------------------------

    def test_the_push_is_signed_with_the_sockets_secret_over_timestamp_and_body(self):
        self.seed()
        self.commit(CREATE_TODO_ITEM.run, {"title": "x"}, self.ctx)
        call = self.push.post.call_args
        headers, body = call.kwargs["headers"], call.kwargs["data"]
        ts = headers["X-Genos-Timestamp"]
        want = hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Genos-Signature"], f"sha256={want}")
        self.assertFalse(call.kwargs["allow_redirects"])

    def test_no_to_do_content_leaves_the_api(self):
        self.commit(
            CREATE_TODO_ITEM.run,
            {"title": "Secret surprise party", "notes_text": "hide the cake"},
            self.ctx,
        )
        sent = self.push.post.call_args.kwargs["data"].decode()
        self.assertNotIn("Secret", sent)
        self.assertNotIn("cake", sent)
        self.assertEqual(self.push.bodies(), [self.me])

    # --- whose room ---------------------------------------------------------------------

    def test_a_to_do_written_for_someone_else_goes_to_them_not_to_the_caller(self):
        """The owner comes from the to-do's group, never from who made the request."""
        theirs = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.commit(CREATE_TODO_ITEM.run, {"title": "Theirs"}, theirs)
        self.assertEqual(
            self.push.bodies(),
            [{"userId": str(self.user2.id), "teamId": str(self.team.team_id)}],
        )

    def test_two_owners_changed_in_one_commit_each_get_their_own_push(self):
        theirs = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.captureOnCommitCallbacks(execute=True):
            CREATE_TODO_ITEM.run({"title": "mine"}, self.ctx)
            CREATE_TODO_ITEM.run({"title": "theirs"}, theirs)
        got = sorted(b["userId"] for b in self.push.bodies())
        self.assertEqual(got, sorted([str(self.user.id), str(self.user2.id)]))

    def test_someone_elses_to_do_cannot_be_edited_and_pushes_nothing(self):
        theirs = self.seed(user=self.user2)
        res = self.rest("patch", f"/api/v2/todo/items/{theirs.item_id}/", {"title": "mine"})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.push.bodies(), [])

    def test_a_read_pushes_nothing(self):
        self.seed()
        self.rest("get", f"/api/v2/todo/groups/?team_id={self.team.team_id}", None, expect=200)
        self.assertEqual(self.push.bodies(), [])

    # --- when ---------------------------------------------------------------------------

    def test_nothing_is_sent_until_the_write_commits(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            CREATE_TODO_ITEM.run({"title": "x"}, self.ctx)
        self.assertTrue(callbacks)
        self.push.post.assert_not_called()

    def test_a_burst_in_one_transaction_is_one_push(self):
        with self.captureOnCommitCallbacks(execute=True):
            for n in range(5):
                CREATE_TODO_ITEM.run({"title": f"t{n}"}, self.ctx)
        self.assertPushedOnceToMe()

    def test_a_rolled_back_write_never_pushes_and_does_not_swallow_the_next(self):
        class Boom(Exception):
            pass

        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    CREATE_TODO_ITEM.run({"title": "never"}, self.ctx)
                    raise Boom
            except Boom:
                pass
        self.assertEqual(self.push.bodies(), [])
        # The queued key of the rolled-back write must not make a later push a no-op.
        self.commit(CREATE_TODO_ITEM.run, {"title": "after"}, self.ctx)
        self.assertPushedOnceToMe()

    # --- failure and configuration ------------------------------------------------------

    def test_an_unreachable_sockets_server_does_not_fail_the_write(self):
        self.push.post.side_effect = requests.ConnectionError("down")
        with self.assertLogs(realtime_push.logger, level="WARNING"):
            out = self.commit(CREATE_TODO_ITEM.run, {"title": "still saved"}, self.ctx)
        self.assertTrue(ToDoItem.objects.filter(item_id=out["item_id"]).exists())

    def test_a_refusal_is_logged_not_raised(self):
        self.push.post.return_value = mock.Mock(status_code=404)
        with self.assertLogs(realtime_push.logger, level="WARNING"):
            self.commit(CREATE_TODO_ITEM.run, {"title": "x"}, self.ctx)

    def test_without_a_sockets_url_nothing_is_sent(self):
        with override_settings(SOCKETS_INTERNAL_URL=""):
            self.commit(CREATE_TODO_ITEM.run, {"title": "x"}, self.ctx)
        self.push.post.assert_not_called()

    def test_without_a_secret_nothing_is_sent_and_it_is_logged(self):
        with override_settings(SOCKETS_INTERNAL_SECRET=""):
            with self.assertLogs(realtime_push.logger, level="WARNING"):
                self.commit(CREATE_TODO_ITEM.run, {"title": "x"}, self.ctx)
        self.push.post.assert_not_called()

    # --- cascades -----------------------------------------------------------------------

    def _cascade_queries(self, n):
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=self.today - timedelta(days=30 + n)
        )
        ToDoItem.objects.bulk_create([ToDoItem(group=group, title=f"t{i}") for i in range(n)])
        self.push.reset()
        with self.captureOnCommitCallbacks(execute=True):
            with CaptureQueriesContext(connection) as ctx:
                group.delete()
        return len(ctx.captured_queries)

    def test_deleting_a_day_with_many_to_dos_does_not_cost_a_query_per_row(self):
        few, many = self._cascade_queries(2), self._cascade_queries(30)
        self.assertEqual(few, many)

    def test_deleting_a_day_announces_it_to_its_owner(self):
        self._cascade_queries(3)
        self.assertPushedOnceToMe()

    def test_an_account_deletion_cascade_pushes_at_most_once_per_owner_and_team(self):
        group = ToDoGroup.objects.create(team=self.team, user=self.user2, local_date=self.today)
        ToDoItem.objects.create(group=group, title="x")
        self.push.reset()
        with self.captureOnCommitCallbacks(execute=True):
            self.user2.delete()
        self.assertLessEqual(len(self.push.bodies()), 1)

    def test_a_flush_over_the_cap_announces_only_the_first_few(self):
        with mock.patch.object(todo_push, "MAX_PUSHES_PER_FLUSH", 2):
            with self.captureOnCommitCallbacks(execute=True):
                for n in range(5):
                    todo_push.note_owner(f"00000000-0000-4000-8000-00000000000{n}", None)
            self.assertEqual(len(self.push.bodies()), 2)

    def test_cancelling_reminders_by_a_generator_of_ids_still_pushes(self):
        item = self.seed()
        todo_reminders.set_reminder(item=item, remind_at=timezone.now() + timedelta(hours=2))
        self.push.reset()
        with self.captureOnCommitCallbacks(execute=True):
            todo_reminders.cancel_pending(item_ids=(i for i in [item.item_id]))
        self.assertPushedOnceToMe()


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class McpTodoPushTests(McpBase):
    """The path that started this: a to-do made by an AI app over MCP."""

    def setUp(self):
        super().setUp()
        self.push = PushRecorder()
        self.push.start(self)
        todo_push.reset_pending()
        self.addCleanup(todo_push.reset_pending)

    def test_a_to_do_created_over_mcp_is_pushed_to_its_owner(self):
        self._key(scope=SCOPE_WRITE)
        with self.captureOnCommitCallbacks(execute=True):
            text, is_error = self._tool_text(
                self._call("create_todo_item", {"title": "Reply to the design review"})
            )
        self.assertFalse(is_error, text)
        self.assertEqual(
            self.push.bodies(),
            [{"userId": str(self.user.id), "teamId": str(self.team.team_id)}],
        )

    def test_a_key_of_one_user_never_pushes_to_another(self):
        self._key(scope=SCOPE_WRITE, user=self.user2)
        with self.captureOnCommitCallbacks(execute=True):
            self._tool_text(self._call("create_todo_item", {"title": "Mine"}))
        users = {b["userId"] for b in self.push.bodies()}
        self.assertEqual(users, {str(self.user2.id)})
