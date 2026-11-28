"""Automation rules on milestones (MIP-2521): the sub-tree move end to end, each guard, the size
cap and the rate limit, what fires and what does not, and the API.

Driven through the app's milestone PATCH, so capture from `MilestoneMaster` and the PATCH's own
flush are exercised.
"""

import json
from unittest import mock

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.common.user_models import GithubWebhookRegistration
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.automation_models import AutomationRule, AutomationRun
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.services import automation_engine, automation_milestones, automation_rules
from origin.services.milestone_service import create_milestone
from origin.services.project_statuses import create_status
from origin.services.task_milestone_conversion import convert_task_to_milestone
from origin.tests.test_automation_rules import APPLIED, REFUSED, AutomationTestBase
from origin.tests.test_github_webhook import WEBHOOK_SECRET, _pr_payload, _sign


class MilestoneRuleBase(AutomationTestBase):
    def milestone(self, project=None, *, tasks=0, **extra):
        project = project or self.a
        extra.setdefault("status", "Open")
        row = create_milestone(project, reporter_id=self.user.id, title="M", **extra)
        self.members = [
            self.task(project, title=f"t{i}", milestone=row, parent_task_id=row.task_id)
            for i in range(tasks)
        ]
        automation_engine._queued.set(None)
        AutomationRun.objects.all().delete()
        return row

    def mrule(self, project=None, **extra):
        extra.setdefault("applies_to", "milestone")
        return self.rule(project, **extra)

    def patch(self, milestone, **body):
        response = self.client.patch(
            reverse("milestone_detail", args=[milestone.milestone_id]), body, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def begin(self, milestone):
        return self.patch(milestone, status="WIP")

    def backing(self, milestone):
        return TaskMaster.objects.get(task_id=milestone.task_id)

    def assert_refused(self, milestone, reason):
        self.assertEqual(self.fresh(milestone).project_id, self.a.project_id)
        backing = self.backing(milestone)
        self.assertEqual(backing.project_id, self.a.project_id)
        rows = self.history(backing, REFUSED)
        self.assertEqual(
            [r.metadata["reason"] for r in rows], [automation_rules.public_reason(reason)]
        )
        self.assertEqual([r.metadata["milestoneId"] for r in rows], [milestone.milestone_id])
        self.assertEqual(AutomationRun.objects.get(task=backing).reason, reason)


class MilestoneRuleMoveTests(MilestoneRuleBase):
    def test_a_started_milestone_moves_with_its_whole_sub_tree(self):
        self.tag(self.a, "only-here")
        self.tag(self.x, "added")
        accepted = create_status(self.x, name="Accepted", category="Open", actor=self.user)
        rule = self.mrule(set_custom_status=accepted, add_tag="added")
        milestone = self.milestone(tags=self.tags("tag-a", "only-here"), tasks=2)
        first, second = self.members
        child = self.task(title="child", parent_task_id=first.task_id, milestone=milestone)
        grandchild = self.task(title="grand", parent_task_id=child.task_id)
        TaskMaster.objects.filter(pk=first.pk).update(tags=self.tags("tag-a", "only-here"))
        TaskDependency.objects.create(team=self.team, blocker_task=first, blocked_task=second)
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.a, task=child, title="n", body=[]
        )
        old_id = self.fresh(first).display_id
        automation_engine._queued.set(None)
        AutomationRun.objects.all().delete()

        response = self.begin(milestone)

        self.assertEqual(response.data["automation"], {"ruleId": rule.id, "outcome": "applied"})
        self.assertEqual(response.data["movedFromProjectId"], self.a.project_id)
        self.assertEqual(response.data["milestone"]["projectId"], self.x.project_id)
        milestone = self.fresh(milestone)
        self.assertEqual(
            (milestone.project_id, milestone.status, milestone.custom_status_id),
            (self.x.project_id, "Open", accepted.status_id),
        )
        self.assertEqual([t["tagName"] for t in milestone.tags], ["tag-a", "added"])
        backing = self.backing(milestone)
        self.assertEqual((backing.project_id, backing.status), (self.x.project_id, "Open"))
        self.assertEqual([t["tagName"] for t in backing.tags], ["tag-a", "added"])
        for row in (first, second, child, grandchild):
            self.assertEqual(self.fresh(row).project_id, self.x.project_id, row.title)
        self.assertEqual([t["tagName"] for t in self.fresh(first).tags], ["tag-a"])
        self.assertEqual(self.fresh(child).parent_task_id, first.task_id)
        self.assertEqual(self.fresh(first).milestone_id, milestone.milestone_id)
        self.assertEqual(self.fresh(note).project_id, self.x.project_id)
        self.assertEqual(TaskDependency.objects.count(), 1)
        self.assertEqual(self.fresh(first).number_aliases.get().number, int(old_id.split("-")[1]))
        (applied,) = self.history(backing, APPLIED)
        self.assertEqual(applied.actor_id, self.user.id)
        self.assertEqual(
            (applied.metadata["milestoneId"], applied.metadata["toProjectId"]),
            (milestone.milestone_id, self.x.project_id),
        )
        moved = self.history(backing, TaskActivityActionType.PROJECT)
        run = AutomationRun.objects.get()
        self.assertEqual(
            (run.state, run.destination_project_id, run.task_id),
            ("applied", self.x.project_id, milestone.task_id),
        )
        self.assertTrue(all(r.metadata["via"]["kind"] == "rule" for r in moved))

    def test_the_manual_move_and_the_rule_move_leave_the_same_rows(self):
        by_hand = self.milestone(tasks=2)
        hand_tasks = self.members
        self.patch(by_hand, project_id=self.x.project_id)
        self.mrule(set_status="")
        by_rule = self.milestone(tasks=2)
        rule_tasks = self.members
        self.begin(by_rule)

        def shape(milestone, tasks):
            m = self.fresh(milestone)
            rows = [self.fresh(t) for t in tasks]
            return (
                m.project_id,
                m.sprint_id,
                m.folder_id,
                self.backing(m).project_id,
                [(r.project_id, r.sprint_id, r.milestone_id == m.milestone_id) for r in rows],
            )

        self.assertEqual(shape(by_hand, hand_tasks), shape(by_rule, rule_tasks))

    def test_runs_as_the_owner(self):
        self.mrule()
        milestone = self.milestone()
        self.authenticate(self.user2)
        self.begin(milestone)
        (applied,) = self.history(self.backing(milestone), APPLIED)
        self.assertEqual(applied.actor_id, self.user.id)

    def test_a_rule_without_a_destination_changes_it_in_place(self):
        self.mrule(destination_project=None, set_status="Pending", set_assignee=self.user2)
        milestone = self.milestone()
        response = self.begin(milestone)
        self.assertNotIn("movedFromProjectId", response.data)
        milestone = self.fresh(milestone)
        self.assertEqual((milestone.project_id, milestone.status), (self.a.project_id, "Pending"))
        self.assertEqual(automation_milestones.assignee_ids_of(milestone), [str(self.user2.id)])
        self.assertEqual(self.backing(milestone).assignee_id, self.user2.id)

    def test_a_rule_that_would_change_nothing_is_skipped_quietly(self):
        self.mrule(destination_project=None, set_status="WIP")
        milestone = self.milestone()
        self.begin(milestone)
        self.assertEqual(AutomationRun.objects.get().reason, "no_change")
        backing = self.backing(milestone)
        self.assertFalse(self.history(backing, APPLIED) + self.history(backing, REFUSED))

    def test_a_saver_who_cannot_read_the_destination_gets_the_milestone_as_it_was(self):
        hidden = self.project("Hidden", "HH", is_private=True)
        self.a.is_private = True
        self.a.save(update_fields=["is_private"])
        self.mrule(destination_project=hidden)
        ProjectMembers.objects.filter(project=hidden).exclude(attendee=self.user).delete()
        milestone = self.milestone()
        self.authenticate(self.user2)
        response = self.begin(milestone)
        self.assertEqual(self.fresh(milestone).project_id, hidden.project_id)
        self.assertEqual(response.data["movedFromProjectId"], self.a.project_id)
        self.assertEqual(response.data["milestone"]["projectId"], self.a.project_id)

    def test_the_old_chat_is_told_that_a_milestone_left(self):
        old, _new = [
            Channel.objects.get_or_create(
                kind=ChannelKind.PM,
                legacy_chat_id=project.project_id,
                defaults={"team": self.team, "project": project},
            )[0]
            for project in (self.a, self.x)
        ]
        self.mrule(name="Handover")
        milestone = self.milestone()
        self.begin(milestone)
        line = Message.objects.get(channel=old, metadata__systemNotice="task_moved_by_rule")
        self.assertEqual(line.body_text, 'Rule "Handover" moved a milestone to another project.')
        self.assertEqual(
            (line.metadata["movedTaskId"], line.metadata["movedMilestone"]),
            (milestone.task_id, True),
        )

    def test_a_move_drops_the_old_projects_status_label(self):
        triage = create_status(self.a, name="Triage", category="WIP", actor=self.user)
        self.mrule(set_status="")
        milestone = self.milestone()
        self.patch(milestone, custom_status=triage.status_id)
        milestone = self.fresh(milestone)
        self.assertEqual(milestone.project_id, self.x.project_id)
        self.assertIsNone(milestone.custom_status_id)
        self.assertIsNone(self.backing(milestone).custom_status_id)

    def test_a_new_assignee_is_told_after_the_commit_only(self):
        self.mrule(destination_project=None, set_status="", set_assignee=self.user2)
        milestone = self.milestone()
        with mock.patch.object(automation_milestones, "notify_milestone_assigned") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self.begin(milestone)
                notify.assert_not_called()
        self.assertEqual(notify.call_args.kwargs["added_user_ids"], [str(self.user2.id)])

    def test_one_request_acts_on_few_milestones_and_leaves_the_rest_to_the_tick(self):
        self.mrule(set_status="")
        rows = [self.milestone() for _ in range(automation_milestones.INLINE_LIMIT + 1)]
        AutomationRun.objects.all().delete()
        for row in rows:
            row.status = "WIP"
            row.save(update_fields=["status", "ts_updated_at"])
        done = automation_engine.flush()
        self.assertEqual(len(done), automation_milestones.INLINE_LIMIT)
        (left,) = AutomationRun.objects.filter(state="pending")
        self.assertEqual(left.task_id, rows[-1].task_id)
        automation_engine.process_task(left.task_id)
        self.assertEqual(self.fresh(rows[-1]).project_id, self.x.project_id)

    def test_a_failing_capture_does_not_fail_the_milestone_save(self):
        milestone = self.milestone()
        with mock.patch.object(
            automation_engine, "has_milestone_rules", side_effect=RuntimeError("boom")
        ):
            self.begin(milestone)
        self.assertEqual(self.fresh(milestone).status, "WIP")

    def test_the_dry_run_cost_does_not_grow_with_the_tasks_under_a_milestone(self):
        def queries():
            with CaptureQueriesContext(connection) as captured:
                automation_engine.dry_run(rule, self.a)
            return len(captured.captured_queries)

        rule = self.mrule()
        milestone = self.milestone(tasks=1, status="WIP")
        one = queries()
        for i in range(10):
            self.task(title=f"more{i}", milestone=milestone, parent_task_id=milestone.task_id)
        self.assertEqual(queries(), one)


class MilestoneRuleTriggerTests(MilestoneRuleBase):
    @override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_a_merged_pr_close_sets_off_a_milestone_rule(self):
        self.user.auto_close_on_pr_merge = True
        self.user.is_email_verified = True
        self.user.save()
        GithubWebhookRegistration.objects.create(
            owner="owner", repo="repo", hook_id=1, registered_by=self.user
        )
        self.mrule(trigger_value={"status": "Closed"}, set_status="")
        row = self.milestone()
        branch = f"x/{self.backing(row).display_id}-y"
        body = json.dumps(_pr_payload(action="closed", merged=True, head_ref=branch)).encode()
        self.unauthenticate()
        response = self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body),
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(response.status_code, 200, response.data)
        row = self.fresh(row)
        self.assertEqual((row.project_id, row.status), (self.x.project_id, "Closed"))

    def in_place(self, trigger_type, trigger_value=None, **extra):
        return self.mrule(
            trigger_type=trigger_type,
            trigger_value=trigger_value or {},
            destination_project=None,
            set_status="Pending",
            **extra,
        )

    def test_created(self):
        self.mrule(trigger_type="created", trigger_value={}, set_status="")
        response = self.client.post(
            reverse("milestone_create"),
            {"project_id": self.a.project_id, "title": "New", "assignee_ids": []},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        milestone = MilestoneMaster.objects.get(
            milestone_id=response.data["milestone"]["milestoneId"]
        )
        self.assertEqual(milestone.project_id, self.x.project_id)

    def test_creating_with_assignees_is_not_an_assignee_change(self):
        self.in_place("assignee_changed")
        create_milestone(
            self.a, reporter_id=self.user.id, title="M", assignee_ids=[str(self.user2.id)]
        )
        self.assertFalse(AutomationRun.objects.exists())

    def test_tag_added(self):
        self.in_place("tag_added", {"tag": "tag-a"})
        milestone = self.milestone()
        self.patch(milestone, tags=self.tags("tag-a"))
        self.assertEqual(self.fresh(milestone).status, "Pending")

    def test_assignee_changed_through_the_patch_and_the_assignee_endpoint(self):
        self.in_place("assignee_changed", {"assignee_id": str(self.user2.id)})
        milestone = self.milestone()
        self.patch(milestone, assignee_ids=[str(self.user2.id)])
        self.assertEqual(self.fresh(milestone).status, "Pending")

        other = self.milestone()
        response = self.client.post(
            reverse("milestone_assignees_add", args=[other.milestone_id]),
            {"user_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.fresh(other).status, "Pending")

    def test_removing_an_assignee_is_a_change_without_a_named_person(self):
        self.in_place("assignee_changed")
        milestone = self.milestone(assignee_ids=[str(self.user2.id)])
        response = self.client.delete(
            reverse("milestone_assignees_remove", args=[milestone.milestone_id, self.user2.id])
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.fresh(milestone).status, "Pending")

    def test_a_custom_status_trigger(self):
        triage = create_status(self.a, name="Triage", category="WIP", actor=self.user)
        self.in_place("status_changed", {"custom_status_id": triage.status_id})
        milestone = self.milestone()
        self.patch(milestone, custom_status=triage.status_id)
        self.assertEqual(self.fresh(milestone).status, "Pending")

    def test_conditions_read_the_milestone(self):
        refs = automation_rules.ProjectRefs(self.a.project_id)
        milestone = self.milestone(
            status="WIP",
            priority="High",
            tags=self.tags("tag-a"),
            assignee_ids=[str(self.user.id), str(self.user2.id)],
        )
        # The backing row's mirror says otherwise; the milestone is what counts.
        TaskMaster.objects.filter(task_id=milestone.task_id).update(priority="Low", tags=None)
        subject = automation_milestones.subject_of(automation_milestones.lock(milestone.task_id))
        holds = lambda **c: automation_rules.condition_holds(c, subject, refs)  # noqa: E731
        self.assertTrue(holds(field="assignee", op="is", value=str(self.user2.id)))
        self.assertFalse(holds(field="assignee", op="is_not", value=str(self.user2.id)))
        self.assertTrue(holds(field="assignee", op="is_set"))
        self.assertTrue(holds(field="priority", op="is", value="High"))
        self.assertTrue(holds(field="tag", op="is", value="tag-a"))
        self.assertTrue(holds(field="reporter", op="is", value=str(self.user.id)))

    def test_task_and_milestone_rules_do_not_cross(self):
        self.rule()  # a task rule
        milestone = self.milestone()
        self.begin(milestone)
        self.assertFalse(AutomationRun.objects.exists())
        self.assertEqual(self.fresh(milestone).project_id, self.a.project_id)

        AutomationRule.objects.all().delete()
        self.mrule()
        task = self.task()
        self.start(task)
        self.assertFalse(AutomationRun.objects.exists())
        self.assertEqual(self.fresh(task).project_id, self.a.project_id)

    def test_what_fires_no_milestone_rule(self):
        self.in_place("status_changed", {"status": "Blocked"})
        self.mrule(trigger_type="created", trigger_value={})
        with automation_engine.suppress_automation():
            milestone = self.milestone()
            blocker = self.task(title="blocker")
        # The automatic Blocked flip.
        TaskDependency.objects.create(
            team=self.team, blocker_task=blocker, blocked_task=self.backing(milestone)
        )
        self.assertEqual(self.fresh(milestone).status, "Blocked")
        self.assertFalse(AutomationRun.objects.exists())
        # Turning a task into a milestone.
        task = self.task(title="becomes")
        automation_engine._queued.set(None)
        AutomationRun.objects.all().delete()
        convert_task_to_milestone(task.task_id)
        self.assertFalse(AutomationRun.objects.exists())

    def test_a_move_by_hand_is_not_a_status_change_even_when_it_drops_a_label(self):
        triage = create_status(self.a, name="Triage", category="Open", actor=self.user)
        self.mrule(self.x, trigger_value={"status": "Open"}, destination_project=None)
        milestone = self.milestone(custom_status=triage)
        self.patch(milestone, project_id=self.x.project_id)
        self.assertFalse(AutomationRun.objects.exists())

    def test_one_hop(self):
        self.mrule()
        self.mrule(self.x, trigger_value={"status": "Open"}, destination_project=self.a)
        milestone = self.milestone(tasks=1)
        self.begin(milestone)
        self.assertEqual(self.fresh(milestone).project_id, self.x.project_id)
        self.assertEqual(AutomationRun.objects.count(), 1)

    def test_the_moved_sub_tree_sets_off_no_task_rule(self):
        self.mrule()
        self.rule(self.x, trigger_type="tag_added", trigger_value={"tag": "tag-a"})
        self.rule(self.x, trigger_value={"status": "Open"}, destination_project=self.a)
        milestone = self.milestone(tasks=1)
        TaskMaster.objects.filter(pk=self.members[0].pk).update(tags=self.tags("tag-a"))
        self.begin(milestone)
        self.assertEqual(self.fresh(self.members[0]).project_id, self.x.project_id)
        self.assertEqual(AutomationRun.objects.count(), 1)


class MilestoneRuleGuardTests(MilestoneRuleBase):
    def run_rule(self, *, tasks=1, **extra):
        self.rule_row = self.mrule(**extra)
        milestone = self.milestone(tasks=tasks)
        self.begin(milestone)
        return milestone

    def private_source(self):
        self.a.is_private = True
        self.a.save(update_fields=["is_private"])

    def test_plan(self):
        self.plan.return_value = False
        self.assert_refused(self.run_rule(), "plan")

    def test_owner_not_on_destination_turns_the_rule_off(self):
        milestone = self.run_rule(owner=self.user2)
        ProjectMembers.objects.filter(project=self.a, attendee=self.user2).update(
            member_role="editor"
        )
        AutomationRun.objects.all().delete()
        TaskActivity.objects.filter(action_type=REFUSED).delete()
        MilestoneMaster.objects.filter(pk=milestone.pk).update(status="Open")
        AutomationRule.objects.update(enabled=True, disabled_reason="")
        ProjectMembers.objects.filter(project=self.x, attendee=self.user2).delete()
        self.begin(milestone)
        self.assert_refused(milestone, "owner_not_on_destination")
        self.assertFalse(AutomationRule.objects.get().enabled)

    def test_owner_who_is_only_a_viewer_of_the_source(self):
        milestone = self.run_rule(owner=self.user2)
        self.assert_refused(milestone, "owner_not_manager")

    def test_other_team(self):
        from origin.models.common.team_models import TeamMaster

        other = TeamMaster.objects.create(team_name="Other", owner=self.user)
        foreign = self.project("Foreign", "FF", team=other)
        self.assert_refused(self.run_rule(destination_project=foreign), "destination_not_found")

    def test_private_to_public(self):
        self.private_source()
        self.assert_refused(self.run_rule(), "private_to_public")

    def test_readers_outside_the_source(self):
        hidden = self.project("Hidden", "HH", is_private=True)
        self.private_source()
        ProjectMembers.objects.create(
            team=self.team, project=hidden, attendee=self.person("outsider")
        )
        self.assert_refused(self.run_rule(destination_project=hidden), "readers_outside_source")

    def test_archived_destination(self):
        self.x.archived_at = self.x.ts_created_at
        self.x.save(update_fields=["archived_at"])
        self.assert_refused(self.run_rule(), "destination_archived")

    def test_nothing_runs_in_an_archived_source(self):
        self.mrule()
        milestone = self.milestone()
        self.a.archived_at = self.a.ts_created_at
        self.a.save(update_fields=["archived_at"])
        milestone.status = "WIP"
        milestone.save(update_fields=["status", "ts_updated_at"])
        automation_engine.flush()
        self.assertEqual(self.fresh(milestone).project_id, self.a.project_id)
        self.assertFalse(AutomationRun.objects.exists())

    def test_an_assignee_in_the_sub_tree_who_is_not_on_the_destination(self):
        outsider = self.person("contractor", team_member=False)
        self.join(self.a, outsider)
        self.private_source()
        hidden = self.project("Hidden", "HH", is_private=True)
        self.join(hidden, self.user2)
        self.mrule(destination_project=hidden)
        milestone = self.milestone(tasks=1)
        TaskMaster.objects.filter(pk=self.members[0].pk).update(assignee=outsider)
        self.begin(milestone)
        self.assert_refused(milestone, "assignee_not_on_destination")
        self.assertEqual(self.fresh(self.members[0]).project_id, self.a.project_id)

    def test_a_task_from_another_project_is_not_carried_along(self):
        other = self.project("Other", "OO")
        self.mrule()
        milestone = self.milestone(tasks=1)
        stray = self.task(other, title="stray")
        TaskMaster.objects.filter(pk=stray.pk).update(parent_task_id=self.members[0].task_id)
        self.begin(milestone)
        self.assert_refused(milestone, "move_not_allowed")
        self.assertEqual(self.fresh(stray).project_id, other.project_id)

    def test_a_rule_assigns_nobody_who_could_not_open_the_project(self):
        self.private_source()
        stranger = self.person("stranger")
        self.assert_refused(
            self.run_rule(destination_project=None, set_assignee=stranger), "assignee_not_allowed"
        )
        self.assertFalse(MilestoneAssignees.objects.filter(user=stranger).exists())

    def test_above_the_cap_it_is_refused_and_recorded_not_an_error(self):
        with mock.patch.object(automation_milestones, "MOVE_TASK_CAP", 3):
            self.mrule()
            milestone = self.milestone(tasks=2)
            self.task(title="c1", parent_task_id=self.members[0].task_id)
            self.task(title="c2", parent_task_id=self.members[1].task_id)
            automation_engine._queued.set(None)
            AutomationRun.objects.all().delete()
            response = self.begin(milestone)
        self.assertEqual(response.data["automation"]["outcome"], "refused")
        self.assert_refused(milestone, "too_many_tasks")
        self.assertEqual(self.fresh(milestone).status, "WIP")

    def test_at_the_cap_it_moves(self):
        with mock.patch.object(automation_milestones, "MOVE_TASK_CAP", 3):
            self.mrule()
            milestone = self.milestone(tasks=2)
            self.task(title="c1", parent_task_id=self.members[0].task_id)
            automation_engine._queued.set(None)
            AutomationRun.objects.all().delete()
            self.begin(milestone)
        self.assertEqual(self.fresh(milestone).project_id, self.x.project_id)

    def test_the_cap_does_not_limit_a_rule_that_does_not_move(self):
        with mock.patch.object(automation_milestones, "MOVE_TASK_CAP", 0):
            milestone = self.run_rule(tasks=2, destination_project=None, set_status="Pending")
        self.assertEqual(self.fresh(milestone).status, "Pending")

    def test_the_count_matches_what_a_move_carries(self):
        from origin.services.task_project_move import milestone_subtree_task_ids

        milestone = self.milestone(tasks=2)
        child = self.task(title="c", parent_task_id=self.members[0].task_id)
        self.task(title="g", parent_task_id=child.task_id)
        loose = self.task(title="by fk only", milestone=milestone)
        self.task(title="deleted", milestone=milestone, status="Deleted")
        self.task(title="under loose", parent_task_id=loose.task_id)
        self.assertEqual(
            automation_milestones.subtree_ids(milestone),
            milestone_subtree_task_ids(milestone) - {milestone.task_id},
        )

    def test_a_per_milestone_rate_limit_said_once(self):
        self.mrule(set_status="Open")
        self.mrule(self.x, destination_project=self.a, set_status="Open")
        milestone = self.milestone(tasks=1)
        for _ in range(automation_milestones.RATE_LIMIT):
            self.begin(self.fresh(milestone))
        where = self.fresh(milestone).project_id
        backing = self.backing(milestone)
        self.assertEqual(len(self.history(backing, APPLIED)), automation_milestones.RATE_LIMIT)
        self.begin(milestone)
        self.assertEqual(self.fresh(milestone).project_id, where)
        self.assertEqual(self.history(backing, REFUSED)[-1].metadata["reason"], "rate_limited")
        self.patch(milestone, status="Open")
        self.begin(milestone)
        self.assertEqual(len(self.history(backing, REFUSED)), 1)

    def test_a_failing_action_is_refused_and_leaves_nothing_behind(self):
        self.mrule()
        milestone = self.milestone(tasks=1)
        with mock.patch.object(
            automation_milestones, "_invalidate", side_effect=RuntimeError("boom")
        ):
            self.begin(milestone)
        self.assert_refused(milestone, "error")
        self.assertEqual(self.fresh(self.members[0]).project_id, self.a.project_id)
        self.assertEqual(self.fresh(milestone).status, "WIP")


class MilestoneRuleApiTests(MilestoneRuleBase):
    def url(self, name, *args):
        return reverse(name, args=[self.a.project_id, *args])

    def body(self, **extra):
        return {
            "name": "Hand over",
            "applies_to": "milestone",
            "trigger_type": "status_changed",
            "trigger_value": {"status": "WIP"},
            "destination_project_id": self.x.project_id,
            **extra,
        }

    def test_create_edit_and_validation(self):
        response = self.client.post(self.url("automation_rules"), self.body(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["appliesTo"], "milestone")
        rule_id = response.data["ruleId"]
        response = self.client.patch(
            self.url("automation_rule_detail", rule_id), {"name": "Renamed"}, format="json"
        )
        self.assertEqual(response.data["appliesTo"], "milestone")
        bad = self.client.post(
            self.url("automation_rules"), self.body(applies_to="sprint"), format="json"
        )
        self.assertEqual((bad.status_code, bad.data["code"]), (400, "bad_applies_to"))
        bad = self.client.post(
            self.url("automation_rules"),
            self.body(conditions=[{"field": "milestone", "op": "is_set"}]),
            format="json",
        )
        self.assertEqual((bad.status_code, bad.data["code"]), (400, "invalid_condition"))
        default = self.body()
        del default["applies_to"]
        response = self.client.post(self.url("automation_rules"), default, format="json")
        self.assertEqual(response.data["appliesTo"], "task")

    def test_dry_run_lists_milestones_with_their_outcome_and_writes_nothing(self):
        big = self.milestone(tasks=3, status="WIP")
        small = self.milestone(status="WIP")
        self.milestone(status="Open")
        self.task(status="WIP")
        automation_engine._queued.set(None)
        with mock.patch.object(automation_milestones, "MOVE_TASK_CAP", 2):
            response = self.client.post(
                self.url("automation_rule_dry_run"), self.body(), format="json"
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["matched"], 2)
        got = {i["milestoneId"]: (i["outcome"], i["reason"]) for i in response.data["items"]}
        self.assertEqual(
            got,
            {
                big.milestone_id: ("refused", "too_many_tasks"),
                small.milestone_id: ("would_apply", ""),
            },
        )
        self.assertEqual(self.fresh(small).project_id, self.a.project_id)
        self.assertFalse(AutomationRun.objects.exists())


class MilestoneRuleRejectedTests(MilestoneRuleBase):
    def test_rejected_is_a_trigger_and_an_action_for_a_milestone(self):
        self.mrule(
            trigger_value={"status": "Rejected"}, destination_project=None, set_status="Closed"
        )
        rejected = self.milestone()
        self.patch(rejected, status="Rejected")
        self.assertEqual(self.fresh(rejected).status, "Closed")

        AutomationRule.objects.all().delete()
        self.mrule(destination_project=None, set_status="Rejected")
        started = self.milestone()
        self.begin(started)
        self.assertEqual(self.fresh(started).status, "Rejected")
        self.assertEqual(self.backing(started).status, "Rejected")
