"""A cross-team share set to View only stops the other team writing (MIP-2571).

Every write path is called by a member of the guest team twice: with the share on View only (403 in
plain words, nothing changed) and again after the host switches the same live share to Edit (it goes
through). Reads stay open, and the host's own people are never affected.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import override_settings

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.api_key_models import (
    SCOPE_WRITE,
    ApiKey,
    generate_key,
    hash_key,
)
from origin.models.common.team_models import ExternalGrant
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import (
    TaskCommentReactionFact,
    TaskComments,
    TaskDependency,
    TaskMaster,
)
from origin.search_engine.agent.controller import _archived_write_refusal
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.create_note import CREATE_NOTE
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.undo_apply import Undoing, UndoRefused, _locked_task
from origin.services.archive import archived_refusal_for_args
from origin.services.external_grants import add_external_participants, set_role_ceiling
from origin.services.member_roles import EDITOR, VIEWER
from origin.services.note_sharing import SharingRefused, share_note
from origin.services.schedule_audience import send_problem
from origin.services.share_roles import (
    CHAT_MESSAGE,
    PROJECT_MESSAGE,
    VIEW_ONLY_CODE,
    is_view_only_channel,
    is_view_only_project,
)
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.tests.test_mcp_endpoint import MCP, _mcp_enabled_everywhere
from origin.views.task.task_views import TASK_CREATE_REQUIRED

BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}]


class _ViewOnlyCase(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant(VIEWER)
        add_external_participants(self.grant, [self.b_editor.id], self.b_owner)
        self.assertTrue(self._in_project(self.b_editor))
        ProjectMembers.objects.get_or_create(
            team=self.team_a, project=self.project, attendee=self.a_owner
        )
        self.task = self._task("Host task")
        self.other = self._task("Other task")
        self.pid = self.project.project_id
        self.team_id = str(self.team_a.team_id)
        self.authenticate(self.b_editor)

    def _task(self, title, **extra):
        values = {"team": self.team_a, "project": self.project, "reporter": self.a_owner}
        values.update(extra)
        return TaskMaster.objects.create(title=title, **values)

    def allow_editing(self):
        self.grant = set_role_ceiling(self.grant, EDITOR, self.a_owner)

    def view_only_again(self):
        self.grant = set_role_ceiling(self.grant, VIEWER, self.a_owner)

    def assert_view_only(self, res):
        self.assertEqual(res.status_code, 403, getattr(res, "data", res))
        self.assertEqual(res.data.get("code"), VIEW_ONLY_CODE, res.data)

    def assert_allowed(self, res):
        self.assertLess(res.status_code, 300, getattr(res, "data", res))

    def both(self, call, refused=None, allowed=None):
        """View only refuses `call`; the same share on Edit lets it through."""
        res = call()
        self.assert_view_only(res)
        if refused:
            refused()
        self.allow_editing()
        res = call()
        self.assert_allowed(res)
        if allowed:
            allowed()
        return res


class ViewOnlyTaskRestTests(_ViewOnlyCase):
    def _create_task_payload(self, title):
        payload = dict.fromkeys(TASK_CREATE_REQUIRED)
        payload.update(
            {
                "team": self.team_id,
                "project": self.pid,
                "title": title,
                "reporter": str(self.b_editor.id),
                "assignee": str(self.b_editor.id),
                "status": "Open",
                "priority": "Normal",
                "effort_level": "Low",
                "tags": [],
                "content": [],
                "links": [],
                "due_date": "2027-01-01",
                "start_date": "2027-01-01",
            }
        )
        return payload

    def test_task_create(self):
        def count():
            return TaskMaster.objects.filter(title="From B").count()

        self.both(
            lambda: self.client.post(
                "/api/v2/task/", self._create_task_payload("From B"), format="json"
            ),
            refused=lambda: self.assertEqual(count(), 0),
            allowed=lambda: self.assertEqual(count(), 1),
        )

    def test_task_update_and_delete(self):
        def title():
            return TaskMaster.objects.get(pk=self.task.pk).title

        self.both(
            lambda: self.client.put(
                "/api/v2/task/",
                {"task_id": self.task.task_id, "team": self.team_id, "title": "Changed"},
                format="json",
            ),
            refused=lambda: self.assertEqual(title(), "Host task"),
            allowed=lambda: self.assertEqual(title(), "Changed"),
        )
        self.view_only_again()
        url = (
            f"/api/v2/task/?team_id={self.team_id}&task_id={self.other.task_id}"
            "&is_init_task_boolean=0"
        )
        self.both(
            lambda: self.client.delete(url),
            refused=lambda: self.assertTrue(TaskMaster.objects.filter(pk=self.other.pk).exists()),
        )

    def test_move_into_a_view_only_project(self):
        # A project this person may edit (invited there directly), and a task of it.
        mine = ProjectMaster.objects.create(
            team=self.team_a,
            project_name="Mine",
            owner=self.a_owner,
            project_system_user=self.a_owner,
        )
        ProjectMembers.objects.create(team=self.team_a, project=mine, attendee=self.b_editor)
        task = self._task("Moving", project=mine)
        self.assert_view_only(
            self.client.put(
                "/api/v2/task/",
                {"task_id": task.task_id, "team": self.team_id, "project": self.pid},
                format="json",
            )
        )
        self.assertEqual(TaskMaster.objects.get(pk=task.pk).project_id, mine.project_id)

    def test_filing_a_task_under_the_view_only_projects_tree(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team_a, project=self.project, title="Theirs", reporter=self.a_owner
        )
        mine = ProjectMaster.objects.create(
            team=self.team_a,
            project_name="Mine",
            owner=self.a_owner,
            project_system_user=self.a_owner,
        )
        ProjectMembers.objects.create(team=self.team_a, project=mine, attendee=self.b_editor)
        task = self._task("Moving", project=mine)
        for link in (
            {"milestone": milestone.milestone_id},
            {"parent_task_id": self.task.task_id},
        ):
            with self.subTest(link=link):
                self.assert_view_only(
                    self.client.put(
                        "/api/v2/task/",
                        {"task_id": task.task_id, "team": self.team_id, **link},
                        format="json",
                    )
                )
        task.refresh_from_db()
        self.assertEqual((task.milestone_id, task.parent_task_id), (None, None))
        payload = self._create_task_payload("Child")
        payload.update(project=mine.project_id, parent_task_id=self.task.task_id)
        self.assert_view_only(self.client.post("/api/v2/task/", payload, format="json"))
        self.assertFalse(TaskMaster.objects.filter(title="Child").exists())

    def test_approving_a_note_access_request_from_the_inbox(self):
        from origin.models.common.inbox_models import InboxItems
        from origin.models.note.common_note_models import NotePermissionMaster
        from origin.views.common.inbox_views import NOTE_ACCESS_REQUEST

        note = TaskNoteMaster.objects.create(
            team=self.team_a,
            project=self.project,
            task=self.task,
            owner=self.b_editor,
            title="Mine",
            body=BODY,
        )
        item = InboxItems.objects.create(
            team=self.team_a,
            sender=self.a_viewer,
            receiver=self.b_editor,
            item_type=NOTE_ACCESS_REQUEST,
            item_body="access",
            item_optionals={"note_type": 2, "note_id": note.note_id},
            request_status="pending",
        )
        self.assert_view_only(
            self.client.post(
                "/api/v2/note/role/fromInbox/", {"item_id": item.item_id}, format="json"
            )
        )
        self.assertFalse(NotePermissionMaster.objects.filter(user=self.a_viewer).exists())

    def test_comments_reactions_and_mentions(self):
        def post():
            return self.client.post(
                "/api/v2/task/comment/",
                {"task_id": self.task.task_id, "comment_body": BODY},
                format="json",
            )

        res = self.both(
            post,
            refused=lambda: self.assertEqual(TaskComments.objects.count(), 0),
            allowed=lambda: self.assertEqual(TaskComments.objects.count(), 1),
        )
        comment_id = res.data["comment_id"]
        self.view_only_again()
        edited = [{"type": "paragraph", "content": [{"type": "text", "text": "new"}]}]
        self.both(
            lambda: self.client.put(
                "/api/v2/task/comment/",
                {"task_id": self.task.task_id, "comment_id": comment_id, "comment_body": edited},
                format="json",
            )
        )
        self.view_only_again()
        self.both(
            lambda: self.client.post(
                "/api/v2/task/comment/reaction/",
                {
                    "team_id": self.team_id,
                    "task_id": self.task.task_id,
                    "comment_id": comment_id,
                    "reaction_emoji": "👍",
                    "sender_id": str(self.b_editor.id),
                },
                format="json",
            ),
            refused=lambda: self.assertEqual(TaskCommentReactionFact.objects.count(), 0),
        )
        self.view_only_again()
        self.both(
            lambda: self.client.post(
                "/api/v2/task/comment/mention/",
                {"team_id": self.team_id, "task_id": self.task.task_id, "mentioned_user_ids": []},
                format="json",
            )
        )
        self.view_only_again()
        self.both(
            lambda: self.client.delete(
                f"/api/v2/task/comment/?task_id={self.task.task_id}&comment_id={comment_id}"
            )
        )

    def test_attachments(self):
        def upload():
            return SimpleUploadedFile("a.txt", b"hello", content_type="text/plain")

        self.both(
            lambda: self.client.post(
                "/api/v2/task/attachment/",
                {
                    "task": self.task.task_id,
                    "attachment_id": -1,
                    "attached_type": "file",
                    "attached_file": upload(),
                },
                format="multipart",
            )
        )
        self.view_only_again()
        self.both(
            lambda: self.client.post(
                "/api/v2/task/body/attachment/",
                {
                    "task_id": self.task.task_id,
                    "uploader": str(self.b_editor.id),
                    "body_attachment_file": upload(),
                },
                format="multipart",
            )
        )

    def test_dependencies(self):
        self.both(
            lambda: self.client.post(
                "/api/v2/task/dependency/",
                {"blocker_task_id": self.task.task_id, "blocked_task_id": self.other.task_id},
                format="json",
            ),
            refused=lambda: self.assertEqual(TaskDependency.objects.count(), 0),
            allowed=lambda: self.assertEqual(TaskDependency.objects.count(), 1),
        )
        dep = TaskDependency.objects.get()
        self.view_only_again()
        self.both(lambda: self.client.delete(f"/api/v2/task/dependency/{dep.pk}/"))

    def test_body_restore_force_delete_and_convert(self):
        self.assert_view_only(
            self.client.post(
                "/api/v2/task/activity/restore-body/",
                {"task_id": self.task.task_id, "activity_id": 1},
                format="json",
            )
        )
        self.assert_view_only(
            self.client.post(
                f"/api/v3/tasks/{self.task.task_id}/force-delete/",
                {"confirm": "delete-permanently"},
                format="json",
            )
        )
        self.assertTrue(TaskMaster.objects.filter(pk=self.task.pk).exists())
        self.both(
            lambda: self.client.post(
                "/api/v2/task/convert-to-milestone/",
                {"task_id": self.task.task_id},
                format="json",
            )
        )

    def test_milestones_and_sprints(self):
        self.both(
            lambda: self.client.post(
                "/api/v2/milestone/", {"project_id": self.pid, "title": "M1"}, format="json"
            ),
            refused=lambda: self.assertFalse(MilestoneMaster.objects.exists()),
        )
        milestone = MilestoneMaster.objects.get()
        self.view_only_again()
        self.both(
            lambda: self.client.post(
                f"/api/v2/milestone/{milestone.milestone_id}/assignees/",
                {"user_id": str(self.a_owner.id)},
                format="json",
            ),
            refused=lambda: self.assertFalse(
                MilestoneAssignees.objects.filter(user=self.a_owner).exists()
            ),
        )
        self.view_only_again()
        self.both(
            lambda: self.client.post(
                "/api/v2/milestone/folder/", {"project_id": self.pid, "name": "F"}, format="json"
            )
        )
        self.view_only_again()
        sprint_payload = {
            "project_id": self.pid,
            "name": "S",
            "start_date": "2027-01-01",
            "end_date": "2027-01-14",
        }
        res = self.client.post("/api/v2/sprint/", sprint_payload, format="json")
        self.assertEqual((res.status_code, res.data), (403, {"error": PROJECT_MESSAGE}))
        self.allow_editing()
        self.assert_allowed(self.client.post("/api/v2/sprint/", sprint_payload, format="json"))
        sprint = Sprint.objects.filter(name="S").get()
        self.view_only_again()
        res = self.client.delete(f"/api/v2/sprint/{sprint.sprint_id}/")
        # The sprint service answers in its own shape: the same words, no code.
        self.assertEqual((res.status_code, res.data), (403, {"error": PROJECT_MESSAGE}))
        self.assert_view_only(
            self.client.post(
                "/api/v2/sprint/config/",
                {"project_id": self.pid, "duration_days": 7, "anchor_date": "2027-01-01"},
                format="json",
            )
        )

    def test_project_catalog_and_filters(self):
        tag = {
            "team_id": self.team_id,
            "project_id": self.pid,
            "tag_name": "bug",
            "tag_color": "#fff",
            "tag_text_color": "#000",
        }
        self.both(
            lambda: self.client.post("/api/v2/project/tag/", tag, format="json"),
            refused=lambda: self.assertFalse(ProjectTags.objects.exists()),
        )
        self.view_only_again()
        self.both(
            lambda: self.client.put(
                "/api/v2/project/tag/",
                {"project_id": self.pid, "old_tag_name": "bug", "tag_name": "defect"},
                format="json",
            )
        )
        self.view_only_again()
        self.both(
            lambda: self.client.post(
                "/api/v2/project/saved-task-filters/",
                {
                    "team_id": self.team_id,
                    "project_id": self.pid,
                    "filter_name": "mine",
                    "filters": {},
                },
                format="json",
            )
        )
        self.view_only_again()
        self.both(
            lambda: self.client.post(
                "/api/v2/project/task-template/",
                {
                    "team_id": self.team_id,
                    "project_id": self.pid,
                    "template_name": "t",
                    "body": [],
                },
                format="json",
            )
        )

    def test_reads_stay_open(self):
        for url in (
            f"/api/v2/task/getTask/?team_id={self.team_id}&task_id={self.task.task_id}",
            f"/api/v2/task/comment/?team_id={self.team_id}&task_id={self.task.task_id}",
            f"/api/v2/task/dependency/list/?task_id={self.task.task_id}",
            f"/api/v2/sprint/list/?project_id={self.pid}",
        ):
            with self.subTest(url=url):
                res = self.client.get(url)
                self.assertNotEqual(res.status_code, 403, res.data)
                self.assertLess(res.status_code, 500)

    def test_the_hosts_own_people_are_not_affected(self):
        self.assertFalse(is_view_only_project(self.pid, self.a_owner.id))
        self.authenticate(self.a_owner)
        self.assert_allowed(
            self.client.post(
                "/api/v2/task/comment/",
                {"task_id": self.task.task_id, "comment_body": BODY},
                format="json",
            )
        )

    def test_a_person_in_both_teams_keeps_the_host_teams_rights(self):
        from origin.models.common.team_models import TeamMembers

        TeamMembers.objects.create(team=self.team_a, attendee=self.b_editor)
        self.assertFalse(is_view_only_project(self.pid, self.b_editor.id))

    def test_an_edit_grant_through_a_second_team_wins(self):
        from origin.models.common.team_models import ShareStatus, TeamMembers

        TeamMembers.objects.create(team=self.team_c, attendee=self.b_editor)
        self.assertTrue(is_view_only_project(self.pid, self.b_editor.id))
        ExternalGrant.objects.create(
            connection=self.grant.connection,
            owner_team=self.team_a,
            guest_team=self.team_c,
            object_type=ExternalGrant.ObjectType.PROJECT,
            object_id=str(self.pid),
            role_ceiling=EDITOR,
            status=ShareStatus.ACTIVE,
        )
        self.assertFalse(is_view_only_project(self.pid, self.b_editor.id))

    def test_a_revoked_or_pending_grant_forbids_nothing(self):
        from origin.models.common.team_models import ShareStatus

        ExternalGrant.objects.filter(pk=self.grant.pk).update(status=ShareStatus.REVOKED)
        self.assertFalse(is_view_only_project(self.pid, self.b_editor.id))

    def test_a_grant_id_stored_oddly_still_counts(self):
        ExternalGrant.objects.filter(pk=self.grant.pk).update(object_id=f"00{self.pid}")
        self.assertTrue(is_view_only_project(self.pid, self.b_editor.id))


class ViewOnlyProjectChatTests(_ViewOnlyCase):
    def setUp(self):
        super().setUp()
        self.chat = Channel.objects.filter(project=self.project).first()
        if self.chat is None:
            self.chat = Channel.objects.create(
                team=self.team_a, kind=ChannelKind.PM, project=self.project
            )

    def test_send_edit_delete_react(self):
        url = f"/api/v3/channels/{self.chat.id}/messages/"
        res = self.both(
            lambda: self.client.post(url, {"body": BODY, "body_text": "hi"}, format="json"),
            refused=lambda: self.assertFalse(Message.objects.filter(sender=self.b_editor).exists()),
        )
        message = f"/api/v3/messages/{res.data['id']}/"
        self.view_only_again()
        self.both(
            lambda: self.client.patch(message, {"body": BODY, "body_text": "edited"}, format="json")
        )
        self.view_only_again()
        self.both(lambda: self.client.post(f"{message}reactions/", {"emoji": "👍"}, format="json"))
        self.view_only_again()
        self.both(
            lambda: self.client.post(
                f"{message}attachments/",
                {"file": SimpleUploadedFile("a.txt", b"x", content_type="text/plain")},
                format="multipart",
            )
        )
        self.view_only_again()
        self.both(lambda: self.client.delete(message))

    def test_pin_a_message_for_everyone(self):
        message = Message.objects.create(
            channel=self.chat, sender=self.a_owner, seq=1, body=BODY, body_text="hi"
        )
        url = f"/api/v3/messages/{message.id}/message_pin/"
        self.both(lambda: self.client.post(url))
        self.view_only_again()
        self.assert_view_only(self.client.delete(url))

    def test_inline_upload(self):
        self.both(
            lambda: self.client.post(
                f"/api/v3/channels/{self.chat.id}/uploads/",
                {"file": SimpleUploadedFile("a.txt", b"x", content_type="text/plain")},
                format="multipart",
            )
        )

    def test_refusal_says_the_project_is_view_only(self):
        res = self.client.post(
            f"/api/v3/channels/{self.chat.id}/messages/",
            {"body": BODY, "body_text": "hi"},
            format="json",
        )
        self.assertEqual(res.data["error"], PROJECT_MESSAGE)

    def test_a_scheduled_message_is_refused(self):
        self.assertEqual(
            send_problem(str(self.b_editor.id), str(self.team_a.team_id), self.chat),
            PROJECT_MESSAGE,
        )

    def test_own_read_state_stays(self):
        res = self.client.put(
            f"/api/v3/channels/{self.chat.id}/read_cursor/", {"last_read_seq": 0}, format="json"
        )
        self.assertNotEqual(res.status_code, 403, res.data)
        res = self.client.post(f"/api/v3/channels/{self.chat.id}/pin/")
        self.assert_allowed(res)


class ViewOnlySharedChatTests(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.channel, self.grant = self.shared_chat(VIEWER)
        add_external_participants(self.grant, [self.b_editor.id], self.b_owner)
        self.authenticate(self.b_editor)
        self.url = f"/api/v3/channels/{self.channel.id}/messages/"

    def _send(self):
        return self.client.post(self.url, {"body": BODY, "body_text": "hi"}, format="json")

    def test_view_only_cannot_post_edit_can(self):
        res = self._send()
        self.assertEqual(res.status_code, 403, res.data)
        self.assertEqual(res.data, {"error": CHAT_MESSAGE, "code": VIEW_ONLY_CODE})
        self.assertFalse(Message.objects.filter(channel=self.channel).exists())
        set_role_ceiling(self.grant, EDITOR, self.a_owner)
        self.assertEqual(self._send().status_code, 201)

    def test_lowering_a_live_share_takes_posting_away(self):
        set_role_ceiling(self.grant, EDITOR, self.a_owner)
        self.assertEqual(self._send().status_code, 201)
        self.grant.refresh_from_db()
        set_role_ceiling(self.grant, VIEWER, self.a_owner)
        # The row still says editor; the grant is what is asked.
        self.assertEqual(
            ChannelMember.objects.get(channel=self.channel, user=self.b_editor).role, "admin"
        )
        self.assertEqual(self._send().status_code, 403)
        res = self.client.patch(
            f"/api/v3/channels/{self.channel.id}/", {"title": "Renamed"}, format="json"
        )
        self.assertEqual(res.status_code, 403, res.data)

    def test_the_host_posts_as_before(self):
        self.assertFalse(is_view_only_channel(self.channel, self.a_owner.id))
        self.authenticate(self.a_owner)
        self.assertEqual(self._send().status_code, 201)

    def test_leaving_stays_open(self):
        res = self.client.delete(f"/api/v3/channels/{self.channel.id}/members/{self.b_editor.id}/")
        self.assertEqual(res.status_code, 204, getattr(res, "data", None))

    def test_a_chat_shared_after_it_was_made(self):
        Channel.objects.filter(pk=self.channel.pk).update(is_external=False)
        self.assertEqual(self._send().status_code, 403)
        res = self.client.post(
            f"/api/v3/channels/{self.channel.id}/members/",
            {"user_ids": [str(self.a_editor.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 403, res.data)


class ViewOnlyNotesAndEditorTests(_ViewOnlyCase):
    def setUp(self):
        super().setUp()
        self.note = TaskNoteMaster.objects.create(
            team=self.team_a,
            project=self.project,
            task=self.task,
            owner=self.a_owner,
            title="Spec",
            body=BODY,
        )

    def _collab(self, name):
        return self.client.post(
            "/api/v2/collab/access/check/", {"document_name": name}, format="json"
        )

    def test_the_editor_is_held_read_only(self):
        for name in (f"task-note:{self.note.note_id}", f"task-body:{self.task.task_id}"):
            with self.subTest(name=name):
                res = self._collab(name)
                self.assertEqual(res.status_code, 200, res.data)
                self.assertTrue(res.data.get("archived"), res.data)
                self.assertEqual(res.data["role_id"], 3)
        self.allow_editing()
        res = self._collab(f"task-note:{self.note.note_id}")
        self.assertNotIn("archived", res.data)

    def test_task_note_writes(self):
        def title():
            return TaskNoteMaster.objects.get(pk=self.note.pk).title

        self.assert_view_only(
            self.client.put(
                "/api/v2/note/task/",
                {
                    "team_id": self.team_id,
                    "note_id": self.note.note_id,
                    "title": "Changed",
                    "body": BODY,
                },
                format="json",
            )
        )
        self.assertEqual(title(), "Spec")
        self.assert_view_only(
            self.client.post(
                "/api/v2/note/task/",
                {
                    "team_id": self.team_id,
                    "project_id": self.pid,
                    "task_id": self.task.task_id,
                    "user_id": str(self.b_editor.id),
                    "title": "New",
                    "body": BODY,
                },
                format="json",
            )
        )
        self.assertEqual(TaskNoteMaster.objects.count(), 1)

    def test_the_agent_note_tool(self):
        ctx = ToolContext(team_id=self.team_id, user_id=str(self.b_editor.id))
        args = {
            "note_type": "task",
            "project_id": self.pid,
            "title": "From agent",
            "body_markdown": "hi",
        }
        with self.assertRaises(ToolError) as caught:
            CREATE_NOTE.run(args, ctx)
        self.assertIn(PROJECT_MESSAGE, str(caught.exception))
        self.assertEqual(TaskNoteMaster.objects.count(), 1)

    def test_sharing_the_note_on(self):
        from origin.models.note.common_note_models import NotePermissionMaster

        NotePermissionMaster.objects.create(
            team=self.team_a,
            user=self.b_editor,
            note_type=2,
            note_id=self.note.note_id,
            role_id=1,
        )
        with self.assertRaises(SharingRefused) as caught:
            share_note(
                team_id=self.team_id,
                granter_id=self.b_editor.id,
                note_type=2,
                note_id=self.note.note_id,
                target_user_id=self.a_viewer.id,
                role_id=3,
            )
        self.assertEqual(caught.exception.status, 403)

    def test_chat_note_on_the_project_chat(self):
        chat = Channel.objects.filter(project=self.project).first() or Channel.objects.create(
            team=self.team_a, kind=ChannelKind.PM, project=self.project
        )
        self.assert_view_only(
            self.client.post(
                "/api/v2/note/chat/",
                {
                    "team_id": self.team_id,
                    "chat_id": str(chat.id),
                    "user_id": str(self.b_editor.id),
                    "title": "x",
                    "body": BODY,
                },
                format="json",
            )
        )
        self.assertFalse(ChatNoteMaster.objects.exists())


class ViewOnlyAgentGateTests(_ViewOnlyCase):
    """The MCP gate and the in-app agent's approved writes share one check, with the caller named."""

    def test_the_gate(self):
        args = {"task_id": self.task.task_id, "body_text": "hello"}
        self.assertEqual(archived_refusal_for_args(args, str(self.b_editor.id)), PROJECT_MESSAGE)
        self.assertEqual(
            _archived_write_refusal("add_comment", args, str(self.b_editor.id)), PROJECT_MESSAGE
        )
        self.assertIsNone(archived_refusal_for_args(args, str(self.a_owner.id)))
        self.allow_editing()
        self.assertIsNone(archived_refusal_for_args(args, str(self.b_editor.id)))

    def test_create_task_tool_without_a_request_user(self):
        ctx = ToolContext(team_id=self.team_id, user_id=str(self.b_editor.id))
        with mock.patch(
            "origin.search_engine.agent.tools.create_task.check_monthly_creation_quota",
            return_value=None,
            create=True,
        ):
            with self.assertRaises(ToolError) as caught:
                CREATE_TASK.run({"project_id": self.pid, "title": "Agent task"}, ctx)
        self.assertIn(PROJECT_MESSAGE, str(caught.exception))
        self.assertFalse(TaskMaster.objects.filter(title="Agent task").exists())


@override_settings(SEARCH_ENGINE=_mcp_enabled_everywhere())
class ViewOnlyMcpAndPublicApiTests(_ViewOnlyCase):
    """The same person over their own API key, on MCP and the public REST API."""

    def setUp(self):
        super().setUp()
        raw = generate_key()
        ApiKey.objects.create(
            user=self.b_editor,
            team=None,
            name="mcp",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=SCOPE_WRITE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")

    def _tool(self, name, arguments, team_id=None):
        res = self.client.post(
            f"{MCP}?team_id={team_id or self.team_id}",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res.data["result"]

    def test_mcp_does_not_open_the_host_team_to_their_key(self):
        res = self.client.post(
            f"{MCP}?team_id={self.team_id}",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            format="json",
        )
        self.assertEqual(res.status_code, 403, res.data)

    def test_mcp_through_their_own_team_cannot_write_the_shared_project(self):
        calls = (
            ("add_comment", {"task_id": self.task.task_id, "body_text": "hello"}),
            ("update_task", {"task_id": self.task.task_id, "title": "Changed"}),
            ("create_task", {"project_id": self.pid, "title": "From MCP"}),
        )
        for name, arguments in calls:
            with self.subTest(tool=name):
                result = self._tool(name, arguments, team_id=str(self.team_b.team_id))
                self.assertTrue(result.get("isError"), result)
        self.assertEqual(TaskComments.objects.count(), 0)
        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).title, "Host task")
        self.assertFalse(TaskMaster.objects.filter(title="From MCP").exists())

    def test_public_api_update(self):
        url = f"/api/public/v1/tasks/{self.task.task_id}/"

        def title():
            return TaskMaster.objects.get(pk=self.task.pk).title

        self.both(
            lambda: self.client.patch(url, {"title": "Changed"}, format="json"),
            refused=lambda: self.assertEqual(title(), "Host task"),
            allowed=lambda: self.assertEqual(title(), "Changed"),
        )


class ViewOnlyUndoTests(_ViewOnlyCase):
    def test_an_agent_write_cannot_be_undone_once_view_only(self):
        undoing = Undoing(
            user=self.b_editor, run=SimpleNamespace(team_id=self.team_a.team_id), step=None
        )
        with self.assertRaises(UndoRefused) as caught:
            _locked_task(undoing, self.task.task_id)
        self.assertEqual(caught.exception.status, 403)
        self.allow_editing()
        with transaction.atomic():
            self.assertEqual(_locked_task(undoing, self.task.task_id).pk, self.task.pk)
