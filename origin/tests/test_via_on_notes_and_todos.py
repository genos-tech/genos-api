"""MIP-1488: a note version and a to-do record how the write that made them arrived (`via`).

Tasks stamp a `via` on every activity row (`task_signals._record`, `get_current_source`). Two
surfaces had nowhere to keep one: the note history (`NoteVersionMaster`) and the to-do list
(`ToDoItem`, which has no per-write record at all: its row is overwritten in place).

What is recorded: ONLY a call that came through an API key or a connected app (the MCP kinds,
`api_key` and `oauth`). The web app, the in-app Genos AI (Ken, 2026-11-14: no chip), an Undo click,
a timer and a command leave `NULL`, which is what every row written before this had.

Who reads what: the key's or app's NAME is its owner's (`via_for_viewer`, the one rule every
surface shares); everyone else gets `{kind}` and, for a connected app, the official `app` family.
A key writes as its owner, so a note version's actor is its `editor`; a to-do's is its owner.
"""

from __future__ import annotations

import json
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.middleware.current_user import clear_current_user, set_current_user, via_override
from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.fetch_note_history import FETCH_NOTE_HISTORY
from origin.serializers.chat.todo_serializers import ToDoGroupSerializer, ToDoItemSerializer
from origin.serializers.note.note_serializers import (
    NoteVersionDetailSerializer,
    NoteVersionListItemSerializer,
)
from origin.services.collab_push import PushOutcome
from origin.tests.test_item_lines import LineBase
from origin.tests.test_mcp_endpoint import McpBase
from origin.tests.test_mcp_oauth_flow import FlowBase
from origin.views.utils.note_role import ROLE_OWNER, ROLE_VIEWER
from origin.views.utils.note_version import snapshot_note_version

PERSONAL = 1
PUSH = "origin.services.collab_push.push_note_body_now"
PUSH_IN_TOOL = "origin.search_engine.agent.tools.update_note.push_note_body_now"
PUSH_IN_RESTORE = "origin.search_engine.agent.tools.restore_note_version.push_note_body_now"

KEY = {
    "kind": "api_key",
    "key_id": "k-alice-1",
    "key_name": "Alice private phone",
    "key_prefix": "gk_alicepfx",
}
APP = {
    "kind": "oauth",
    "connection_id": "conn-alice-9",
    "client_name": "Alice Claude Desktop",
    "app": "claude",
}
SECRETS = (
    "Alice private phone",
    "k-alice-1",
    "gk_alicepfx",
    "conn-alice-9",
    "Alice Claude Desktop",
)
PLANTED = {"kind": "api_key", "key_id": "planted-id", "key_name": "planted-name"}


def leaks(payload) -> list[str]:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return [s for s in SECRETS if s in text]


def blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text, "styles": {}}]}]


class _NoteBase(McpBase):
    def setUp(self):
        super().setUp()
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan", body=blocks("first")
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.note.note_id,
            note_type=PERSONAL,
            role_id=ROLE_OWNER,
        )
        for target in (PUSH, PUSH_IN_TOOL, PUSH_IN_RESTORE):
            patcher = mock.patch(target, return_value=PushOutcome.SENT)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(clear_current_user)

    def rows(self):
        return list(
            NoteVersionMaster.objects.filter(
                note_type=PERSONAL, note_id=self.note.note_id
            ).order_by("version_no")
        )

    def last(self):
        return self.rows()[-1]

    def share(self, user, role=ROLE_VIEWER):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=user,
            note_id=self.note.note_id,
            note_type=PERSONAL,
            role_id=role,
        )

    def update_args(self, text="written by the AI app"):
        return {"note_type": "personal", "note_id": self.note.note_id, "content_text": text}

    def snap(self, text="x", *, editor=None, via=None, **kw):
        row = snapshot_note_version(
            team=self.team,
            editor=editor or self.user,
            note_type=PERSONAL,
            note_id=self.note.note_id,
            title="Plan",
            body=blocks(text),
            **kw,
        )
        if via is not None:
            NoteVersionMaster.objects.filter(pk=row.pk).update(via=via)
            row.refresh_from_db()
        return row


class TheNoteWritePaths(_NoteBase):
    """Every path that saves a note version, and what each one records."""

    def test_an_api_key_update_note_records_the_key(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._tool_text(self._call("update_note", self.update_args()))
        self.assertFalse(is_error, text)
        row = self.last()
        self.assertEqual(row.via["kind"], "api_key")
        self.assertEqual(row.via["key_name"], "mcp")
        self.assertEqual(row.editor_id, self.user.id)

    def test_the_copy_kept_before_the_edit_is_not_the_edits_work(self):
        # `keep_current_state` saves what the note said before: nobody just made it.
        self._key(scope=SCOPE_WRITE)
        self._call("update_note", self.update_args())
        kept, edit = self.rows()
        self.assertIsNone(kept.editor_id)
        self.assertIsNone(kept.via, "what was there is not marked as the AI app's text")
        self.assertEqual(edit.via["kind"], "api_key")

    def test_a_restore_over_mcp_records_the_key(self):
        self.snap("v1", force_new=True)
        self.snap("v2", force_new=True)
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._tool_text(
            self._call(
                "restore_note_version",
                {"note_type": "personal", "note_id": self.note.note_id, "version_no": 1},
            )
        )
        self.assertFalse(is_error, text)
        row = self.last()
        self.assertEqual(row.restored_from_version_no, 1)
        self.assertEqual(row.via["kind"], "api_key")

    def test_create_note_over_mcp_records_the_key_on_version_one(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._tool_text(
            self._call(
                "create_note",
                {"note_type": "personal", "title": "From the AI", "content_text": "hello"},
            )
        )
        self.assertFalse(is_error, text)
        row = NoteVersionMaster.objects.filter(title="From the AI").get()
        self.assertEqual(row.via["kind"], "api_key")

    def test_the_web_app_records_nothing(self):
        self.authenticate()
        res = self.client.put(
            "/api/v2/note/personal/",
            {"note_id": self.note.note_id, "title": "Plan", "body": blocks("typed")},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIsNone(self.last().via)

    def test_the_in_app_ai_records_nothing(self):
        # Ken, 2026-11-14: the in-app Genos AI is not MCP and gets no chip.
        set_current_user(self.user, via={"kind": "agent"})
        update = REGISTRY["update_note"]
        update.run(self.update_args(), ToolContext(str(self.team.team_id), str(self.user.id)))
        self.assertTrue(self.rows())
        self.assertTrue(all(r.via is None for r in self.rows()))

    def test_an_undo_click_records_nothing(self):
        with via_override({"kind": "app", "undo_of": {"run_id": "r-1", "step": 3}}):
            self.snap("undone", force_new=True)
        self.assertIsNone(self.last().via)

    def test_a_kind_that_is_not_mcp_is_not_recorded(self):
        for kind in ("app", "agent", "github", "pr_merge", "unknown", "future_kind"):
            set_current_user(self.user, via={"kind": kind, "github_user": "x"})
            self.snap(kind, force_new=True)
        self.assertTrue(all(r.via is None for r in self.rows()))

    def test_a_write_with_no_caller_records_nothing(self):
        clear_current_user()
        self.snap("a command", force_new=True)
        self.assertIsNone(self.last().via)

    def test_old_rows_are_null_and_read_as_before(self):
        row = self.snap("before this existed")
        self.assertIsNone(row.via)
        out = NoteVersionListItemSerializer(row, context={"viewer_id": self.user.id}).data
        self.assertIsNone(out["via"])


class TheNoteWriteOverOauth(FlowBase):
    def test_a_connected_app_records_its_family_and_its_name(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan", body=blocks("first")
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=note.note_id,
            note_type=PERSONAL,
            role_id=ROLE_OWNER,
        )
        token = self.tokens()["access_token"]
        with (
            mock.patch(PUSH_IN_TOOL, return_value=PushOutcome.SENT),
            mock.patch(PUSH, return_value=PushOutcome.SENT),
        ):
            text, is_error = self._tool_text(
                self.call(
                    token,
                    "update_note",
                    {"note_type": "personal", "note_id": note.note_id, "content_text": "hi"},
                )
            )
        self.assertFalse(is_error, text)
        row = NoteVersionMaster.objects.filter(note_id=note.note_id).order_by("version_no").last()
        self.assertEqual(row.via["kind"], "oauth")
        self.assertEqual(row.via["app"], "claude")
        self.assertEqual(row.via["client_name"], "Claude")


class TheNoteCoalescing(_NoteBase):
    """A person's autosave within five minutes overwrites the newest row of theirs. It must
    never overwrite a row the AI app wrote, or the row would show the AI app's chip on text the
    person typed (or hide the chip on the AI app's text)."""

    def test_an_autosave_after_an_mcp_edit_is_a_new_row(self):
        self._key(scope=SCOPE_WRITE)
        self._call("update_note", self.update_args())
        mcp_row = self.last()
        self.authenticate()
        res = self.client.put(
            "/api/v2/note/personal/",
            {"note_id": self.note.note_id, "title": "Plan", "body": blocks("typed after")},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        mcp_row.refresh_from_db()
        self.assertEqual(mcp_row.via["kind"], "api_key")
        self.assertIn("written by the AI app", json.dumps(mcp_row.body))
        newest = self.last()
        self.assertNotEqual(newest.pk, mcp_row.pk)
        self.assertIsNone(newest.via)

    def test_two_autosaves_still_coalesce(self):
        a = self.snap("one")
        b = self.snap("two")
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(len(self.rows()), 1)

    def test_an_app_write_never_stamps_a_row_that_was_written_by_a_person(self):
        a = self.snap("typed")
        set_current_user(self.user, via=KEY)
        b = self.snap("by the key")
        self.assertNotEqual(a.pk, b.pk)
        a.refresh_from_db()
        self.assertIsNone(a.via)
        self.assertEqual(b.via["kind"], "api_key")


class TheNoteReaders(_NoteBase):
    LIST = "/api/v2/note/version/all/"
    DETAIL = "/api/v2/note/version/"

    def setUp(self):
        super().setUp()
        self.share(self.user2)
        self.mine = self.snap("by my key", editor=self.user, via=APP, force_new=True)
        self.key_row = self.snap("by my other key", editor=self.user, via=KEY, force_new=True)
        self.old = self.snap("old", editor=self.user, force_new=True)

    def params(self, **extra):
        return {
            "team_id": str(self.team.team_id),
            "note_type": PERSONAL,
            "note_id": self.note.note_id,
            **extra,
        }

    def listing(self, viewer):
        self.authenticate(viewer)
        res = self.client.get(self.LIST, self.params())
        self.assertEqual(res.status_code, 200, res.content)
        return {row["versionNo"]: row for row in res.data}

    def test_a_colleague_reads_the_kind_and_the_family_and_no_secret(self):
        rows = self.listing(self.user2)
        self.assertEqual(rows[self.mine.version_no]["via"], {"kind": "oauth", "app": "claude"})
        self.assertEqual(rows[self.key_row.version_no]["via"], {"kind": "api_key"})
        self.assertEqual(leaks(list(rows.values())), [])

    def test_the_owner_reads_it_whole(self):
        rows = self.listing(self.user)
        self.assertEqual(rows[self.mine.version_no]["via"], APP)
        self.assertEqual(rows[self.key_row.version_no]["via"], KEY)

    def test_an_old_row_reads_null(self):
        for viewer in (self.user, self.user2):
            self.assertIsNone(self.listing(viewer)[self.old.version_no]["via"])

    def test_the_detail_view_reduces_the_same_way(self):
        self.authenticate(self.user2)
        res = self.client.get(self.DETAIL, self.params(version_no=self.key_row.version_no))
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["via"], {"kind": "api_key"})
        self.assertEqual(leaks(res.data), [])
        self.authenticate(self.user)
        res = self.client.get(self.DETAIL, self.params(version_no=self.key_row.version_no))
        self.assertEqual(res.data["via"], KEY)

    def test_a_restore_answers_with_the_same_reduction(self):
        # The viewer is the person restoring; the new row is theirs and carries no via.
        NotePermissionMaster.objects.filter(user=self.user2, note_id=self.note.note_id).update(
            role_id=ROLE_OWNER
        )
        self.authenticate(self.user2)
        res = self.client.post(
            "/api/v2/note/version/restore/",
            {**self.params(), "version_no": self.key_row.version_no},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIsNone(res.data["via"])

    def test_without_a_viewer_the_serializer_fails_closed(self):
        row = NoteVersionMaster.objects.get(pk=self.key_row.pk)
        for cls in (NoteVersionListItemSerializer, NoteVersionDetailSerializer):
            self.assertEqual(cls(row).data["via"], {"kind": "api_key"})
            self.assertEqual(leaks(cls(row).data), [])

    def test_a_via_with_no_usable_kind_is_an_empty_object(self):
        row = self.snap("odd", via={"key_name": "Alice private phone"}, force_new=True)
        out = NoteVersionListItemSerializer(row, context={"viewer_id": self.user2.id}).data
        self.assertEqual(out["via"], {})

    def test_the_switch_flips_the_family_to_the_owner_only(self):
        from origin.views.public.mcp import via_privacy  # noqa: PLC0415

        with mock.patch.object(via_privacy, "SHOW_OAUTH_APP_TO_EVERYONE", False):
            rows = self.listing(self.user2)
        self.assertEqual(rows[self.mine.version_no]["via"], {"kind": "oauth"})

    def test_the_history_the_ai_reads_is_byte_identical_with_or_without_via(self):
        # Not model-visible: `get_note_history` does not tell a version's `via`.
        ctx = ToolContext(str(self.team.team_id), str(self.user.id))
        args = {"note_type": "personal", "note_id": self.note.note_id}
        with_via = json.dumps(FETCH_NOTE_HISTORY.run(args, ctx), sort_keys=True, default=str)
        NoteVersionMaster.objects.filter(note_id=self.note.note_id).update(via=None)
        without = json.dumps(FETCH_NOTE_HISTORY.run(args, ctx), sort_keys=True, default=str)
        self.assertEqual(with_via, without)
        self.assertNotIn("via", with_via)


class ThePlantedViaOnNotes(_NoteBase):
    def test_a_via_in_a_note_save_is_ignored(self):
        self.authenticate()
        res = self.client.put(
            "/api/v2/note/personal/",
            {
                "note_id": self.note.note_id,
                "title": "Plan",
                "body": blocks("typed"),
                "via": PLANTED,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIsNone(self.last().via)

    def test_a_via_in_a_restore_is_ignored(self):
        self.snap("v1", force_new=True)
        self.authenticate()
        res = self.client.post(
            "/api/v2/note/version/restore/",
            {
                "team_id": str(self.team.team_id),
                "note_type": PERSONAL,
                "note_id": self.note.note_id,
                "version_no": 1,
                "via": PLANTED,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIsNone(self.last().via)
        self.assertEqual(NoteVersionMaster.objects.exclude(via=None).count(), 0)

    def test_a_via_in_a_note_create_is_ignored(self):
        self.authenticate()
        res = self.client.post(
            "/api/v2/note/personal/",
            {"team": str(self.team.team_id), "title": "New", "body": [], "via": PLANTED},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201, 400), res.content)
        self.assertEqual(NoteVersionMaster.objects.exclude(via=None).count(), 0)

    def test_the_serializers_take_no_via(self):
        # Output only: nothing that builds a `NoteVersionMaster` from a request exists, and the
        # field is not a writable one on either serializer.
        for cls in (NoteVersionListItemSerializer, NoteVersionDetailSerializer):
            self.assertTrue(cls().fields["via"].read_only)


# --------------------------------------------------------------------------- #
# To-dos                                                                      #
# --------------------------------------------------------------------------- #


class _TodoBase(McpBase):
    def setUp(self):
        super().setUp()
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        self.addCleanup(clear_current_user)

    def create_over_mcp(self, title="Call the lawyer"):
        text, is_error = self._tool_text(self._call("create_todo_item", {"title": title}))
        self.assertFalse(is_error, text)
        return ToDoItem.objects.get(title=title)

    def item(self, title="Mine", *, via=None, user=None):
        group, _ = ToDoGroup.objects.get_or_create(
            team=self.team,
            user=user or self.user,
            local_date="2026-11-14",
            defaults={"is_completed": False},
        )
        item = ToDoItem.objects.create(group=group, title=title)
        if via is not None:
            ToDoItem.objects.filter(pk=item.pk).update(via=via)
            item.refresh_from_db()
        return item


class TheTodoWritePaths(_TodoBase):
    def test_an_api_key_create_records_the_key(self):
        self._key(scope=SCOPE_WRITE)
        item = self.create_over_mcp()
        self.assertEqual(item.via["kind"], "api_key")
        self.assertEqual(item.via["key_name"], "mcp")

    def test_a_key_update_records_the_key(self):
        item = self.item()
        self._key(scope=SCOPE_WRITE)
        text, is_error = self._tool_text(
            self._call("update_todo_item", {"item_id": item.item_id, "title": "Renamed"})
        )
        self.assertFalse(is_error, text)
        item.refresh_from_db()
        self.assertEqual(item.title, "Renamed")
        self.assertEqual(item.via["kind"], "api_key")

    def test_a_key_completing_it_records_the_key(self):
        item = self.item()
        self._key(scope=SCOPE_WRITE)
        self._call("update_todo_item", {"item_id": item.item_id, "is_completed": True})
        item.refresh_from_db()
        self.assertTrue(item.is_completed)
        self.assertEqual(item.via["kind"], "api_key")

    def test_a_reminder_alone_is_not_a_write_to_the_row(self):
        item = self.item(via=KEY)
        self._key(scope=SCOPE_WRITE)
        ToDoItem.objects.filter(pk=item.pk).update(via=None)
        text, is_error = self._tool_text(
            self._call("update_todo_item", {"item_id": item.item_id, "remind_in_minutes": 30})
        )
        self.assertFalse(is_error, text)
        item.refresh_from_db()
        self.assertIsNone(item.via)

    def test_the_in_app_ai_records_nothing(self):
        set_current_user(self.user, via={"kind": "agent"})
        out = REGISTRY["create_todo_item"].run(
            {"title": "By the in-app AI"}, ToolContext(str(self.team.team_id), str(self.user.id))
        )
        self.assertIsNone(ToDoItem.objects.get(item_id=out["item_id"]).via)

    def test_the_web_app_create_records_nothing(self):
        self.authenticate()
        res = self.client.post(
            "/api/v2/todo/items/",
            {"team_id": str(self.team.team_id), "local_date": "2026-11-14", "title": "Typed"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertIsNone(ToDoItem.objects.get(title="Typed").via)
        self.assertIsNone(res.data["via"])

    def test_a_row_saved_with_no_caller_records_nothing(self):
        clear_current_user()
        self.assertIsNone(self.item("seeded").via)

    def test_a_kind_that_is_not_mcp_is_not_recorded(self):
        for kind in ("app", "agent", "github", "future_kind"):
            set_current_user(self.user, via={"kind": kind})
            self.assertIsNone(self.item(kind).via, kind)

    def test_the_web_app_editing_the_title_after_the_ai_is_the_last_write(self):
        self._key(scope=SCOPE_WRITE)
        item = self.create_over_mcp()
        self.client.credentials()
        self.authenticate()
        res = self.client.patch(
            f"/api/v2/todo/items/{item.item_id}/", {"title": "My own words"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        item.refresh_from_db()
        self.assertIsNone(item.via)
        self.assertIsNone(res.data["via"])

    def test_a_reorder_is_not_a_write_to_the_content(self):
        self._key(scope=SCOPE_WRITE)
        item = self.create_over_mcp()
        self.client.credentials()
        self.authenticate()
        res = self.client.patch(
            f"/api/v2/todo/items/{item.item_id}/", {"sort_order": 4}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        item.refresh_from_db()
        self.assertEqual(item.sort_order, 4)
        self.assertEqual(item.via["kind"], "api_key")

    def test_a_save_that_changes_nothing_keeps_the_stamp(self):
        item = self.item(via=KEY)
        again = ToDoItem.objects.get(pk=item.pk)
        again.save()
        again.refresh_from_db()
        self.assertEqual(again.via, KEY)

    def test_the_children_of_a_retagged_parent_keep_their_own_stamp(self):
        # `cascade_category` is a queryset `.update()`: a child is not itself written by the call.
        parent = self.item("Parent")
        child = ToDoItem.objects.create(group=parent.group, parent_item=parent, title="Child")
        self._key(scope=SCOPE_WRITE)
        self._call("update_todo_item", {"item_id": parent.item_id, "category": "Legal"})
        child.refresh_from_db()
        self.assertIsNotNone(child.category_id)
        self.assertIsNone(child.via)


class TheTodoWriteOverOauth(FlowBase):
    def test_a_connected_app_records_its_family(self):
        token = self.tokens()["access_token"]
        text, is_error = self._tool_text(
            self.call(token, "create_todo_item", {"title": "From the connector"})
        )
        self.assertFalse(is_error, text)
        item = ToDoItem.objects.get(title="From the connector")
        self.assertEqual(item.via["kind"], "oauth")
        self.assertEqual(item.via["app"], "claude")


class ThePlantedViaOnTodos(_TodoBase):
    def test_a_via_in_a_create_is_ignored(self):
        self.authenticate()
        res = self.client.post(
            "/api/v2/todo/items/",
            {
                "team_id": str(self.team.team_id),
                "local_date": "2026-11-14",
                "title": "Planted",
                "via": PLANTED,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertIsNone(ToDoItem.objects.get(title="Planted").via)
        self.assertIsNone(res.data["via"])

    def test_a_via_in_a_patch_is_ignored_and_cannot_be_cleared_or_set(self):
        item = self.item(via=KEY)
        self.authenticate()
        res = self.client.patch(
            f"/api/v2/todo/items/{item.item_id}/",
            {"sort_order": 2, "via": PLANTED},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        item.refresh_from_db()
        self.assertEqual(item.via, KEY, "a reorder neither clears nor takes a client's value")
        res = self.client.patch(
            f"/api/v2/todo/items/{item.item_id}/",
            {"title": "New title", "via": PLANTED},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        item.refresh_from_db()
        self.assertIsNone(item.via)
        self.assertNotEqual(item.via, PLANTED)

    def test_the_item_serializer_takes_no_via(self):
        self.assertTrue(ToDoItemSerializer().fields["via"].read_only)


class TheTodoReaders(_TodoBase):
    GROUPS = "/api/v2/todo/groups/"

    def groups(self, viewer):
        self.authenticate(viewer)
        res = self.client.get(
            self.GROUPS,
            {"team_id": str(self.team.team_id), "from": "2026-11-01", "to": "2026-11-30"},
        )
        self.assertEqual(res.status_code, 200, res.content)
        return res.data

    def test_the_owner_reads_their_own_via_whole(self):
        self.item("By my app", via=APP)
        self.item("By my key", via=KEY)
        self.item("By hand")
        by_title = {i["title"]: i for g in self.groups(self.user) for i in g["items"]}
        self.assertEqual(by_title["By my app"]["via"], APP)
        self.assertEqual(by_title["By my key"]["via"], KEY)
        self.assertIsNone(by_title["By hand"]["via"])

    def test_a_colleague_is_never_sent_the_list(self):
        self.item("Private", via=KEY)
        self.assertEqual(self.groups(self.user2), [])

    def test_the_serializer_reduces_for_anyone_but_the_owner(self):
        item = self.item("Private", via=APP)
        shown = ToDoItemSerializer(item, context={"viewer_id": self.user2.id}).data
        self.assertEqual(shown["via"], {"kind": "oauth", "app": "claude"})
        self.assertEqual(leaks(shown), [])
        whole = ToDoItemSerializer(item, context={"viewer_id": self.user.id}).data
        self.assertEqual(whole["via"], APP)

    def test_without_a_viewer_the_serializer_fails_closed(self):
        item = self.item("Private", via=KEY)
        self.assertEqual(ToDoItemSerializer(item).data["via"], {"kind": "api_key"})
        group = ToDoGroup.objects.get(pk=item.group_id)
        self.assertEqual(leaks(ToDoGroupSerializer(group).data), [])

    def test_an_old_row_reads_null(self):
        item = self.item("Old")
        self.assertIsNone(ToDoItemSerializer(item, context={"viewer_id": self.user.id}).data["via"])

    def test_the_list_costs_no_query_per_item(self):
        for n in range(3):
            self.item(f"warm {n}", via=KEY)
        self.groups(self.user)
        with CaptureQueriesContext(connection) as few:
            self.groups(self.user)
        for n in range(12):
            self.item(f"more {n}", via=KEY)
        with CaptureQueriesContext(connection) as many:
            self.groups(self.user)
        self.assertEqual(len(many), len(few))

    def test_the_lists_the_ai_reads_are_byte_identical_with_or_without_via(self):
        # Not model-visible: neither tool tells a to-do's `via`.
        group = self.item("A", via=KEY).group
        ctx = ToolContext(str(self.team.team_id), str(self.user.id))
        outs = []
        for name in ("list_today_todos", "list_uncompleted_todos"):
            args = {"date": "2026-11-14"} if name == "list_today_todos" else {}
            outs.append(json.dumps(REGISTRY[name].run(args, ctx), sort_keys=True, default=str))
        ToDoItem.objects.filter(group=group).update(via=None)
        for name, before in zip(("list_today_todos", "list_uncompleted_todos"), outs):
            args = {"date": "2026-11-14"} if name == "list_today_todos" else {}
            after = json.dumps(REGISTRY[name].run(args, ctx), sort_keys=True, default=str)
            self.assertEqual(before, after, name)
            self.assertNotIn("via", after)


class TheOtherReadersOfNoteVersionsCarryNoVia(LineBase):
    """`get_timeline` and `get_item_line` read note versions by explicit field. A colleague's
    version written with a key (a real secret on the row) must not reach either, and the control
    is that the owner's own row does hold it."""

    def test_a_colleagues_key_write_to_a_task_note_is_not_told(self):
        from origin.models.note.task_note_models import TaskNoteMaster  # noqa: PLC0415
        from origin.services import item_lines, timeline  # noqa: PLC0415

        task = self.make_task("Ship the thing", assignee=self.user, reporter=self.user)
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=task, owner=self.user2, title="N", body=[]
        )
        row = NoteVersionMaster.objects.create(
            team=self.team,
            editor=self.user2,
            note_type=2,
            note_id=note.note_id,
            version_no=1,
            title="N",
            body=blocks("by key"),
            via=KEY,
        )
        NoteVersionMaster.objects.filter(pk=row.pk).update(ts_updated_at=self.at(10))
        self.assertIn("Alice private phone", json.dumps(row.via), "control: the secret is stored")
        events = timeline.timeline(
            self.ctx,
            "task",
            str(task.task_id),
            since=self.since.isoformat(),
            until=self.until.isoformat(),
        )
        self.assertTrue(
            any(e.get("kind") == "note_edit" for e in events["events"]), "the version was read"
        )
        self.assertEqual(leaks(events), [])
        lines = item_lines.item_lines(self.ctx, task.task_id, self.since, self.until)
        self.assertEqual(leaks(lines), [])
