"""MIP-2009 (decision D4): an AI-app notice (feed row, toast, push) starts on only for risky actions.

Deletes and any call that changes 10+ items at once alert by default. Every other covered write
makes no notice for a member who never touched the switches (it shows in the Today card instead).
An explicit choice of the member, either way, is kept.
"""

from __future__ import annotations

from origin.models.chat.todo_models import ToDoItem
from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.notification_models import NotificationPreference
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.services import ai_app_switches as switches
from origin.services.webpush_dispatch import _push_spec
from origin.tests.ai_app_defaults import restore_real_defaults
from origin.tests.test_mcp_notice import NoticeBase, notices


class RiskyDefaultsTests(NoticeBase):
    def setUp(self):
        super().setUp()
        restore_real_defaults(self)

    def test_a_plain_create_status_comment_and_edit_make_no_notice(self):
        self.call("create_task", project_id=self.project.project_id, title="Quiet")
        self.call("update_task", task_id=self.task.task_id, status="WIP")
        self.call("add_comment", task_id=self.task.task_id, body_text="hello")
        self.call("create_note", note_type="personal", title="N", content_text="x")
        self.call("create_todo_item", title="A to-do")
        self.assertEqual(notices(self.user), [])

    def test_a_delete_of_each_kind_makes_a_notice(self):
        self.call("create_note", note_type="personal", title="Doomed", content_text="x")
        note = PersonalNoteMaster.objects.get(title="Doomed")
        self.call("delete_note", note_id=note.note_id, note_type="personal")
        self.call("create_todo_item", title="Doomed todo")
        item = ToDoItem.objects.get(title="Doomed todo")
        self.call("delete_todo_item", item_id=item.item_id)
        self.assertEqual(
            [(a.meta["verb"], a.meta["kind"]) for a in notices(self.user)],
            [("deleted", "note"), ("deleted", "todo")],
        )

    def test_a_bulk_change_of_ten_or_more_makes_a_notice_pushed_under_the_group(self):
        updates = [{"title": f"Bulk {i}"} for i in range(switches.BULK_THRESHOLD)]
        self.call("create_task_plan", project_id=self.project.project_id, tasks=updates)
        row = self.only()
        self.assertEqual(row.meta["count"], switches.BULK_THRESHOLD)
        self.assertTrue(row.meta["bulk"])
        self.assertEqual(_push_spec(row)["category"], "ai_app")

    def test_nine_tasks_at_once_is_not_bulk(self):
        updates = [{"title": f"Few {i}"} for i in range(9)]
        self.call("create_task_plan", project_id=self.project.project_id, tasks=updates)
        self.assertEqual(notices(self.user), [])

    def test_an_explicit_on_is_kept_for_a_non_risky_action(self):
        NotificationPreference.objects.create(
            user=self.user, category_settings={"ai_app_task_created": True}
        )
        self.call("create_task", project_id=self.project.project_id, title="Wanted")
        self.assertEqual(len(notices(self.user)), 1)

    def test_an_explicit_off_is_kept_for_a_risky_action_and_a_bulk(self):
        NotificationPreference.objects.create(
            user=self.user,
            category_settings={"ai_app_note_deleted": False, "ai_app_task_created": False},
        )
        self.call("create_note", note_type="personal", title="Gone", content_text="x")
        note = PersonalNoteMaster.objects.get(title="Gone")
        self.call("delete_note", note_id=note.note_id, note_type="personal")
        updates = [{"title": f"B{i}"} for i in range(switches.BULK_THRESHOLD)]
        self.call("create_task_plan", project_id=self.project.project_id, tasks=updates)
        self.assertEqual(Activity.objects.filter(activity_type=ActivityType.AI_APP).count(), 0)

    def test_another_setting_does_not_count_as_touching_a_switch(self):
        NotificationPreference.objects.create(user=self.user, category_settings={"chats": False})
        self.call("create_task", project_id=self.project.project_id, title="Still quiet")
        self.assertEqual(notices(self.user), [])
