"""MIP-842 — the agent's approval card labels only what the caller may read.

`_friendly_arguments` swaps the raw ids in a proposed write for names. The
ids come from the model, so a model steered by an injected task body, note
or chat message can propose an id the caller cannot read. Each case below
pairs a caller who MAY read the object (the label appears) with one who may
not (the raw id stays, and no title, name or snapshot is added). The rule
under test is `label_acl.LabelAccess`, which mirrors the matching read tool.

Callers: `self.user` is the project's member and owner; `self.user2` is in
the same TEAM but not in the project (the usual "can't read" case);
`self.stranger` belongs to another team entirely.
"""

from datetime import date

from django.contrib.auth import get_user_model

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectLabel, ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.create_note import _run as create_note_run
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase


class ApprovalCardAclBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = self._project(self.team, "Website Redesign", "WRD", self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = self._task(self.project, "Launch checklist", self.user)

        # A second project only user2 belongs to — user can't read it.
        self.secret = self._project(self.team, "Layoffs", "SEC", self.user2)
        ProjectMembers.objects.create(team=self.team, project=self.secret, attendee=self.user2)
        self.secret_task = self._task(self.secret, "Reorg list", self.user2)

        self.stranger = get_user_model().objects.create_user(
            username="stranger", email="stranger@example.com", password="x"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.stranger)

    # ---- fixtures ------------------------------------------------------

    def _project(self, team, name, code, owner):
        return ProjectMaster.objects.create(
            team=team, project_name=name, code=code, owner=owner, project_system_user=owner
        )

    def _task(self, project, title, reporter, **kwargs):
        return TaskMaster.objects.create(
            team=project.team, project=project, reporter=reporter, title=title, **kwargs
        )

    def _milestone(self, project, title, reporter):
        milestone = MilestoneMaster.objects.create(
            team=project.team, project=project, reporter=reporter, title=title
        )
        ensure_backing_task(milestone)
        return milestone

    # ---- calling the function under test --------------------------------

    def _ctx(self, user, team=None):
        return ToolContext(team_id=str((team or self.team).team_id), user_id=str(user.id))

    def _card(self, args, tool_name, user, team=None):
        return controller._friendly_arguments(args, tool_name, ctx=self._ctx(user, team))


class TaskLabelTests(ApprovalCardAclBase):
    def test_a_project_member_sees_the_display_id(self):
        out = self._card({"task_id": self.task.task_id}, "update_task", self.user)
        self.assertEqual(out["task_id"], self.task.display_id)

    def test_a_same_team_non_member_gets_the_raw_id(self):
        out = self._card({"task_id": self.task.task_id}, "update_task", self.user2)
        self.assertEqual(out["task_id"], self.task.task_id)

    def test_parent_task_id_follows_the_same_rule(self):
        args = {"parent_task_id": self.task.task_id}
        self.assertEqual(
            self._card(args, "create_task", self.user)["parent_task_id"], self.task.display_id
        )
        self.assertEqual(
            self._card(args, "create_task", self.user2)["parent_task_id"], self.task.task_id
        )

    def test_the_assignee_can_read_without_being_a_project_member(self):
        self.task.assignee = self.user2
        self.task.save()
        out = self._card({"task_id": self.task.task_id}, "update_task", self.user2)
        self.assertEqual(out["task_id"], self.task.display_id)

    def test_a_task_in_another_team_is_raw_even_for_a_project_member(self):
        """The caller is a member of the project but runs the agent in the
        other team: the tenant check decides, not the membership row."""
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)
        out = self._card(
            {"task_id": self.task.task_id}, "update_task", self.user, team=self.other_team
        )
        self.assertEqual(out["task_id"], self.task.task_id)

    def test_a_deleted_task_is_raw(self):
        self.task.is_deleted = True
        self.task.save()
        out = self._card({"task_id": self.task.task_id}, "update_task", self.user)
        self.assertEqual(out["task_id"], self.task.task_id)


class ProjectLabelTests(ApprovalCardAclBase):
    def test_a_member_sees_the_name(self):
        out = self._card({"project_id": self.project.project_id}, "create_task", self.user)
        self.assertEqual(out["project_id"], "Website Redesign")

    def test_a_non_member_gets_the_raw_id(self):
        out = self._card({"project_id": self.project.project_id}, "create_task", self.user2)
        self.assertEqual(out["project_id"], self.project.project_id)

    def test_another_team_gets_the_raw_id(self):
        out = self._card(
            {"project_id": self.project.project_id},
            "create_task",
            self.stranger,
            team=self.other_team,
        )
        self.assertEqual(out["project_id"], self.project.project_id)

    def test_a_deleted_project_is_raw(self):
        self.project.is_deleted = True
        self.project.save()
        out = self._card({"project_id": self.project.project_id}, "create_task", self.user)
        self.assertEqual(out["project_id"], self.project.project_id)


class MilestoneLabelTests(ApprovalCardAclBase):
    def setUp(self):
        super().setUp()
        self.milestone = self._milestone(self.project, "Q4 launch", self.user)

    def test_a_project_member_sees_the_title(self):
        args = {"milestone_id": self.milestone.milestone_id}
        out = self._card(args, "update_task", self.user)
        self.assertEqual(out["milestone_id"], "Q4 launch")

    def test_existing_milestone_id_follows_the_same_rule(self):
        args = {"existing_milestone_id": self.milestone.milestone_id}
        self.assertEqual(
            self._card(args, "create_task_plan", self.user)["existing_milestone_id"], "Q4 launch"
        )
        self.assertEqual(
            self._card(args, "create_task_plan", self.user2)["existing_milestone_id"],
            self.milestone.milestone_id,
        )

    def test_a_non_member_gets_the_raw_id(self):
        args = {"milestone_id": self.milestone.milestone_id}
        out = self._card(args, "update_task", self.user2)
        self.assertEqual(out["milestone_id"], self.milestone.milestone_id)

    def test_a_milestone_assignee_can_read_without_being_a_member(self):
        MilestoneAssignees.objects.create(team=self.team, milestone=self.milestone, user=self.user2)
        args = {"milestone_id": self.milestone.milestone_id}
        self.assertEqual(self._card(args, "update_task", self.user2)["milestone_id"], "Q4 launch")

    def test_another_team_gets_the_raw_id(self):
        args = {"milestone_id": self.milestone.milestone_id}
        out = self._card(args, "update_task", self.stranger, team=self.other_team)
        self.assertEqual(out["milestone_id"], self.milestone.milestone_id)

    def test_a_project_member_running_the_agent_in_another_team_gets_the_raw_id(self):
        """Membership alone isn't enough: the milestone's own team decides."""
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)
        args = {"milestone_id": self.milestone.milestone_id}
        out = self._card(args, "update_task", self.user, team=self.other_team)
        self.assertEqual(out["milestone_id"], self.milestone.milestone_id)


class UserLabelTests(ApprovalCardAclBase):
    def test_a_team_member_is_named(self):
        out = self._card({"assignee_id": str(self.user.id)}, "assign_task", self.user2)
        self.assertEqual(out["assignee_id"], "testuser")

    def test_every_user_key_is_covered(self):
        out = self._card(
            {
                "assignee_id": str(self.user.id),
                "reporter_id": str(self.user.id),
                "new_assignee_id": str(self.user.id),
            },
            "assign_task",
            self.user2,
        )
        self.assertEqual(set(out.values()), {"testuser"})

    def test_a_user_outside_the_team_stays_a_uuid(self):
        uid = str(self.stranger.id)
        out = self._card({"assignee_id": uid}, "assign_task", self.user)
        self.assertEqual(out["assignee_id"], uid)

    def test_a_removed_member_stays_a_uuid(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        uid = str(self.user2.id)
        out = self._card({"assignee_id": uid}, "assign_task", self.user)
        self.assertEqual(out["assignee_id"], uid)

    def test_a_differently_spelled_uuid_still_resolves(self):
        upper = str(self.user.id).upper()
        out = self._card({"assignee_id": upper}, "assign_task", self.user2)
        self.assertEqual(out["assignee_id"], "testuser")

    def test_junk_is_left_alone(self):
        out = self._card({"assignee_id": "not-a-uuid"}, "assign_task", self.user)
        self.assertEqual(out["assignee_id"], "not-a-uuid")


class ProjectMemberLabelTests(ApprovalCardAclBase):
    """`manage_project_members` names the person it adds, and `manage_project`
    the people a new project starts with."""

    def test_the_person_being_added_is_named(self):
        out = self._card(
            {"action": "add", "project_id": self.project.project_id, "user_id": str(self.user2.id)},
            "manage_project_members",
            self.user,
        )
        self.assertEqual(out["user_id"], self.user2.username)
        self.assertEqual(out["project_id"], "Website Redesign")

    def test_a_person_outside_the_team_stays_a_uuid(self):
        uid = str(self.stranger.id)
        out = self._card({"action": "add", "user_id": uid}, "manage_project_members", self.user)
        self.assertEqual(out["user_id"], uid)

    def test_new_project_members_are_named_in_order(self):
        out = self._card(
            {
                "action": "create",
                "name": "Kitchen",
                "member_ids": [str(self.user2.id), str(self.user.id)],
            },
            "manage_project",
            self.user,
        )
        self.assertEqual(out["member_ids"], [self.user2.username, self.user.username])

    def test_an_entry_the_caller_may_not_see_stays_as_written(self):
        stranger = str(self.stranger.id)
        out = self._card(
            {
                "action": "create",
                "name": "Kitchen",
                "member_ids": [str(self.user2.id), stranger, "junk"],
            },
            "manage_project",
            self.user,
        )
        self.assertEqual(out["member_ids"], [self.user2.username, stranger, "junk"])

    def test_a_differently_spelled_id_in_the_list_still_resolves(self):
        out = self._card(
            {"action": "create", "name": "Kitchen", "member_ids": [str(self.user2.id).upper()]},
            "manage_project",
            self.user,
        )
        self.assertEqual(out["member_ids"], [self.user2.username])

    def test_something_that_is_not_a_list_is_left_alone(self):
        out = self._card(
            {"action": "create", "name": "Kitchen", "member_ids": str(self.user2.id)},
            "manage_project",
            self.user,
        )
        self.assertEqual(out["member_ids"], str(self.user2.id))

    def test_an_empty_list_stays_empty(self):
        out = self._card(
            {"action": "create", "name": "Kitchen", "member_ids": []}, "manage_project", self.user
        )
        self.assertEqual(out["member_ids"], [])

    def test_the_stored_arguments_keep_the_ids(self):
        """Only the wire copy is relabelled; the resume path re-runs the tool
        with the canonical ids."""
        args = {"action": "create", "name": "Kitchen", "member_ids": [str(self.user2.id)]}
        self._card(args, "manage_project", self.user)
        self.assertEqual(args["member_ids"], [str(self.user2.id)])


class TodoLabelTests(ApprovalCardAclBase):
    def setUp(self):
        super().setUp()
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=date(2026, 11, 1)
        )
        self.item = ToDoItem.objects.create(group=group, title="Call the lawyer", sort_order=1)

    def test_the_owner_sees_the_title(self):
        out = self._card({"item_id": self.item.item_id}, "update_todo_item", self.user)
        self.assertEqual(out["item_id"], "Call the lawyer")

    def test_parent_item_id_follows_the_same_rule(self):
        args = {"parent_item_id": self.item.item_id}
        self.assertEqual(
            self._card(args, "create_todo_item", self.user)["parent_item_id"], "Call the lawyer"
        )
        self.assertEqual(
            self._card(args, "create_todo_item", self.user2)["parent_item_id"], self.item.item_id
        )

    def test_a_teammate_gets_the_raw_id(self):
        out = self._card({"item_id": self.item.item_id}, "update_todo_item", self.user2)
        self.assertEqual(out["item_id"], self.item.item_id)

    def test_another_team_gets_the_raw_id(self):
        out = self._card(
            {"item_id": self.item.item_id}, "update_todo_item", self.user, team=self.other_team
        )
        self.assertEqual(out["item_id"], self.item.item_id)


class FolderLabelTests(ApprovalCardAclBase):
    def setUp(self):
        super().setUp()
        self.folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name="Private research"
        )

    def test_the_owner_sees_the_name(self):
        out = self._card({"folder_id": self.folder.folder_id}, "create_note", self.user)
        self.assertEqual(out["folder_id"], "Private research")

    def test_a_teammate_gets_the_raw_id(self):
        out = self._card({"folder_id": self.folder.folder_id}, "create_note", self.user2)
        self.assertEqual(out["folder_id"], self.folder.folder_id)

    def test_a_team_folder_is_named_only_for_someone_with_a_role_on_it(self):
        team_folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Board notes",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE,
        )
        args = {"folder_id": team_folder.folder_id}
        self.assertEqual(self._card(args, "create_note", self.user)["folder_id"], "Board notes")
        self.assertEqual(
            self._card(args, "create_note", self.user2)["folder_id"], team_folder.folder_id
        )

    def test_another_team_gets_the_raw_id(self):
        out = self._card(
            {"folder_id": self.folder.folder_id}, "create_note", self.user, team=self.other_team
        )
        self.assertEqual(out["folder_id"], self.folder.folder_id)


class NoteTitleTests(ApprovalCardAclBase):
    def _title(self, note_type, note_id, user, team=None):
        out = self._card({"note_type": note_type, "note_id": note_id}, "update_note", user, team)
        self.assertEqual(out["note_id"], note_id)  # the raw id always stays
        return out.get("note_title")

    def test_a_personal_note_titles_only_for_its_owner(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Salary bands", body=[]
        )
        self.assertEqual(self._title("personal", note.note_id, self.user), "Salary bands")
        self.assertIsNone(self._title("personal", note.note_id, self.user2))

    def test_a_task_note_titles_for_project_members_only(self):
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, owner=self.user, title="Perf research", body=[]
        )
        self.assertEqual(self._title("task", note.note_id, self.user), "Perf research")
        self.assertIsNone(self._title("task", note.note_id, self.user2))

    def _team_note(self, visibility):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Handbook",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )
        note = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="On-call rota",
            body=[],
            folder_id=folder.folder_id,
        )
        return folder, note

    def test_a_note_in_a_public_team_folder_titles_for_any_teammate(self):
        """The ACL set for a public folder is a `team:<id>` sentinel, not user
        ids, so membership in that set would hide the title from a teammate
        who can edit the note."""
        _folder, note = self._team_note(PersonalNoteFolder.VISIBILITY_PUBLIC)
        self.assertEqual(self._title("team", note.note_id, self.user2), "On-call rota")

    def test_a_note_in_a_private_team_folder_titles_only_with_a_grant(self):
        folder, note = self._team_note(PersonalNoteFolder.VISIBILITY_PRIVATE)
        self.assertIsNone(self._title("team", note.note_id, self.user2))
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=self.user2, role_id=3
        )
        self.assertEqual(self._title("team", note.note_id, self.user2), "On-call rota")

    def test_a_public_team_folder_note_is_still_raw_in_another_team(self):
        _folder, note = self._team_note(PersonalNoteFolder.VISIBILITY_PUBLIC)
        self.assertIsNone(self._title("team", note.note_id, self.stranger, team=self.other_team))

    def test_another_team_never_gets_a_title(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Salary bands", body=[]
        )
        self.assertIsNone(self._title("personal", note.note_id, self.user, team=self.other_team))


class ChatLabelTests(ApprovalCardAclBase):
    def setUp(self):
        super().setUp()
        self.carol = get_user_model().objects.create_user(
            username="carol", email="carol@example.com", password="x"
        )
        # A GM carol owns: self.user is a member, self.user2 is not.
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Board", owner=self.carol
        )
        ChannelMember.objects.create(channel=self.gm, user=self.carol, role="owner")
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="member")

    def test_a_member_sees_the_chat_name(self):
        out = self._card(
            {"note_type": "chat", "chat_id": str(self.gm.id)}, "create_note", self.user
        )
        self.assertEqual(out["chat_name"], "Board")

    def test_a_non_member_gets_no_chat_name(self):
        out = self._card(
            {"note_type": "chat", "chat_id": str(self.gm.id)}, "create_note", self.user2
        )
        self.assertNotIn("chat_name", out)

    def test_an_untitled_chat_does_not_list_its_members_to_a_non_member(self):
        self.gm.title = ""
        self.gm.save()
        args = {"note_type": "chat", "chat_id": str(self.gm.id)}
        self.assertIn("carol", self._card(args, "create_note", self.user)["chat_name"])
        self.assertNotIn("chat_name", self._card(args, "create_note", self.user2))

    def test_a_chat_note_titles_for_chat_members_only(self):
        made = create_note_run(
            {
                "note_type": "chat",
                "title": "Decisions",
                "content_text": "We ship",
                "chat_id": str(self.gm.id),
            },
            self._ctx(self.user),
        )
        args = {"note_type": "chat", "note_id": made["note_id"]}
        self.assertEqual(self._card(args, "update_note", self.user)["note_title"], "Decisions")
        self.assertNotIn("note_title", self._card(args, "update_note", self.user2))


class NestedEnricherTests(ApprovalCardAclBase):
    def test_a_bulk_row_for_an_unreadable_task_is_left_exactly_as_proposed(self):
        readable = {"task_id": self.task.task_id, "priority": "High"}
        unreadable = {"task_id": self.secret_task.task_id, "priority": "Low"}
        out = self._card({"updates": [readable, unreadable]}, "update_tasks_bulk", self.user)
        good, bad = out["updates"]
        self.assertEqual(
            (good["display_id"], good["title"]), (self.task.display_id, "Launch checklist")
        )
        self.assertIn("current", good)
        self.assertEqual(bad, unreadable)  # no display_id, no title, no snapshot

    def test_a_bulk_move_into_an_unreadable_milestone_keeps_the_raw_id(self):
        secret_ms = self._milestone(self.secret, "Reorg Q1", self.user2)
        row = {"task_id": self.task.task_id, "milestone_id": secret_ms.milestone_id}
        out = self._card({"updates": [row]}, "update_tasks_bulk", self.user)
        self.assertEqual(out["updates"][0]["milestone_id"], secret_ms.milestone_id)

    def test_a_task_sitting_in_an_unreadable_milestone_does_not_name_it(self):
        secret_ms = self._milestone(self.secret, "Reorg Q1", self.user2)
        self.task.milestone = secret_ms
        self.task.save()
        out = self._card(
            {"updates": [{"task_id": self.task.task_id, "priority": "High"}]},
            "update_tasks_bulk",
            self.user,
        )
        current = out["updates"][0]["current"]["milestone_id"]
        self.assertEqual(current, secret_ms.milestone_id)  # not the title, not None

    def test_update_task_adds_current_only_for_a_readable_task(self):
        mine = self._milestone(self.project, "Q4 launch", self.user)
        self.task.milestone = mine
        self.task.save()
        args = {"task_id": self.task.task_id, "milestone_id": ""}
        self.assertEqual(
            self._card(args, "update_task", self.user)["current"]["milestone_id"], "Q4 launch"
        )
        self.assertNotIn("current", self._card(args, "update_task", self.user2))

    def test_update_task_shows_the_current_and_proposed_parent(self):
        old_parent = self._task(self.project, "Old parent", self.user)
        new_parent = self._task(self.project, "New parent", self.user)
        self.task.parent_task_id = old_parent.task_id
        self.task.save()
        args = {"task_id": self.task.task_id, "parent_task_id": new_parent.task_id}

        out = self._card(args, "update_task", self.user)

        self.assertEqual(out["parent_task_id"], new_parent.display_id)
        self.assertEqual(out["current"]["parent_task_id"], old_parent.display_id)

    def test_making_a_task_top_level_shows_the_parent_it_leaves(self):
        old_parent = self._task(self.project, "Old parent", self.user)
        self.task.parent_task_id = old_parent.task_id
        self.task.save()

        out = self._card(
            {"task_id": self.task.task_id, "parent_task_id": ""}, "update_task", self.user
        )

        self.assertEqual(out["parent_task_id"], "")
        self.assertEqual(out["current"]["parent_task_id"], old_parent.display_id)

    def test_a_parent_the_caller_cannot_read_stays_a_raw_id_either_side(self):
        self.task.parent_task_id = self.secret_task.task_id
        self.task.save()

        leaving = self._card(
            {"task_id": self.task.task_id, "parent_task_id": ""}, "update_task", self.user
        )
        joining = self._card(
            {"task_id": self.task.task_id, "parent_task_id": self.secret_task.task_id},
            "update_task",
            self.user,
        )

        self.assertEqual(leaving["current"]["parent_task_id"], self.secret_task.task_id)
        self.assertEqual(joining["parent_task_id"], self.secret_task.task_id)

    def test_update_task_shows_the_current_and_proposed_project(self):
        other = self._project(self.team, "Other", "OTH", self.user)
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)

        out = self._card(
            {"task_id": self.task.task_id, "project_id": other.project_id}, "update_task", self.user
        )

        self.assertEqual(out["project_id"], "Other")
        self.assertEqual(out["current"]["project_id"], "Website Redesign")

    def test_a_project_the_caller_is_not_in_stays_a_raw_id(self):
        out = self._card(
            {"task_id": self.task.task_id, "project_id": self.secret.project_id},
            "update_task",
            self.user,
        )

        self.assertEqual(out["project_id"], self.secret.project_id)

    def test_no_current_is_added_when_nothing_is_being_moved(self):
        out = self._card({"task_id": self.task.task_id, "title": "x"}, "update_task", self.user)

        self.assertNotIn("current", out)

    def test_dependency_ends_are_labelled_per_task(self):
        edge = {"blocker_task_id": self.task.task_id, "blocked_task_id": self.secret_task.task_id}
        out = self._card({"add": [edge]}, "update_task_dependencies", self.user)
        row = out["add"][0]
        self.assertEqual(row["blocker_task_id"], f"{self.task.display_id} Launch checklist")
        self.assertEqual(row["blocked_task_id"], self.secret_task.task_id)  # not "SEC-1 Reorg list"

    def test_a_task_plan_names_only_people_on_the_team(self):
        raw = {
            "project_id": self.project.project_id,
            "milestone": {"title": "M", "assignee_ids": [str(self.stranger.id)]},
            "tasks": [{"title": "T", "assignee_id": str(self.user2.id)}],
        }
        out = self._card(raw, "create_task_plan", self.user)
        self.assertEqual(out["tasks"][0]["assignee_id"], "otheruser")
        self.assertEqual(out["milestone"]["assignee_ids"], [str(self.stranger.id)])


class FailClosedTests(ApprovalCardAclBase):
    def test_a_context_with_no_user_or_team_labels_nothing(self):
        ctx = ToolContext(team_id="", user_id="")
        raw = {
            "task_id": self.task.task_id,
            "project_id": self.project.project_id,
            "assignee_id": str(self.user.id),
        }
        self.assertEqual(controller._friendly_arguments(dict(raw), "update_task", ctx=ctx), raw)

    def test_a_context_missing_either_half_is_not_usable(self):
        """Defence in depth: today the empty id would also match no row, but
        no rule should have to rely on that."""
        uid, tid = str(self.user.id), str(self.team.team_id)
        self.assertTrue(LabelAccess(ToolContext(team_id=tid, user_id=uid)).usable)
        self.assertFalse(LabelAccess(ToolContext(team_id="", user_id=uid)).usable)
        self.assertFalse(LabelAccess(ToolContext(team_id=tid, user_id="")).usable)

    def test_the_caller_is_required(self):
        """A call site that forgets `ctx` must fail loudly, not label for no one."""
        with self.assertRaises(TypeError):
            controller._friendly_arguments({"task_id": self.task.task_id}, "update_task")

    def test_the_input_is_never_mutated(self):
        raw = {"task_id": self.task.task_id, "project_id": self.project.project_id}
        self._card(raw, "update_task", self.user)
        self.assertEqual(raw, {"task_id": self.task.task_id, "project_id": self.project.project_id})


class ProjectLabelCardTests(ApprovalCardAclBase):
    """`manage_project_labels` names the labels it puts on a project or takes off. Labels
    belong to the team, and the label catalogue is readable by any member of it, so the rule
    is the team check and nothing finer."""

    def setUp(self):
        super().setUp()
        self.client_work = ProjectLabel.objects.create(
            team=self.team, name="Client Work", color="#7c3aed", text_color="white"
        )
        self.q3 = ProjectLabel.objects.create(
            team=self.team, name="Q3", color="#111111", text_color="white"
        )
        self.theirs = ProjectLabel.objects.create(
            team=self.other_team, name="Theirs", color="#222222", text_color="white"
        )

    def card(self, ids, user=None, **extra):
        return self._card(
            {"action": "assign", "project_id": self.project.project_id, "label_ids": ids, **extra},
            "manage_project_labels",
            user or self.user,
        )

    def test_the_labels_are_named_in_the_order_given(self):
        out = self.card([self.q3.label_id, self.client_work.label_id])
        self.assertEqual(out["label_ids"], ["Q3", "Client Work"])
        self.assertEqual(out["project_id"], "Website Redesign")

    def test_a_teammate_who_is_not_on_the_project_sees_the_names_too(self):
        # The catalogue is the team's: reading it needs the team, not the project.
        out = self.card([self.client_work.label_id], self.user2)
        self.assertEqual(out["label_ids"], ["Client Work"])

    def test_another_teams_label_and_an_unknown_id_stay_as_written(self):
        out = self.card([self.client_work.label_id, self.theirs.label_id, 999999])
        self.assertEqual(out["label_ids"], ["Client Work", self.theirs.label_id, 999999])

    def test_someone_outside_the_team_gets_no_names(self):
        out = self.card([self.client_work.label_id], self.stranger)
        self.assertEqual(out["label_ids"], [self.client_work.label_id])

    def test_digits_as_text_resolve_and_junk_stays(self):
        out = self.card([str(self.q3.label_id), "junk", None, True, 1.5])
        self.assertEqual(out["label_ids"], ["Q3", "junk", None, True, 1.5])

    def test_true_is_not_label_one(self):
        """`int(True)` is 1. Label 1 is made to exist, and shown to resolve, so that this can
        tell a guard from a lookup that finds nothing."""
        ProjectLabel.objects.filter(label_id=1).delete()
        ProjectLabel.objects.create(
            label_id=1, team=self.team, name="Label One", color="#111111", text_color="white"
        )
        self.assertEqual(self.card([1])["label_ids"], ["Label One"])
        self.assertEqual(self.card([True])["label_ids"], [True])
        self.assertEqual(self.card([False, 1])["label_ids"], [False, "Label One"])

    def test_something_that_is_not_a_list_is_left_alone(self):
        out = self.card(str(self.q3.label_id))
        self.assertEqual(out["label_ids"], str(self.q3.label_id))

    def test_an_empty_list_stays_empty(self):
        self.assertEqual(self.card([])["label_ids"], [])

    def test_the_label_name_in_a_create_is_left_as_the_model_wrote_it(self):
        out = self._card(
            {"action": "create", "project_id": self.project.project_id, "name": "New one"},
            "manage_project_labels",
            self.user,
        )
        self.assertEqual(out["name"], "New one")
        self.assertEqual(out["project_id"], "Website Redesign")

    def test_the_stored_arguments_keep_the_ids(self):
        args = {
            "action": "assign",
            "project_id": self.project.project_id,
            "label_ids": [self.q3.label_id],
        }
        self._card(args, "manage_project_labels", self.user)
        self.assertEqual(args["label_ids"], [self.q3.label_id])
