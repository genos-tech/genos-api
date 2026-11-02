"""The approval card for `manage_milestone` / `delete_milestone` (MIP-879).

The card swaps raw ids for names, and the ids come from the model, so each
label is printed only for what the caller may read (`LabelAccess`, MIP-842).
Specific to milestones:

* A milestone folder is another table than a note folder. Its argument is
  `milestone_folder_id` so the note-folder resolver (keyed on `folder_id`) can
  never label it, and a note folder with the same number can't lend its name.
* `assignee_ids` is a list, below the flat pass, so a nested enricher names the
  people.
* An update gets a `current` snapshot of the fields it changes, for old -> new.
* After the run, a milestone action gets the milestone's chip, a folder action
  its project's, and a delete only the project (the milestone is gone).
"""

from __future__ import annotations

from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneFolder
from origin.models.task.sprint_models import Sprint
from origin.search_engine.agent import controller
from origin.search_engine.agent.label_acl import LabelAccess

from .test_agent_approval_card_label_acl import ApprovalCardAclBase


class MilestoneCardBase(ApprovalCardAclBase):
    def setUp(self):
        super().setUp()
        self.folder = MilestoneFolder.objects.create(
            team=self.team, project=self.project, name="Launch"
        )
        self.secret_folder = MilestoneFolder.objects.create(
            team=self.team, project=self.secret, name="Reorg"
        )
        self.sprint = self._sprint(self.project, "Sprint 1")
        self.secret_sprint = self._sprint(self.secret, "Hidden sprint")
        self.milestone = self._milestone(self.project, "Q4 launch", self.user)

    def _sprint(self, project, name):
        return Sprint.objects.create(
            team=self.team,
            project=project,
            name=name,
            sequence_number=1,
            start_date="2026-10-01",
            end_date="2026-10-14",
        )


class FolderAndSprintLabelTests(MilestoneCardBase):
    def test_a_project_member_sees_the_folder_and_sprint_names(self):
        out = self._card(
            {
                "action": "update",
                "milestone_id": self.milestone.milestone_id,
                "milestone_folder_id": self.folder.folder_id,
                "sprint_id": self.sprint.sprint_id,
            },
            "manage_milestone",
            self.user,
        )
        self.assertEqual(out["milestone_folder_id"], "Launch")
        self.assertEqual(out["sprint_id"], "Sprint 1")
        self.assertEqual(out["milestone_id"], "Q4 launch")

    def test_a_folder_or_sprint_in_a_project_the_caller_is_not_in_stays_raw(self):
        out = self._card(
            {
                "action": "update",
                "milestone_id": self.milestone.milestone_id,
                "milestone_folder_id": self.secret_folder.folder_id,
                "sprint_id": self.secret_sprint.sprint_id,
            },
            "manage_milestone",
            self.user,
        )
        self.assertEqual(out["milestone_folder_id"], self.secret_folder.folder_id)
        self.assertEqual(out["sprint_id"], self.secret_sprint.sprint_id)

    def test_nothing_is_labelled_across_teams(self):
        out = self._card(
            {
                "action": "rename_folder",
                "milestone_folder_id": self.folder.folder_id,
                "folder_name": "x",
            },
            "manage_milestone",
            self.user,
            team=self.other_team,
        )
        self.assertEqual(out["milestone_folder_id"], self.folder.folder_id)

    def test_a_deleted_sprint_stays_raw(self):
        Sprint.objects.filter(pk=self.sprint.pk).update(is_deleted=True)
        out = self._card(
            {"action": "update", "milestone_id": 1, "sprint_id": self.sprint.sprint_id},
            "manage_milestone",
            self.user,
        )
        self.assertEqual(out["sprint_id"], self.sprint.sprint_id)

    def test_a_note_folder_with_the_same_number_never_lends_its_name(self):
        """The two kinds of folder have separate id spaces. Give a note folder
        the milestone folder's id: the card must still say the milestone
        folder's name, never the note folder's."""
        PersonalNoteFolder.objects.create(
            folder_id=self.folder.folder_id,
            team=self.team,
            owner=self.user,
            name="My private notes",
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
        )
        out = self._card(
            {
                "action": "update",
                "milestone_id": self.milestone.milestone_id,
                "milestone_folder_id": self.folder.folder_id,
            },
            "manage_milestone",
            self.user,
        )
        self.assertEqual(out["milestone_folder_id"], "Launch")

    def test_a_milestone_folder_is_not_labelled_when_only_a_note_folder_has_that_number(self):
        gone = MilestoneFolder.objects.create(team=self.team, project=self.project, name="Temp")
        number = gone.folder_id
        gone.delete()
        PersonalNoteFolder.objects.create(
            folder_id=number,
            team=self.team,
            owner=self.user,
            name="My private notes",
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
        )
        out = self._card(
            {"action": "delete_folder", "milestone_folder_id": number},
            "manage_milestone",
            self.user,
        )
        self.assertEqual(out["milestone_folder_id"], number)

    def test_label_access_answers_by_the_membership_rule(self):
        access = LabelAccess(self._ctx(self.user))
        self.assertEqual(access.milestone_folder_name(self.folder.folder_id), "Launch")
        self.assertIsNone(access.milestone_folder_name(self.secret_folder.folder_id))
        self.assertIsNone(access.milestone_folder_name("abc"))
        self.assertIsNone(access.milestone_folder_name(None))
        self.assertEqual(access.sprint_name(self.sprint.sprint_id), "Sprint 1")
        self.assertIsNone(access.sprint_name(self.secret_sprint.sprint_id))
        self.assertIsNone(access.sprint_name("abc"))
        # a caller with no user labels nothing
        blank = LabelAccess(controller.ToolContext(team_id="", user_id=""))
        self.assertIsNone(blank.milestone_folder_name(self.folder.folder_id))
        self.assertIsNone(blank.sprint_name(self.sprint.sprint_id))


class AssigneeLabelTests(MilestoneCardBase):
    def test_assignees_are_named(self):
        out = self._card(
            {
                "action": "create",
                "project_id": self.project.project_id,
                "title": "t",
                "assignee_ids": [str(self.user.id), str(self.user2.id)],
            },
            "manage_milestone",
            self.user,
        )
        self.assertEqual(out["assignee_ids"], ["testuser", "otheruser"])

    def test_a_person_the_caller_cannot_see_stays_an_id(self):
        out = self._card(
            {
                "action": "create",
                "project_id": self.project.project_id,
                "title": "t",
                "assignee_ids": [str(self.stranger.id)],
            },
            "manage_milestone",
            self.user,
        )
        self.assertEqual(out["assignee_ids"], [str(self.stranger.id)])

    def test_clearing_the_assignees_stays_empty(self):
        out = self._card(
            {"action": "update", "milestone_id": self.milestone.milestone_id, "assignee_ids": []},
            "manage_milestone",
            self.user,
        )
        self.assertEqual(out["assignee_ids"], [])


class CurrentSnapshotTests(MilestoneCardBase):
    def setUp(self):
        super().setUp()
        m = self.milestone
        m.priority, m.status, m.title = "Low", "WIP", "Q4 launch"
        m.folder, m.sprint = self.folder, self.sprint
        m.tags = [{"tagName": "Feature", "tagColor": "#0f0", "tagTextColor": "#000"}]
        m.save()
        MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user2)

    def test_an_update_shows_the_present_value_of_each_field_it_changes(self):
        out = self._card(
            {
                "action": "update",
                "milestone_id": self.milestone.milestone_id,
                "priority": "High",
                "status": "Closed",
                "title": "Renamed",
                "milestone_folder_id": "",
                "sprint_id": "",
                "tags": [],
                "assignee_ids": [str(self.user.id)],
                "due_date": "2026-12-01",
            },
            "manage_milestone",
            self.user,
        )
        self.assertEqual(
            out["current"],
            {
                "priority": "Low",
                "status": "WIP",
                "title": "Q4 launch",
                "milestone_folder_id": "Launch",
                "sprint_id": "Sprint 1",
                "tags": ["Feature"],
                "assignee_ids": ["otheruser"],
                "due_date": None,
            },
        )
        self.assertEqual(out["assignee_ids"], ["testuser"])

    def test_only_the_fields_being_changed_appear(self):
        out = self._card(
            {"action": "update", "milestone_id": self.milestone.milestone_id, "priority": "High"},
            "manage_milestone",
            self.user,
        )
        self.assertEqual(out["current"], {"priority": "Low"})

    def test_a_milestone_the_caller_cannot_read_gets_no_snapshot(self):
        hidden = self._milestone(self.secret, "Reorg plan", self.user2)
        out = self._card(
            {"action": "update", "milestone_id": hidden.milestone_id, "priority": "High"},
            "manage_milestone",
            self.user,
        )
        self.assertNotIn("current", out)
        self.assertEqual(out["milestone_id"], hidden.milestone_id)

    def test_a_deleted_milestone_gets_no_snapshot(self):
        self.milestone.is_deleted = True
        self.milestone.save()
        out = self._card(
            {"action": "update", "milestone_id": self.milestone.milestone_id, "priority": "High"},
            "manage_milestone",
            self.user,
        )
        self.assertNotIn("current", out)

    def test_a_create_has_no_snapshot(self):
        out = self._card(
            {"action": "create", "project_id": self.project.project_id, "title": "New"},
            "manage_milestone",
            self.user,
        )
        self.assertNotIn("current", out)
        self.assertEqual(out["project_id"], "Website Redesign")

    def test_a_folder_rename_shows_the_present_name(self):
        out = self._card(
            {
                "action": "rename_folder",
                "milestone_folder_id": self.folder.folder_id,
                "folder_name": "Q4",
            },
            "manage_milestone",
            self.user,
        )
        self.assertEqual(out["current"], {"folder_name": "Launch"})

    def test_a_folder_delete_says_how_many_milestones_it_unfiles(self):
        out = self._card(
            {"action": "delete_folder", "milestone_folder_id": self.folder.folder_id},
            "manage_milestone",
            self.user,
        )
        self.assertEqual(out["current"], {"folder_name": "Launch", "milestones": 1})

    def test_a_folder_the_caller_cannot_read_gets_no_snapshot(self):
        out = self._card(
            {"action": "delete_folder", "milestone_folder_id": self.secret_folder.folder_id},
            "manage_milestone",
            self.user,
        )
        self.assertNotIn("current", out)


class DeleteCardTests(MilestoneCardBase):
    def test_the_card_names_the_milestone_and_the_tasks_it_will_detach(self):
        self._task(self.project, "one", self.user, milestone=self.milestone)
        self._task(self.project, "two", self.user, milestone=self.milestone)
        out = self._card(
            {"milestone_id": self.milestone.milestone_id}, "delete_milestone", self.user
        )
        self.assertEqual(out["milestone_id"], "Q4 launch")
        self.assertEqual(out["current"], {"title": "Q4 launch", "tasks": 2})

    def test_a_milestone_the_caller_cannot_read_stays_a_bare_id(self):
        hidden = self._milestone(self.secret, "Reorg plan", self.user2)
        out = self._card({"milestone_id": hidden.milestone_id}, "delete_milestone", self.user)
        self.assertEqual(out["milestone_id"], hidden.milestone_id)
        self.assertNotIn("current", out)


class ChipTests(MilestoneCardBase):
    def _sources(self, name, result):
        return controller._ui_sources_from_tool_result(name, result)

    def test_a_milestone_action_chips_the_milestone_as_operated(self):
        sources = self._sources(
            "manage_milestone",
            {
                "action": "update",
                "milestone_id": self.milestone.milestone_id,
                "task_id": self.milestone.task_id,
                "project_id": self.project.project_id,
                "title": "Q4 launch",
            },
        )
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["entity_type"], "milestone")
        self.assertEqual(sources[0]["entity_id"], f"milestone:{self.milestone.milestone_id}")
        self.assertEqual(sources[0]["task_id"], str(self.milestone.task_id))
        self.assertTrue(sources[0]["operated"])

    def test_a_folder_action_chips_the_project(self):
        sources = self._sources(
            "manage_milestone",
            {
                "action": "create_folder",
                "folder_id": 5,
                "project_id": self.project.project_id,
                "project_name": "Website Redesign",
            },
        )
        self.assertEqual([s["entity_type"] for s in sources], ["project"])
        self.assertTrue(sources[0]["operated"])

    def test_a_delete_chips_the_project_never_the_milestone(self):
        sources = self._sources(
            "delete_milestone",
            {
                "milestone_id": self.milestone.milestone_id,
                "project_id": self.project.project_id,
                "project_name": "Website Redesign",
                "deleted": True,
            },
        )
        self.assertEqual([s["entity_type"] for s in sources], ["project"])

    def test_a_result_with_nothing_to_point_at_chips_nothing(self):
        self.assertEqual(self._sources("manage_milestone", {"action": "update"}), [])
        self.assertEqual(self._sources("delete_milestone", {}), [])
