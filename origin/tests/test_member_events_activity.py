"""The member's own changes that nothing else saves go to the ledger (MIP-1085).

Collaborators added or removed, dependencies added or removed, to-dos edited or deleted and
notes shared or unshared are noted as references only, in `MemberEvent`, for the member who
made the change. What is pinned, each by a test that fails when it breaks:

  * every path is covered because the hook is in the funnel they share: the REST views and
    the MCP tools (the in-app agent runs the same tools and services);
  * an event is a reference: kind, item type and id, the other end's id, who, when, how it
    arrived. Never a title: a deleted to-do's title is nowhere in the table or the export;
  * it is the acting member's, never another's; the owner filter is the reader's;
  * the switch off records nothing (not even a statement), on the row the request holds and
    in the statement itself; the PATCH that turns it off and the account deletion purge it;
  * the briefing's "looked at with your AI app" never tells one of these;
  * nothing goes to `TaskActivity`, so `get_task_history` (shown to the in-app model) is the
    same with the recorder on and off;
  * the same event twice in a ten-minute bucket is one row, two people are two rows;
  * a failure to record never fails the change; a rolled-back change leaves no event;
  * a delete that only cascades, and the seeders, are not the member's own change.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from unittest import mock
from urllib.parse import urlencode

from django.db import transaction
from django.db.models.query import QuerySet
from django.db.utils import DatabaseError
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from origin.middleware.current_user import clear_current_user, set_current_user
from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.common.member_event_models import (
    ACTIVITY_KINDS,
    KIND_COLLABORATOR_ADD,
    KIND_COLLABORATOR_REMOVE,
    KIND_DEPENDENCY_ADD,
    KIND_DEPENDENCY_REMOVE,
    KIND_NOTE_SHARE,
    KIND_NOTE_UNSHARE,
    KIND_TODO_DELETE,
    KIND_TODO_EDIT,
    MemberEvent,
)
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.services import member_events
from origin.services.account_deletion import delete_account
from origin.services.account_export import build_export
from origin.services.task_collaborators import sync_task_collaborators
from origin.tests.test_briefing import BriefingBase
from origin.views.utils.note_role import ROLE_OWNER

PURGE_TODO = "origin.services.todo_items.purge_todo_item"
PURGE_NOTE = "origin.services.note_sharing.purge_note"
TITLE = "Renegotiate the Hendricks lease"
PREF = "/api/v2/user/preferences/member-events/"


def shape(rows):
    return sorted((r.kind, r.object_type, r.object_id, r.other_id) for r in rows)


class Base(BriefingBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(clear_current_user)
        self.blocker = self.make_task("Blocker", number=101)
        self.blocked = self.make_task("Blocked", number=102)
        self.today = timezone.localdate()
        self.group = ToDoGroup.objects.create(team=self.team, user=self.user, local_date=self.today)
        self.item = ToDoItem.objects.create(group=self.group, title=TITLE)
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="A private plan", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.note.note_id,
            note_type=1,
            role_id=ROLE_OWNER,
        )
        MemberEvent.objects.all().delete()
        for patcher in (mock.patch(PURGE_TODO), mock.patch(PURGE_NOTE)):
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- who is acting --

    def rest(self, user=None):
        self.authenticate(user or self.user)

    def mcp(self, **kw):
        self._key(scope=SCOPE_WRITE, allow_destructive=True, **kw)

    def tool(self, name, args):
        text, is_error = self._tool_text(self._call(name, args))
        self.assertFalse(is_error, text)
        return text

    # -- what was written --

    def events(self, user=None):
        return MemberEvent.objects.filter(user=user or self.user, kind__in=ACTIVITY_KINDS)

    def switch_off(self, user=None):
        user = user or self.user
        user.member_events_enabled = False
        user.save(update_fields=["member_events_enabled"])


class TestCollaborators(Base):
    def put(self, people):
        return self.client.put(
            reverse("task_create"),
            {"task_id": self.blocked.task_id, "collaborators": [str(p.id) for p in people]},
            format="json",
        )

    def test_the_app_notes_who_was_added_and_who_was_taken_off(self):
        self.rest()
        self.assertEqual(self.put([self.user2]).status_code, 200)
        self.assertEqual(
            shape(self.events()),
            [(KIND_COLLABORATOR_ADD, "task", str(self.blocked.task_id), str(self.user2.id))],
        )
        [row] = self.events()
        self.assertEqual((row.source, row.client, row.count), ("app", "", 1))
        self.assertEqual(str(row.team_id), str(self.team.team_id))
        self.assertEqual(self.put([]).status_code, 200)
        self.assertEqual(
            sorted(r.kind for r in self.events()), [KIND_COLLABORATOR_ADD, KIND_COLLABORATOR_REMOVE]
        )

    def test_saving_the_same_people_again_notes_nothing(self):
        self.rest()
        self.put([self.user2])
        before = self.events().count()
        self.put([self.user2])
        self.assertEqual(self.events().count(), before)

    def test_an_mcp_call_is_noted_with_how_it_arrived(self):
        self.mcp()
        self.tool(
            "update_task",
            {"task_id": self.blocked.task_id, "collaborators": [str(self.user2.id)]},
        )
        [row] = self.events()
        self.assertEqual(row.kind, KIND_COLLABORATOR_ADD)
        self.assertEqual(row.other_id, str(self.user2.id))
        self.assertEqual(row.source, "api_key")

    def test_a_milestones_collaborators_are_noted_on_its_backing_task(self):
        set_current_user(self.user)
        sync_task_collaborators(self.blocked, [str(self.user2.id)])
        self.assertEqual(self.events().count(), 1)

    def test_nobody_acting_is_nobody_to_describe(self):
        sync_task_collaborators(self.blocked, [str(self.user2.id)])
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_the_conversion_of_a_milestone_is_not_a_collaborator_change_by_the_member(self):
        set_current_user(self.user)
        self.blocked.collaborators.add(self.user2)  # what the conversion does
        self.assertEqual(MemberEvent.objects.count(), 0)


class TestDependencies(Base):
    def test_the_rest_view_notes_an_add_and_a_remove_on_the_blocked_task(self):
        self.rest()
        made = self.client.post(
            "/api/v2/task/dependency/",
            {
                "blocker_task_id": self.blocker.task_id,
                "blocked_task_id": self.blocked.task_id,
            },
            format="json",
        )
        self.assertEqual(made.status_code, 201, made.data)
        self.assertEqual(
            shape(self.events()),
            [(KIND_DEPENDENCY_ADD, "task", str(self.blocked.task_id), str(self.blocker.task_id))],
        )
        dep = TaskDependency.objects.get()
        self.assertEqual(self.client.delete(f"/api/v2/task/dependency/{dep.pk}/").status_code, 204)
        self.assertEqual(
            sorted(r.kind for r in self.events()), [KIND_DEPENDENCY_ADD, KIND_DEPENDENCY_REMOVE]
        )

    def test_the_mcp_tool_notes_an_add_and_a_remove(self):
        self.mcp()
        edge = {"blocker_task_id": self.blocker.task_id, "blocked_task_id": self.blocked.task_id}
        self.tool("update_task_dependencies", {"add": [edge]})
        self.tool("update_task_dependencies", {"remove": [edge]})
        rows = list(self.events())
        self.assertEqual(
            sorted(r.kind for r in rows), [KIND_DEPENDENCY_ADD, KIND_DEPENDENCY_REMOVE]
        )
        self.assertEqual({r.source for r in rows}, {"api_key"})

    def test_bulk_create_is_covered_too(self):
        # `create_task_plan` hands its edges to `bulk_create`, which fires no `post_save`.
        set_current_user(self.user)
        TaskDependency.objects.bulk_create(
            [
                TaskDependency(
                    blocker_task=self.blocker, blocked_task=self.blocked, team_id=self.team.team_id
                )
            ],
            ignore_conflicts=True,
        )
        self.assertEqual(
            shape(self.events()),
            [(KIND_DEPENDENCY_ADD, "task", str(self.blocked.task_id), str(self.blocker.task_id))],
        )

    def test_the_plan_tool_notes_the_edges_it_made(self):
        from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN

        set_current_user(self.user)
        CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "tasks": [{"title": "First"}, {"title": "Second", "blocked_by_indexes": [0]}],
            },
            self.ctx,
        )
        [row] = self.events()
        self.assertEqual(row.kind, KIND_DEPENDENCY_ADD)
        second = TaskMaster.objects.get(title="Second")
        self.assertEqual(row.object_id, str(second.task_id))

    def test_edges_that_go_with_a_deleted_task_are_not_the_members_own_remove(self):
        set_current_user(self.user)
        TaskDependency.objects.create(
            blocker_task=self.blocker, blocked_task=self.blocked, team_id=self.team.team_id
        )
        MemberEvent.objects.all().delete()
        self.blocker.delete()
        self.assertFalse(TaskDependency.objects.exists())
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_bulk_create_notes_only_the_edges_it_added(self):
        set_current_user(self.user)
        TaskDependency.objects.create(
            blocker_task=self.blocker, blocked_task=self.blocked, team_id=self.team.team_id
        )
        extra = self.make_task("Extra", number=103)
        MemberEvent.objects.all().delete()
        TaskDependency.objects.bulk_create(
            [
                TaskDependency(
                    blocker_task=self.blocker, blocked_task=self.blocked, team_id=self.team.team_id
                ),
                TaskDependency(
                    blocker_task=self.blocker, blocked_task=extra, team_id=self.team.team_id
                ),
            ],
            ignore_conflicts=True,
        )
        self.assertEqual(
            shape(self.events()),
            [(KIND_DEPENDENCY_ADD, "task", str(extra.task_id), str(self.blocker.task_id))],
        )

    def test_edges_dropped_by_moving_a_task_to_another_team_are_not_the_members_remove(self):
        from origin.models.common.team_models import TeamMaster
        from origin.services.task_project_move import _settle_dependencies

        set_current_user(self.user)
        TaskDependency.objects.create(
            blocker_task=self.blocker, blocked_task=self.blocked, team_id=self.team.team_id
        )
        MemberEvent.objects.all().delete()
        other = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.user
        )
        TaskMaster.objects.filter(pk=self.blocked.pk).update(team_id=other.team_id)
        _settle_dependencies([self.blocked.task_id], team_id=other.team_id)
        self.assertFalse(TaskDependency.objects.exists())
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_a_queryset_delete_is_a_remove(self):
        set_current_user(self.user)
        TaskDependency.objects.create(
            blocker_task=self.blocker, blocked_task=self.blocked, team_id=self.team.team_id
        )
        TaskDependency.objects.filter(blocked_task=self.blocked).delete()
        self.assertIn(KIND_DEPENDENCY_REMOVE, [r.kind for r in self.events()])

    def test_example_data_made_for_the_member_is_not_their_change(self):
        set_current_user(self.user)
        with member_events.not_recorded():
            TaskDependency.objects.create(
                blocker_task=self.blocker, blocked_task=self.blocked, team_id=self.team.team_id
            )
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_the_starter_workspace_does_not_credit_the_member(self):
        from origin.models.common.team_models import TeamMaster, TeamMembers
        from origin.services.starter_seeder import create_starter_workspace

        team = TeamMaster.objects.create(
            team_name="Fresh", team_email="f@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=team, attendee=self.user)
        set_current_user(self.user)
        create_starter_workspace(self.user, team)
        self.assertTrue(TaskDependency.objects.filter(team=team).exists())
        self.assertEqual(MemberEvent.objects.count(), 0)


class TestTodos(Base):
    def patch(self, body):
        return self.client.patch(
            reverse("todo_item_detail", args=[self.item.item_id]), body, format="json"
        )

    def test_an_edit_of_the_title_the_notes_the_tag_or_the_day_is_noted(self):
        self.rest()
        category = ToDoCategory.objects.create(user=self.user, team=self.team, name="Legal")
        later = (self.today + timedelta(days=3)).isoformat()
        for body in (
            {"title": "A new title"},
            {"notes": {"a": 1}},
            {"category_id": category.category_id},
            {"local_date": later},
        ):
            MemberEvent.objects.all().delete()
            self.assertEqual(self.patch(body).status_code, 200, body)
            self.assertEqual(
                shape(self.events()),
                [(KIND_TODO_EDIT, "todo", str(self.item.item_id), "")],
                body,
            )

    def test_a_tick_a_reorder_and_a_save_that_changed_nothing_are_not_edits(self):
        self.rest()
        for body in (
            {"is_completed": True},
            {"is_completed": False},
            {"sort_order": 5},
            {"title": TITLE},
        ):
            self.assertEqual(self.patch(body).status_code, 200, body)
        self.assertEqual(self.events().count(), 0)

    def test_a_delete_is_noted_without_the_title_anywhere(self):
        self.rest()
        item_id = self.item.item_id
        self.patch({"title": TITLE + " v2"})
        res = self.client.delete(reverse("todo_item_detail", args=[item_id]))
        self.assertEqual(res.status_code, 204)
        self.assertIn((KIND_TODO_DELETE, "todo", str(item_id), ""), shape(self.events()))
        everything = json.dumps(list(MemberEvent.objects.values()), default=str) + json.dumps(
            build_export(self.user)["activity"], default=str
        )
        self.assertNotIn("Hendricks", everything)
        self.assertNotIn("lease", everything)

    def test_the_mcp_tools_note_an_edit_and_a_delete(self):
        self.mcp()
        self.tool("update_todo_item", {"item_id": self.item.item_id, "title": "Via MCP"})
        self.tool("delete_todo_item", {"item_id": self.item.item_id})
        rows = list(self.events())
        self.assertEqual(sorted(r.kind for r in rows), [KIND_TODO_DELETE, KIND_TODO_EDIT])
        self.assertEqual({r.source for r in rows}, {"api_key"})

    def test_someone_elses_to_do_is_not_noted_for_anyone(self):
        theirs = ToDoItem.objects.create(
            group=ToDoGroup.objects.create(team=self.team, user=self.user2, local_date=self.today),
            title="theirs",
        )
        self.rest()
        res = self.client.patch(
            reverse("todo_item_detail", args=[theirs.item_id]), {"title": "x"}, format="json"
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_the_accounts_deletion_cascade_is_not_a_member_delete(self):
        ToDoGroup.objects.filter(user=self.user).delete()
        self.assertEqual(MemberEvent.objects.count(), 0)


class TestNoteShares(Base):
    def post(self, role=3, target=None):
        return self.client.post(
            "/api/v2/note/role/",
            {
                "team_id": str(self.team.team_id),
                "note_type": 1,
                "note_id": self.note.note_id,
                "target_user_id": str((target or self.user2).id),
                "role_id": role,
            },
            format="json",
        )

    def delete(self, target=None):
        query = urlencode(
            {
                "team_id": str(self.team.team_id),
                "note_type": 1,
                "note_id": self.note.note_id,
                "target_user_id": str((target or self.user2).id),
            }
        )
        return self.client.delete(f"/api/v2/note/role/?{query}")

    def key(self):
        return f"personal:{self.note.note_id}"

    def test_the_page_notes_who_shared_with_whom_and_who_stopped(self):
        self.rest()
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(
            shape(self.events()),
            [(KIND_NOTE_SHARE, "note", self.key(), str(self.user2.id))],
        )
        self.assertEqual(self.post(role=2).status_code, 200)  # a change of role
        self.assertEqual(self.events().get(kind=KIND_NOTE_SHARE).count, 2)
        self.assertEqual(self.delete().status_code, 204)
        self.assertIn(
            (KIND_NOTE_UNSHARE, "note", self.key(), str(self.user2.id)), shape(self.events())
        )

    def test_sending_the_same_grant_again_is_not_a_second_share(self):
        self.rest()
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.events().get(kind=KIND_NOTE_SHARE).count, 1)

    def test_the_mcp_tool_notes_a_share_and_an_unshare(self):
        self.mcp()
        base = {
            "note_type": "personal",
            "note_id": self.note.note_id,
            "user_id": str(self.user2.id),
        }
        self.tool("manage_note_sharing", {**base, "action": "share", "role": "viewer"})
        self.tool("manage_note_sharing", {**base, "action": "unshare"})
        rows = list(self.events())
        self.assertEqual(sorted(r.kind for r in rows), [KIND_NOTE_SHARE, KIND_NOTE_UNSHARE])
        self.assertEqual({r.source for r in rows}, {"api_key"})

    def test_a_refused_share_is_not_noted(self):
        self.rest(self.user2)  # not the owner
        self.assertEqual(self.post(target=self.user).status_code, 403)
        self.assertEqual(self.delete(target=self.user).status_code, 403)
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_unsharing_nobody_is_noted_for_nobody(self):
        self.rest()
        self.assertEqual(self.delete().status_code, 404)
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_the_owner_row_made_with_the_note_is_not_a_share(self):
        NotePermissionMaster.objects.update_or_create(
            user=self.user2, note_id=self.note.note_id, note_type=1, defaults={"role_id": 3}
        )
        self.assertEqual(MemberEvent.objects.count(), 0)


class TestTheSwitch(Base):
    def everything(self):
        """Each hooked path once, as the app does it."""
        self.client.put(
            reverse("task_create"),
            {"task_id": self.blocked.task_id, "collaborators": [str(self.user2.id)]},
            format="json",
        )
        self.client.post(
            "/api/v2/task/dependency/",
            {"blocker_task_id": self.blocker.task_id, "blocked_task_id": self.blocked.task_id},
            format="json",
        )
        self.client.patch(
            reverse("todo_item_detail", args=[self.item.item_id]), {"title": "x"}, format="json"
        )
        self.client.post(
            "/api/v2/note/role/",
            {
                "team_id": str(self.team.team_id),
                "note_type": 1,
                "note_id": self.note.note_id,
                "target_user_id": str(self.user2.id),
                "role_id": 3,
            },
            format="json",
        )
        self.client.delete(reverse("todo_item_detail", args=[self.item.item_id]))

    def test_on_by_default_every_path_writes(self):
        self.rest()
        self.everything()
        self.assertEqual(
            {r.kind for r in self.events()},
            {
                KIND_COLLABORATOR_ADD,
                KIND_DEPENDENCY_ADD,
                KIND_TODO_EDIT,
                KIND_NOTE_SHARE,
                KIND_TODO_DELETE,
            },
        )

    def test_off_every_path_writes_nothing_and_asks_the_table_nothing(self):
        self.switch_off()
        self.rest()
        with CaptureQueriesContext(member_events.connection) as captured:
            self.everything()
        self.assertEqual(MemberEvent.objects.count(), 0)
        touched = [q["sql"] for q in captured.captured_queries if "origin_memberevent" in q["sql"]]
        self.assertEqual(touched, [])
        # ... and the changes themselves went through.
        self.assertTrue(TaskDependency.objects.exists())
        self.assertFalse(ToDoItem.objects.filter(pk=self.item.pk).exists())

    def test_off_in_the_database_is_enough_when_no_request_holds_the_user(self):
        # The statement checks the switch itself, under lock: a thread that holds a stale
        # (or no) user row still writes nothing.
        self.switch_off()
        member_events.record_activity(
            user_id=str(self.user.id),
            team_id=str(self.team.team_id),
            kind=KIND_TODO_EDIT,
            items=[("todo", "1", "")],
        )
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_a_stale_row_that_says_on_is_still_refused_by_the_statement(self):
        set_current_user(self.user)  # in memory: on
        MemberEvent.objects.all().delete()
        type(self.user).objects.filter(pk=self.user.pk).update(member_events_enabled=False)
        member_events.record_activity(
            user_id=str(self.user.id),
            team_id=str(self.team.team_id),
            kind=KIND_TODO_EDIT,
            items=[("todo", "1", "")],
        )
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_turning_it_off_deletes_what_was_noted(self):
        self.rest()
        self.everything()
        self.assertGreater(self.events().count(), 0)
        res = self.client.patch(PREF, {"member_events_enabled": False}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(MemberEvent.objects.filter(user=self.user).count(), 0)

    def test_the_accounts_deletion_deletes_them_and_only_theirs(self):
        set_current_user(self.user)
        sync_task_collaborators(self.blocked, [str(self.user2.id)])
        clear_current_user()
        set_current_user(self.user2)
        sync_task_collaborators(self.blocker, [str(self.user.id)])
        clear_current_user()
        self.assertEqual(MemberEvent.objects.count(), 2)
        delete_account(self.user2)
        self.assertEqual(shape(MemberEvent.objects.all())[0][0], KIND_COLLABORATOR_ADD)
        self.assertEqual(MemberEvent.objects.filter(user=self.user2).count(), 0)
        self.assertEqual(MemberEvent.objects.filter(user=self.user).count(), 1)


class TestPrivateToTheMember(Base):
    def test_the_reader_returns_only_the_members_own_rows(self):
        set_current_user(self.user)
        sync_task_collaborators(self.blocked, [str(self.user2.id)])
        clear_current_user()
        set_current_user(self.user2)
        sync_task_collaborators(self.blocker, [str(self.user.id)])
        clear_current_user()
        since = timezone.now() - timedelta(days=1)
        until = timezone.now() + timedelta(days=1)
        team = str(self.team.team_id)
        mine = member_events.recent(str(self.user.id), team, since, until, kinds=ACTIVITY_KINDS)
        theirs = member_events.recent(str(self.user2.id), team, since, until, kinds=ACTIVITY_KINDS)
        self.assertEqual([m.other_id for m in mine], [str(self.user2.id)])
        self.assertEqual([m.other_id for m in theirs], [str(self.user.id)])

    def test_the_default_reader_never_returns_an_activity_row(self):
        set_current_user(self.user)
        sync_task_collaborators(self.blocked, [str(self.user2.id)])
        since = timezone.now() - timedelta(days=1)
        until = timezone.now() + timedelta(days=1)
        self.assertEqual(
            member_events.recent(str(self.user.id), str(self.team.team_id), since, until), []
        )

    def test_the_briefing_never_tells_one_as_looked_at_with_the_ai_app(self):
        set_current_user(self.user)
        sync_task_collaborators(self.blocked, [str(self.user2.id)])
        clear_current_user()
        self.assertEqual(self.events().count(), 1)
        did = self.brief()["what_you_did"]
        self.assertEqual(did["today"]["looked_at"], [])

    def test_the_export_keeps_the_two_apart_and_names_no_title(self):
        set_current_user(self.user)
        sync_task_collaborators(self.blocked, [str(self.user2.id)])
        MemberEvent.objects.create(
            user=self.user,
            team=self.team,
            kind="read",
            object_type="task",
            object_id=str(self.blocked.task_id),
            client="Claude",
            bucket=1,
        )
        # Another member's rows must never travel in this member's export.
        set_current_user(self.user2)
        sync_task_collaborators(self.blocker, [str(self.user.id)])
        MemberEvent.objects.create(
            user=self.user2,
            team=self.team,
            kind="read",
            object_type="task",
            object_id=str(self.blocker.task_id),
            client="ChatGPT",
            bucket=1,
        )
        clear_current_user()
        export = build_export(self.user)
        self.assertEqual([r["app"] for r in export["mcp_use"]], ["Claude"])
        [row] = export["activity"]
        self.assertEqual(row["kind"], KIND_COLLABORATOR_ADD)
        self.assertEqual(row["other_id"], str(self.user2.id))
        self.assertEqual((export["counts"]["mcp_use"], export["counts"]["activity"]), (1, 1))
        self.assertNotIn("Blocked", json.dumps(export["activity"]))

    def test_retention_sweeps_the_members_own_old_rows_on_their_next_write(self):
        old = timezone.now() - member_events.RETENTION - timedelta(days=1)
        for user in (self.user, self.user2):
            MemberEvent.objects.create(
                user=user,
                team=self.team,
                kind=KIND_TODO_EDIT,
                object_type="todo",
                object_id="1",
                ts=old,
                bucket=1,
            )
        set_current_user(self.user)
        sync_task_collaborators(self.blocked, [str(self.user2.id)])
        self.assertEqual(
            MemberEvent.objects.filter(user=self.user, ts__lt=old + timedelta(days=1)).count(), 0
        )
        self.assertEqual(MemberEvent.objects.filter(user=self.user2).count(), 1)


class TestOneRowPerBucket(Base):
    def setUp(self):
        super().setUp()
        # An hour back, on a ten-minute boundary: independent of the hour of day.
        self.t0 = timezone.now().replace(microsecond=0)
        self.t0 = self.t0 - timedelta(seconds=int(self.t0.timestamp()) % 600 + 3600)

    def write(self, other, seconds, kind=KIND_COLLABORATOR_ADD):
        member_events.record_activity(
            user_id=str(self.user.id),
            team_id=str(self.team.team_id),
            kind=kind,
            items=[("task", "5", other)],
            now=self.t0 + timedelta(seconds=seconds),
        )

    def test_the_same_event_in_a_bucket_is_one_row_with_a_count(self):
        self.write("p1", 10)
        self.write("p1", 20)
        [row] = self.events()
        self.assertEqual(row.count, 2)
        self.assertEqual(row.ts, self.t0 + timedelta(seconds=20))

    def test_two_people_in_one_bucket_are_two_rows(self):
        self.write("p1", 10)
        self.write("p2", 11)
        self.assertEqual(sorted(r.other_id for r in self.events()), ["p1", "p2"])

    def test_the_next_bucket_is_a_new_row_and_another_kind_is_another_row(self):
        self.write("p1", 10)
        self.write("p1", 610)
        self.write("p1", 10, kind=KIND_COLLABORATOR_REMOVE)
        self.assertEqual(self.events().count(), 3)

    def test_a_marker_and_an_activity_row_do_not_collide(self):
        member_events.record(
            user_id=str(self.user.id),
            team_id=str(self.team.team_id),
            items=[("task", "5")],
            kind="write",
            client="Claude",
            now=self.t0,
        )
        self.write("", 10)
        self.assertEqual(MemberEvent.objects.count(), 2)

    def test_a_list_longer_than_one_statement_holds_is_all_written(self):
        member_events.record_activity(
            user_id=str(self.user.id),
            team_id=str(self.team.team_id),
            kind=KIND_DEPENDENCY_ADD,
            items=[("task", str(n), "1") for n in range(1, 71)],
            now=self.t0,
        )
        self.assertEqual(self.events().count(), 70)


class TestNeverInTheWay(Base):
    def test_a_recorder_that_raises_does_not_fail_the_change(self):
        self.rest()
        with mock.patch.object(member_events, "record", side_effect=DatabaseError("boom")):
            res = self.client.put(
                reverse("task_create"),
                {"task_id": self.blocked.task_id, "collaborators": [str(self.user2.id)]},
                format="json",
            )
            gone = self.client.delete(reverse("todo_item_detail", args=[self.item.item_id]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(gone.status_code, 204)
        self.assertEqual(self.blocked.collaborators.count(), 1)
        self.assertFalse(ToDoItem.objects.filter(pk=self.item.pk).exists())

    def test_a_failed_statement_does_not_break_the_transaction_around_it(self):
        set_current_user(self.user)
        with transaction.atomic():
            member_events.record_activity(
                user_id=str(self.user.id),
                team_id=str(self.team.team_id),
                kind="x" * 80,  # longer than the column: the database refuses it
                items=[("task", "1", "")],
            )
            self.assertEqual(TaskMaster.objects.filter(pk=self.blocked.pk).count(), 1)

    def test_a_change_that_rolls_back_leaves_no_event(self):
        set_current_user(self.user)
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                TaskDependency.objects.create(
                    blocker_task=self.blocker, blocked_task=self.blocked, team_id=self.team.team_id
                )
                self.assertEqual(self.events().count(), 1)
                raise RuntimeError
        self.assertEqual(self.events().count(), 0)

    def test_nobody_acting_is_not_even_attempted(self):
        # No member is no write and no failed write either: nothing is logged.
        with self.assertNoLogs("origin.services.member_events", level="WARNING"):
            member_events.record_activity(
                user_id=None,
                team_id=str(self.team.team_id),
                kind=KIND_TODO_EDIT,
                items=[("todo", "1", "")],
            )
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_no_team_is_nothing_to_write(self):
        set_current_user(self.user)
        member_events.record_activity(
            user_id=str(self.user.id), team_id=None, kind=KIND_TODO_EDIT, items=[("todo", "1", "")]
        )
        self.assertEqual(MemberEvent.objects.count(), 0)


class TestTheModelsHistoryIsUntouched(Base):
    """`get_task_history` shows `TaskActivity` to the in-app model: this work must not add to it."""

    def drive(self, blocker, blocked, user):
        """Collaborator, dependency and to-do changes on this pair, as the app makes them."""
        self.rest(user)
        self.client.put(
            reverse("task_create"),
            {"task_id": blocked.task_id, "collaborators": [str(self.user2.id)]},
            format="json",
        )
        self.client.post(
            "/api/v2/task/dependency/",
            {"blocker_task_id": blocker.task_id, "blocked_task_id": blocked.task_id},
            format="json",
        )
        dep = TaskDependency.objects.get(blocker_task=blocker, blocked_task=blocked)
        self.client.delete(f"/api/v2/task/dependency/{dep.pk}/")
        self.client.post(
            "/api/v2/task/dependency/",
            {"blocker_task_id": blocker.task_id, "blocked_task_id": blocked.task_id},
            format="json",
        )

    def history(self, task):
        self._key(scope=SCOPE_WRITE)
        text = self.tool("get_task_history", {"task_id": task.task_id})
        text = re.sub(r"\d{4}-\d\d-\d\dT[\d:.+Z-]+", "<ts>", text)
        text = re.sub(r"\b[A-Z]{2,5}-\d+\b", "<display>", text)
        return re.sub(r"\b\d+\b", "<n>", text)

    def activity_rows(self, task):
        return list(
            TaskActivity.objects.filter(task=task)
            .order_by("activity_id")
            .values_list("action_type", "field_name", "old_value", "new_value")
        )

    def test_the_same_changes_leave_the_same_history_with_the_recorder_on_and_off(self):
        first_blocker = self.make_task("Blocker", number=201)
        first_blocked = self.make_task("Blocked", number=202)
        second_blocker = self.make_task("Blocker", number=203)
        second_blocked = self.make_task("Blocked", number=204)

        self.drive(first_blocker, first_blocked, self.user)
        self.assertGreater(self.events().count(), 0, "the recorder was on for the first pair")
        on_history = self.history(first_blocked)

        self.switch_off()
        MemberEvent.objects.all().delete()
        self.drive(second_blocker, second_blocked, self.user)
        self.assertEqual(self.events().count(), 0)
        off_history = self.history(second_blocked)

        self.assertEqual(self.activity_rows(first_blocked), self.activity_rows(second_blocked))
        self.assertEqual(self.activity_rows(first_blocker), self.activity_rows(second_blocker))
        self.assertEqual(on_history, off_history)

    def test_the_recorder_writes_no_task_activity_row_of_its_own(self):
        set_current_user(self.user)
        before = TaskActivity.objects.count()
        with mock.patch("origin.signals.dependency_signals.sync_blocked_status"):
            # the status flip to Blocked writes history of its own, and is not the recorder's
            TaskDependency.objects.create(
                blocker_task=self.blocker, blocked_task=self.blocked, team_id=self.team.team_id
            )
        sync_task_collaborators(self.blocked, [str(self.user2.id)])
        self.assertEqual(self.events().count(), 2)
        self.assertEqual(TaskActivity.objects.count(), before)


class TestTheWaysOfAskingForADelete(Base):
    def test_the_signal_tells_a_queryset_from_a_cascade(self):
        # The `origin` the dependency signal reads: a queryset of dependencies or one
        # dependency is an explicit delete; a task is a cascade.
        seen = []

        def spy(sender, instance, **kwargs):
            seen.append(kwargs.get("origin"))

        from django.db.models.signals import post_delete

        post_delete.connect(spy, sender=TaskDependency, weak=False)
        self.addCleanup(post_delete.disconnect, spy, sender=TaskDependency)
        TaskDependency.objects.create(
            blocker_task=self.blocker, blocked_task=self.blocked, team_id=self.team.team_id
        )
        TaskDependency.objects.all().delete()
        TaskDependency.objects.create(
            blocker_task=self.blocker, blocked_task=self.blocked, team_id=self.team.team_id
        )
        self.blocker.delete()
        self.assertIsInstance(seen[0], QuerySet)
        self.assertIsInstance(seen[1], TaskMaster)


class TestAThreadMarkerIsStored(Base):
    """A `chat_thread` marker is `chat_id:thread_id`, two UUIDs: 73 characters. The column was
    64, the insert failed and the recorder swallowed it, so no thread marker was ever stored
    (found by MIP-1083)."""

    CHAT = "11111111-2222-3333-4444-555555555555"
    THREAD = "66666666-7777-8888-9999-000000000000"

    def test_a_real_thread_marker_is_stored_and_read_back(self):
        [(object_type, object_id)] = member_events.subjects(
            "get_chat_thread", {"chat_id": self.CHAT, "thread_id": self.THREAD}
        )
        self.assertEqual(len(object_id), 73)
        member_events.record(
            user_id=str(self.user.id),
            team_id=str(self.team.team_id),
            items=[(object_type, object_id)],
            kind="read",
            client="Claude",
        )
        since = timezone.now() - timedelta(days=1)
        until = timezone.now() + timedelta(days=1)
        [marker] = member_events.recent(str(self.user.id), str(self.team.team_id), since, until)
        self.assertEqual((marker.object_type, marker.object_id), ("chat_thread", object_id))

    def test_the_call_path_stores_it_too(self):
        member_events.record_call(
            user=self.user,
            team_id=str(self.team.team_id),
            tool_name="get_chat_thread",
            write=False,
            args={"chat_id": self.CHAT, "thread_id": self.THREAD},
        )
        self.assertEqual(
            list(MemberEvent.objects.values_list("object_id", flat=True)),
            [f"{self.CHAT}:{self.THREAD}"],
        )

    def test_a_note_in_the_longest_family_still_fits(self):
        member_events.record(
            user_id=str(self.user.id),
            team_id=str(self.team.team_id),
            items=[("note", "personal:" + "9" * 18)],
            kind="write",
            client="Claude",
        )
        self.assertEqual(MemberEvent.objects.count(), 1)
