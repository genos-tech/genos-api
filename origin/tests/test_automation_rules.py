"""Project automation rules (MIP-2498): the engine, every guard, and the REST endpoints.

The milestone's own example runs end to end through the app's task save: a task with tag-a in
project A that becomes WIP moves to project X as Open.

`Rules...` tests drive the engine through real requests, so the capture, the end-of-request flush
and the PUT's own flush are all exercised.
"""

import json
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import GithubWebhookRegistration
from origin.models.project.prj_models import (
    ProjectCustomField,
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
)
from origin.models.task.automation_models import AutomationRule, AutomationRun
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.services import automation_engine, automation_rules, housekeeping_prune
from origin.services.milestone_service import create_milestone
from origin.services.project_statuses import create_status
from origin.services.task_card_post import post_task_created_card
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_github_webhook import WEBHOOK_SECRET, _pr_payload, _sign

User = get_user_model()

APPLIED = TaskActivityActionType.AUTOMATION_APPLIED
REFUSED = TaskActivityActionType.AUTOMATION_REFUSED


class AutomationTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        plan = mock.patch.object(automation_rules, "plan_allows", return_value=True)
        self.plan = plan.start()
        self.addCleanup(plan.stop)
        automation_engine._queued.set(None)
        # self.user owns both; self.user2 is a plain member of both.
        self.a = self.project("Alpha", "AA")
        self.x = self.project("Xray", "XX")
        for project in (self.a, self.x):
            self.join(project, self.user2)
        self.tag(self.a, "tag-a")
        self.tag(self.x, "tag-a")
        self.authenticate()

    # -- fixtures ----------------------------------------------------------

    def project(self, name, code, **extra):
        project = ProjectMaster.objects.create(
            team=extra.pop("team", self.team),
            project_name=name,
            code=code,
            owner=extra.pop("owner", self.user),
            project_system_user=self.user,
            **extra,
        )
        ProjectMembers.objects.create(team=project.team, project=project, attendee=project.owner)
        return project

    def join(self, project, user, role="viewer"):
        ProjectMembers.objects.create(
            team=project.team, project=project, attendee=user, member_role=role
        )

    def person(self, name, *, team_member=True):
        user = User.objects.create_user(
            username=name, email=f"{name}@example.com", password="x-pass-123456"
        )
        if team_member:
            TeamMembers.objects.create(team=self.team, attendee=user)
        return user

    def tag(self, project, name):
        count = ProjectTags.objects.filter(project=project).count()
        return ProjectTags.objects.create(
            team=project.team,
            project=project,
            tag_id=count + 1,
            tag_name=name,
            tag_color="#111111",
            tag_text_color="#ffffff",
        )

    def tags(self, *names):
        return [{"tagName": n, "tagColor": "#111111", "tagTextColor": "#fff"} for n in names]

    def task(self, project=None, **extra):
        project = project or self.a
        return TaskMaster.objects.create(
            team=project.team,
            project=project,
            title=extra.pop("title", "T"),
            status=extra.pop("status", "Open"),
            assignee=extra.pop("assignee", self.user),
            reporter=self.user,
            **extra,
        )

    def rule(self, project=None, *, owner=None, position=0, **extra):
        """A rule row written straight to the table, for engine tests."""
        fields = {
            "name": "R",
            "trigger_type": "status_changed",
            "trigger_value": {"status": "WIP"},
            "destination_project": self.x,
            "set_status": "Open",
            **extra,
        }
        fields.setdefault("moves_task", fields["destination_project"] is not None)
        project = project or self.a
        return AutomationRule.objects.create(
            team=project.team,
            project=project,
            owner=owner or self.user,
            position=position,
            **fields,
        )

    # -- actions -----------------------------------------------------------

    def put(self, task, **body):
        return self.client.put(
            reverse("task_create"), {"task_id": task.task_id, **body}, format="json"
        )

    def start(self, task):
        """The change that sets off the default rule, through the app."""
        response = self.put(task, status="WIP")
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def fresh(self, row):
        row.refresh_from_db()
        return row

    def history(self, task, action):
        return list(TaskActivity.objects.filter(task=task, action_type=action).order_by("pk"))

    def refused_with(self, task, reason):
        self.assertEqual(self.fresh(task).project_id, self.a.project_id)
        rows = self.history(task, REFUSED)
        self.assertEqual(
            [r.metadata["reason"] for r in rows], [automation_rules.public_reason(reason)]
        )
        self.assertEqual(AutomationRun.objects.get(task=task).reason, reason)


class RulesEngineTests(AutomationTestBase):
    def test_the_milestone_example_end_to_end(self):
        rule = self.rule(conditions=[{"field": "tag", "op": "is", "value": "tag-a"}])
        task = self.task(tags=self.tags("tag-a"))
        old_id = task.display_id

        response = self.start(task)

        self.assertEqual(response.data["task"]["project"], self.x.project_id)
        self.assertEqual(response.data["task"]["status"], "Open")
        self.assertTrue(response.data["task"]["displayId"].startswith("XX-"))
        self.assertEqual(response.data["movedFromProjectId"], self.a.project_id)
        self.assertEqual(response.data["automation"], {"ruleId": rule.id, "outcome": "applied"})
        task = self.fresh(task)
        self.assertEqual((task.project_id, task.status), (self.x.project_id, "Open"))
        self.assertEqual([t["tagName"] for t in task.tags], ["tag-a"])
        self.assertEqual(task.number_aliases.get().number, int(old_id.split("-")[1]))
        (applied,) = self.history(task, APPLIED)
        self.assertEqual(applied.actor_id, self.user.id)
        self.assertEqual((applied.metadata["ruleId"], applied.metadata["ruleName"]), (rule.id, "R"))
        self.assertEqual(applied.metadata["toProjectId"], self.x.project_id)
        moved = self.history(task, TaskActivityActionType.PROJECT)[-1]
        self.assertEqual(moved.metadata["via"], {"kind": "rule", "rule_id": rule.id})
        run = AutomationRun.objects.get(task=task)
        self.assertEqual((run.state, run.destination_project_id), ("applied", self.x.project_id))

    def test_a_task_that_misses_the_condition_stays_and_leaves_no_trace(self):
        self.rule(conditions=[{"field": "tag", "op": "is", "value": "tag-a"}])
        task = self.task()
        response = self.start(task)
        self.assertNotIn("automation", response.data)
        self.assertEqual(self.fresh(task).project_id, self.a.project_id)
        self.assertFalse(AutomationRun.objects.exists())
        self.assertFalse(self.history(task, APPLIED) + self.history(task, REFUSED))

    def test_runs_as_the_owner_not_as_the_person_who_made_the_change(self):
        self.rule()
        task = self.task()
        self.authenticate(self.user2)
        self.start(task)
        (applied,) = self.history(task, APPLIED)
        self.assertEqual(applied.actor_id, self.user.id)
        # The person's own change stays theirs.
        status_rows = self.history(task, TaskActivityActionType.STATUS)
        self.assertEqual(status_rows[0].actor_id, self.user2.id)

    def test_the_first_rule_in_order_wins_and_a_disabled_one_is_ignored(self):
        y = self.project("Yankee", "YY")
        self.join(y, self.user2)
        self.rule(position=0, enabled=False, destination_project=y)
        first = self.rule(position=2, destination_project=y)
        self.rule(position=1)
        task = self.task()
        self.start(task)
        self.assertEqual(self.fresh(task).project_id, self.x.project_id)
        self.assertNotEqual(AutomationRun.objects.get().rule_id, first.id)

    def test_a_refused_first_match_does_not_fall_through(self):
        hidden = self.project("Hidden", "HH", is_private=True)
        self.rule(position=0, destination_project=hidden)
        ProjectMembers.objects.create(
            team=self.team, project=hidden, attendee=self.person("outsider")
        )
        self.rule(position=1)
        private_a = self.fresh(self.a)
        private_a.is_private = True
        private_a.save(update_fields=["is_private"])
        task = self.task()
        self.start(task)
        self.refused_with(task, "readers_outside_source")

    def test_created_runs_once_for_a_quick_create(self):
        self.rule(trigger_type="created", trigger_value={}, set_status="")
        response = self.client.post(
            reverse("task_create"),
            {
                "team": str(self.team.team_id),
                "project": self.a.project_id,
                "assignee": str(self.user.id),
                "reporter": str(self.user.id),
                "title": "New",
                "priority": "Low",
                "effort_level": "Low",
                "status": "Open",
                "content": [],
                "due_date": None,
                "links": [],
                "tags": [],
                "is_init_task": False,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        task = TaskMaster.objects.get(task_id=response.data["task"]["task_id"])
        self.assertEqual(task.project_id, self.x.project_id)
        self.assertEqual(len(self.history(task, APPLIED)), 1)

    def test_created_waits_for_the_draft_to_be_finalized(self):
        self.rule(trigger_type="created", trigger_value={}, set_status="")
        draft = self.task(is_init_task=True, title="")
        self.assertFalse(AutomationRun.objects.exists())
        response = self.put(draft, title="Real", is_init_task=False, status="Open")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.fresh(draft).project_id, self.x.project_id)
        self.assertEqual(len(self.history(draft, APPLIED)), 1)
        self.assertEqual(response.data["movedFromProjectId"], self.a.project_id)

    def test_a_milestone_filed_in_the_same_save_does_not_follow_the_moved_task(self):
        # Why rules run after the PUT's last step: the milestone bridge would otherwise
        # re-file a moved task under the old project's milestone.
        self.rule()
        milestone = create_milestone(self.a, reporter_id=self.user.id, title="M")
        task = self.task()
        response = self.put(task, status="WIP", milestone=milestone.milestone_id)
        self.assertEqual(response.status_code, 200, response.data)
        task = self.fresh(task)
        self.assertEqual(
            (task.project_id, task.milestone_id, task.parent_task_id),
            (self.x.project_id, None, None),
        )

    def test_tag_added_and_assignee_changed_triggers(self):
        self.rule(
            trigger_type="tag_added",
            trigger_value={"tag": "tag-a"},
            destination_project=None,
            set_status="Pending",
        )
        task = self.task()
        self.put(task, tags=self.tags("tag-a"))
        self.assertEqual(self.fresh(task).status, "Pending")

        AutomationRule.objects.all().delete()
        self.rule(
            trigger_type="assignee_changed",
            trigger_value={"assignee_id": str(self.user2.id)},
            destination_project=None,
            set_status="WIP",
        )
        other = self.task()
        self.put(other, assignee=str(self.user2.id))
        self.assertEqual(self.fresh(other).status, "WIP")
        self.assertEqual(self.fresh(other).project_id, self.a.project_id)

    def test_conditions(self):
        field = ProjectCustomField.objects.create(
            team=self.team,
            project=self.a,
            field_name="Customer",
            field_type="text",
        )
        milestone = create_milestone(self.a, reporter_id=self.user.id, title="M")
        review = create_status(self.a, name="Triage", category="WIP", actor=self.user)
        refs = automation_rules.ProjectRefs(self.a.project_id)
        task = self.task(
            status="WIP",
            custom_status=review,
            priority="High",
            milestone=milestone,
            custom_field_values={str(field.field_id): "acme"},
            tags=self.tags("tag-a"),
        )
        holds = lambda **c: automation_rules.condition_holds(c, task, refs)  # noqa: E731
        self.assertTrue(holds(field="assignee", op="is", value=str(self.user.id)))
        self.assertFalse(holds(field="assignee", op="is_not", value=str(self.user.id)))
        self.assertTrue(holds(field="reporter", op="is_set"))
        self.assertTrue(holds(field="priority", op="is", value="High"))
        self.assertTrue(holds(field="milestone", op="is", value=milestone.milestone_id))
        self.assertTrue(holds(field="custom_status", op="is", value=review.status_id))
        self.assertTrue(holds(field="custom_field", field_id=field.field_id, op="is", value="acme"))
        self.assertTrue(holds(field="tag", op="is", value="TAG-A"))
        # What the project no longer has is false whatever the operator.
        self.assertFalse(holds(field="tag", op="is_not", value="gone"))
        self.assertFalse(holds(field="milestone", op="is_not", value=999999))
        self.assertFalse(holds(field="custom_field", field_id=999999, op="is_not", value="x"))
        self.assertFalse(holds(field="nonsense", op="is_not", value="x"))

    def test_moves_sub_tasks_remaps_tags_by_name_and_adds_one(self):
        self.tag(self.a, "only-here")
        self.tag(self.a, "Mixed")
        self.tag(self.x, "mixed")
        self.tag(self.x, "added")
        self.rule(add_tag="added", set_status="")
        task = self.task(tags=self.tags("tag-a", "only-here", "Mixed"))
        child = self.task(title="child", parent_task_id=task.task_id, tags=self.tags("tag-a"))
        self.start(task)
        task, child = self.fresh(task), self.fresh(child)
        self.assertEqual((task.project_id, child.project_id), (self.x.project_id,) * 2)
        self.assertEqual([t["tagName"] for t in task.tags], ["tag-a", "mixed", "added"])
        self.assertEqual([t["tagName"] for t in child.tags], ["tag-a"])
        self.assertEqual(task.status, "WIP")

    def test_sets_a_custom_status_in_the_destination_and_an_assignee(self):
        accepted = create_status(self.x, name="Accepted", category="Open", actor=self.user)
        self.rule(set_custom_status=accepted, set_assignee=self.user2)
        task = self.task()
        self.start(task)
        task = self.fresh(task)
        self.assertEqual(
            (task.status, task.custom_status_id, task.assignee_id),
            ("Open", accepted.status_id, self.user2.id),
        )

    def test_a_rule_that_would_change_nothing_is_skipped_quietly(self):
        self.rule(destination_project=None, set_status="WIP")
        task = self.task()
        self.start(task)
        self.assertEqual(AutomationRun.objects.get().reason, "no_change")
        self.assertFalse(self.history(task, APPLIED) + self.history(task, REFUSED))

    # -- loops -------------------------------------------------------------

    def test_a_to_b_to_a_cannot_happen(self):
        self.rule()
        self.rule(self.x, destination_project=self.a, set_status="WIP")
        self.rule(self.x, trigger_value={"status": "Open"}, destination_project=self.a)
        task = self.task()
        self.start(task)
        task = self.fresh(task)
        self.assertEqual((task.project_id, task.status), (self.x.project_id, "Open"))
        self.assertEqual(len(self.history(task, APPLIED)), 1)
        self.assertEqual(AutomationRun.objects.count(), 1)

    def test_a_per_task_rate_limit(self):
        self.rule()
        self.rule(self.x, destination_project=self.a)
        task = self.task()
        for _ in range(automation_engine.RATE_LIMIT):
            self.start(task)
            self.fresh(task)
        where = task.project_id
        self.assertEqual(len(self.history(task, APPLIED)), automation_engine.RATE_LIMIT)
        self.start(task)
        self.assertEqual(self.fresh(task).project_id, where)
        self.assertEqual(self.history(task, REFUSED)[-1].metadata["reason"], "rate_limited")
        # Said once, not once per later change.
        self.put(task, status="Open")
        self.start(task)
        self.assertEqual(len(self.history(task, REFUSED)), 1)
        self.assertEqual(AutomationRun.objects.filter(reason="rate_limited").count(), 1)

    def test_runs_are_applied_once(self):
        self.rule()
        task = self.task()
        task.status = "WIP"
        task.save(update_fields=["status"])
        self.assertEqual(automation_engine.flush()[task.task_id].state, "applied")
        self.assertIsNone(automation_engine.process_task(task.task_id))
        self.assertEqual(automation_engine.flush(), {})
        self.assertEqual(len(self.history(task, APPLIED)), 1)

    def test_a_change_made_in_a_project_the_task_has_left_is_dropped(self):
        self.rule()
        y = self.project("Yankee", "YY")
        task = self.task()
        task.status = "WIP"
        task.save(update_fields=["status"])
        TaskMaster.objects.filter(pk=task.pk).update(project=y, project_task_number=1)
        self.assertEqual(automation_engine.flush(), {})
        self.assertEqual(self.fresh(task).project_id, y.project_id)
        self.assertFalse(AutomationRun.objects.exists())

    def test_the_change_must_still_hold(self):
        self.rule()
        task = self.task()
        task.status = "WIP"
        task.save(update_fields=["status"])
        task.status = "Closed"
        task.save(update_fields=["status"])
        automation_engine.flush()
        self.assertEqual(self.fresh(task).project_id, self.a.project_id)

    def test_a_failing_action_is_refused_and_leaves_nothing_behind(self):
        self.rule()
        task = self.task()
        with mock.patch.object(
            automation_engine, "invalidate_for_task", side_effect=RuntimeError("boom")
        ):
            self.start(task)
        self.refused_with(task, "error")
        self.assertEqual(self.fresh(task).status, "WIP")

    # -- writers that fire nothing ----------------------------------------

    def test_what_fires_no_rule(self):
        self.rule(trigger_value={"status": "Blocked"})
        self.rule(trigger_value={"status": "Open"})
        self.rule(trigger_type="created", trigger_value={})
        with automation_engine.suppress_automation():
            task = self.task()
            blocker = self.task(title="blocker")
        # The automatic Blocked flip, and back.
        TaskDependency.objects.create(team=self.team, blocker_task=blocker, blocked_task=task)
        self.assertEqual(self.fresh(task).status, "Blocked")
        self.assertFalse(AutomationRun.objects.filter(task=task).exists())
        # A milestone's backing row.
        milestone = create_milestone(self.a, reporter_id=self.user.id, title="M")
        self.assertFalse(AutomationRun.objects.filter(task_id=milestone.task_id).exists())
        # An undo of an agent's write.
        with mock.patch.object(
            automation_engine, "get_current_source", return_value={"undo_of": {"run_id": "x"}}
        ):
            self.task(title="undone")
        self.assertFalse(AutomationRun.objects.exists())

    def test_the_csv_import_fires_no_rule(self):
        from origin.services import task_import

        self.rule(trigger_type="created", trigger_value={})
        headers, rows = task_import.parse("title\nImported one\n")
        mapping = task_import.propose_mapping(headers, "generic")
        resolved = task_import.resolve_rows(self.a, "generic", rows, mapping)
        task_import.commit(
            project=self.a,
            user=self.user,
            profile="generic",
            filename="a.csv",
            file_sha256="0" * 64,
            mapping=mapping,
            resolved=resolved,
            create_tags=False,
        )
        self.assertTrue(TaskMaster.objects.filter(title="Imported one", project=self.a).exists())
        self.assertFalse(AutomationRun.objects.exists())

    def test_a_save_that_did_not_write_the_column_is_not_a_change(self):
        self.rule()
        task = self.task()
        task.status = "WIP"  # stale on the instance, never written
        task.save(update_fields=["title"])
        self.assertFalse(AutomationRun.objects.exists())

    @override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
    def test_a_merged_pr_close_sets_off_a_rule(self):
        self.user.auto_close_on_pr_merge = True
        self.user.is_email_verified = True
        self.user.save()
        GithubWebhookRegistration.objects.create(
            owner="owner", repo="repo", hook_id=1, registered_by=self.user
        )
        self.rule(trigger_value={"status": "Closed"}, set_status="")
        task = self.task()
        body = json.dumps(
            _pr_payload(action="closed", merged=True, head_ref=f"x/{task.display_id}-y")
        ).encode()
        self.unauthenticate()
        response = self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body),
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(response.status_code, 200, response.data)
        task = self.fresh(task)
        self.assertEqual((task.project_id, task.status), (self.x.project_id, "Closed"))
        self.assertEqual(self.history(task, APPLIED)[0].actor_id, self.user.id)

    # -- sweep and prune ---------------------------------------------------

    def test_the_tick_runs_what_nobody_flushed_and_drops_old_changes(self):
        self.rule()
        task = self.task()
        stale = self.task(title="old")
        for row in (task, stale):
            row.status = "WIP"
            row.save(update_fields=["status"])
        automation_engine._queued.set(None)
        self.assertEqual(automation_engine.sweep(), 0)  # too fresh: its writer may flush it
        AutomationRun.objects.filter(task=task).update(
            ts_created_at=timezone.now() - timedelta(minutes=5)
        )
        AutomationRun.objects.filter(task=stale).update(
            ts_created_at=timezone.now() - timedelta(days=2)
        )
        call_command("schedule_tick", stdout=mock.MagicMock())
        self.assertEqual(self.fresh(task).project_id, self.x.project_id)
        self.assertEqual(self.fresh(stale).project_id, self.a.project_id)
        self.assertFalse(AutomationRun.objects.filter(state="pending").exists())

    def test_finished_runs_are_pruned_after_90_days(self):
        self.rule()
        task = self.task()
        self.start(task)
        self.assertEqual(housekeeping_prune.count("automation_runs"), 0)
        AutomationRun.objects.update(ts_done_at=timezone.now() - timedelta(days=91))
        self.assertEqual(housekeeping_prune.delete_in_batches("automation_runs"), 1)

    def test_a_batch_runs_when_a_worker_block_ends(self):
        self.rule()
        task = self.task()
        with automation_engine.batch():
            task.status = "WIP"
            task.save(update_fields=["status"])
            self.assertEqual(self.fresh(task).project_id, self.a.project_id)
        self.assertEqual(self.fresh(task).project_id, self.x.project_id)

    def test_the_chat_card_follows(self):
        for project in (self.a, self.x):
            if not Channel.objects.filter(
                kind=ChannelKind.PM, legacy_chat_id=project.project_id
            ).exists():
                Channel.objects.create(
                    team=self.team,
                    kind=ChannelKind.PM,
                    legacy_chat_id=project.project_id,
                    project=project,
                    name=project.project_name,
                )
        self.rule()
        task = self.task()
        post_task_created_card(task, actor_id=self.user.id)
        source = Channel.objects.get(kind=ChannelKind.PM, legacy_chat_id=self.a.project_id)
        destination = Channel.objects.get(kind=ChannelKind.PM, legacy_chat_id=self.x.project_id)
        self.assertTrue(Message.objects.filter(channel=source, task_id=task.task_id).exists())
        self.start(task)
        self.assertFalse(Message.objects.filter(channel=source, task_id=task.task_id).exists())
        self.assertTrue(Message.objects.filter(channel=destination, task_id=task.task_id).exists())


class RulesGuardTests(AutomationTestBase):
    """Each guard refuses at run, writes the reason to the task, and moves nothing."""

    def run_rule(self, **rule):
        self.rule_row = self.rule(**rule)
        task = self.task()
        self.start(task)
        return task

    def set(self, row, **values):
        for key, value in values.items():
            setattr(row, key, value)
        row.save(update_fields=list(values))

    def test_plan(self):
        self.plan.return_value = False
        task = self.run_rule()
        self.refused_with(task, "plan")
        self.assertTrue(self.fresh(self.rule_row).enabled)

    def assert_owner_refusal(self, task, reason):
        self.refused_with(task, reason)
        rule = self.fresh(self.rule_row)
        self.assertEqual((rule.enabled, rule.disabled_reason), (False, reason))

    def test_owner_not_on_destination_turns_the_rule_off(self):
        owner = self.person("ed")
        self.join(self.a, owner, "editor")
        task = self.run_rule(owner=owner)
        self.assert_owner_refusal(task, "owner_not_on_destination")

    def test_owner_who_lost_the_editor_role(self):
        owner = self.person("ed")
        self.join(self.a, owner, "viewer")
        self.join(self.x, owner, "editor")
        task = self.run_rule(owner=owner)
        self.assert_owner_refusal(task, "owner_not_manager")

    def test_owner_removed_from_the_team(self):
        owner = self.person("ed", team_member=False)
        self.join(self.a, owner, "editor")
        self.join(self.x, owner, "editor")
        task = self.run_rule(owner=owner)
        self.assert_owner_refusal(task, "owner_not_member")

    def test_owner_gone(self):
        self.rule_row = self.rule()
        AutomationRule.objects.update(owner=None)
        task = self.task()
        self.start(task)
        self.assert_owner_refusal(task, "owner_gone")

    def test_deleted_destination_project_does_not_become_an_in_place_change(self):
        self.rule_row = self.rule()
        AutomationRule.objects.update(destination_project=None)
        task = self.task()
        self.start(task)
        self.refused_with(task, "destination_not_found")
        self.assertEqual(self.fresh(task).status, "WIP")
        self.assertFalse(self.fresh(self.rule_row).enabled)

    def test_soft_deleted_destination(self):
        self.set(self.x, is_deleted=True)
        task = self.run_rule()
        self.assertEqual(self.fresh(task).project_id, self.a.project_id)

    def test_archived_destination(self):
        self.set(self.x, archived_at=timezone.now())
        self.refused_with(self.run_rule(), "destination_archived")

    def test_other_team(self):
        team = TeamMaster.objects.create(team_name="O", team_email="o@x.com", owner=self.user)
        TeamMembers.objects.create(team=team, attendee=self.user)
        other = self.project("Other", "OT", team=team)
        self.refused_with(self.run_rule(destination_project=other), "destination_not_found")

    def test_private_to_public(self):
        self.set(self.a, is_private=True)
        self.refused_with(self.run_rule(), "private_to_public")

    def test_destination_with_a_guest_the_source_lacks(self):
        guest = self.person("guest", team_member=False)
        self.join(self.x, guest, "guest")
        self.refused_with(self.run_rule(), "readers_outside_source")

    def test_private_destination_with_a_member_the_source_lacks(self):
        self.set(self.a, is_private=True)
        self.set(self.x, is_private=True)
        self.join(self.x, self.person("extra"))
        self.refused_with(self.run_rule(), "readers_outside_source")

    def test_public_destination_of_a_private_source_is_refused_even_with_equal_members(self):
        self.set(self.a, is_private=True)
        self.refused_with(self.run_rule(), "private_to_public")

    def test_private_destination_with_fewer_readers_is_allowed(self):
        self.set(self.x, is_private=True)
        task = self.run_rule()
        self.assertEqual(self.fresh(task).project_id, self.x.project_id)

    def test_public_source_to_private_destination_with_a_non_member_of_the_team(self):
        self.set(self.x, is_private=True)
        guest = self.person("guest", team_member=False)
        self.join(self.x, guest, "guest")
        self.refused_with(self.run_rule(), "readers_outside_source")

    def test_sub_task_assignee_outside_the_destination(self):
        # On the source only, and no longer on the team: they may not hold work in X.
        gone = self.person("gone", team_member=False)
        self.join(self.a, gone)
        self.join(self.x, gone)
        ProjectMembers.objects.filter(project=self.x, attendee=gone).delete()
        self.rule_row = self.rule()
        task = self.task()
        self.task(title="child", parent_task_id=task.task_id, assignee=gone)
        self.start(task)
        self.refused_with(task, "assignee_not_on_destination")

    def test_nested_task_is_skipped(self):
        self.rule()
        parent = self.task(title="parent")
        child = self.task(title="child", parent_task_id=parent.task_id)
        self.start(child)
        self.assertEqual(self.fresh(child).project_id, self.a.project_id)
        run = AutomationRun.objects.get(task=child)
        self.assertEqual((run.state, run.reason), ("skipped", "nested"))
        self.assertEqual(self.history(child, REFUSED)[0].metadata["reason"], "nested")

    def test_assignee_who_may_not_be_assigned(self):
        outsider = self.person("out", team_member=False)
        task = self.run_rule(set_assignee=outsider, destination_project=None, set_status="")
        self.refused_with(task, "assignee_not_allowed")

    def test_custom_status_turned_off(self):
        accepted = create_status(self.x, name="Accepted", category="Open", actor=self.user)
        self.set(accepted, archived_at=timezone.now())
        self.refused_with(self.run_rule(set_custom_status=accepted), "bad_status")

    def test_nothing_runs_in_an_archived_source(self):
        self.rule()
        task = self.task()
        self.set(self.a, archived_at=timezone.now())
        task.status = "WIP"
        task.save(update_fields=["status"])
        self.assertEqual(automation_engine.flush(), {})

    def test_refusal_history_names_no_other_project(self):
        self.set(self.a, is_private=True)
        task = self.run_rule()
        (row,) = self.history(task, REFUSED)
        self.assertNotIn("Xray", json.dumps(row.metadata))
        self.assertNotIn("toProjectId", row.metadata)


class RulesDisclosureTests(AutomationTestBase):
    def test_a_rule_assigns_nobody_who_could_not_open_a_private_project(self):
        self.a.is_private = True
        self.a.save()
        outsider = self.person("teammate")
        response = self.client.post(
            reverse("automation_rules", args=[self.a.project_id]),
            {"name": "n", "trigger_type": "created", "set_assignee_id": str(outsider.id)},
            format="json",
        )
        self.assertEqual(response.data["code"], "bad_assignee")
        self.rule(destination_project=None, set_status="", set_assignee=outsider)
        task = self.task()
        self.start(task)
        self.refused_with(task, "assignee_not_allowed")
        self.assertEqual(self.fresh(task).assignee_id, self.user.id)

    def test_history_says_less_than_the_runs_log(self):
        self.x.archived_at = timezone.now()
        self.x.save()
        self.rule()
        task = self.task()
        self.start(task)
        self.assertEqual(self.history(task, REFUSED)[0].metadata["reason"], "destination")
        self.assertEqual(AutomationRun.objects.get().reason, "destination_archived")
        rule = AutomationRule.objects.get()
        url = reverse("automation_rule_runs", args=[self.a.project_id, rule.id])
        self.assertEqual(self.client.get(url).data["runs"][0]["reason"], "destination_archived")
        editor = self.person("ed")
        self.join(self.a, editor, "editor")
        self.authenticate(editor)
        self.assertEqual(self.client.get(url).data["runs"][0]["reason"], "destination")

    def test_a_saver_who_cannot_read_the_destination_gets_the_task_as_it_was(self):
        self.x.is_private = True
        self.x.save()
        ProjectMembers.objects.filter(project=self.x, attendee=self.user2).delete()
        self.rule()
        task = self.task()
        self.authenticate(self.user2)
        response = self.start(task)
        self.assertEqual(response.data["task"]["project"], self.a.project_id)
        self.assertTrue(response.data["task"]["displayId"].startswith("AA-"))
        self.assertEqual(response.data["movedFromProjectId"], self.a.project_id)
        self.assertEqual(self.fresh(task).project_id, self.x.project_id)

    def test_one_request_acts_on_so_many_tasks_and_leaves_the_rest_to_the_tick(self):
        self.rule()
        tasks = [self.task(), self.task()]
        with mock.patch.object(automation_engine, "INLINE_LIMIT", 1):
            for task in tasks:
                task.status = "WIP"
                task.save(update_fields=["status"])
            self.assertEqual(len(automation_engine.flush()), 1)
        self.assertEqual(AutomationRun.objects.filter(state="pending").count(), 1)

    def test_a_body_that_is_not_an_object_is_refused(self):
        for name in ("automation_rules", "automation_rule_dry_run"):
            url = reverse(name, args=[self.a.project_id])
            self.assertEqual(self.client.post(url, [1], format="json").status_code, 400)


class RulesApiTests(AutomationTestBase):
    def url(self, name="automation_rules", project=None, *extra):
        return reverse(name, args=[(project or self.a).project_id, *extra])

    def body(self, **extra):
        return {
            "name": "Move started",
            "trigger_type": "status_changed",
            "trigger_value": {"status": "WIP"},
            "conditions": [{"field": "tag", "op": "is", "value": "tag-a"}],
            "destination_project_id": self.x.project_id,
            "set_status": "Open",
            **extra,
        }

    def create(self, **extra):
        return self.client.post(self.url(), self.body(**extra), format="json")

    def detail(self, rule_id):
        return self.url("automation_rule_detail", None, rule_id)

    def assert_code(self, response, code, status_code=400):
        self.assertEqual((response.status_code, response.data.get("code")), (status_code, code))

    def test_create_list_patch_delete(self):
        created = self.create()
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["destination"]["name"], "Xray")
        self.assertEqual(created.data["ownerId"], str(self.user.id))
        second = self.create(name="Second")
        self.assertEqual(second.data["position"], 1)
        listed = self.client.get(self.url())
        self.assertEqual([r["name"] for r in listed.data["rules"]], ["Move started", "Second"])

        patched = self.client.patch(
            self.detail(created.data["ruleId"]), {"name": "Renamed"}, format="json"
        )
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(patched.data["name"], "Renamed")
        self.assertEqual(patched.data["destination"]["projectId"], self.x.project_id)

        order = self.client.put(
            self.url("automation_rule_order"),
            {"order": [second.data["ruleId"], created.data["ruleId"]]},
            format="json",
        )
        self.assertEqual([r["name"] for r in order.data["rules"]], ["Second", "Renamed"])
        self.assert_code(
            self.client.put(self.url("automation_rule_order"), {"order": []}, format="json"),
            "bad_order",
        )
        deleted = self.client.delete(self.detail(created.data["ruleId"]))
        self.assertEqual([r["name"] for r in deleted.data["rules"]], ["Second"])

    def test_who_may_read_and_write(self):
        rule_id = self.create().data["ruleId"]
        stranger = self.person("stranger")
        for user, expected in ((stranger, 404), (self.user2, 403)):
            self.authenticate(user)
            for response in (
                self.client.get(self.url()),
                self.create(),
                self.client.get(self.detail(rule_id)),
                self.client.patch(self.detail(rule_id), {"enabled": False}, format="json"),
                self.client.delete(self.detail(rule_id)),
                self.client.get(self.url("automation_rule_runs", None, rule_id)),
                self.client.put(self.url("automation_rule_order"), {"order": [rule_id]}),
                self.client.post(self.url("automation_rule_dry_run"), {"rule_id": rule_id}),
            ):
                self.assertEqual(response.status_code, expected, response.data)
        self.unauthenticate()
        self.assertEqual(self.client.get(self.url()).status_code, 401)
        self.assertTrue(AutomationRule.objects.get().enabled)

    def test_a_deleted_destination_is_said_to_be_gone_and_never_named(self):
        # MIP-2544: to a reader on it and to one who never was, and when the row itself is gone.
        rule_id = self.create().data["ruleId"]
        editor = self.person("ed")
        self.join(self.a, editor, "editor")
        gone = {"projectId": None, "name": None, "visible": False, "gone": True}

        def destinations():
            for reader in (self.user, editor):
                self.authenticate(reader)
                yield self.client.get(self.detail(rule_id)).data["destination"]

        self.x.is_deleted = True
        self.x.save(update_fields=["is_deleted"])
        self.assertEqual(list(destinations()), [gone, gone])
        AutomationRule.objects.update(destination_project=None)
        self.assertEqual(list(destinations()), [gone, gone])

    def test_a_rule_of_another_project_is_not_reached_through_this_one(self):
        rule_id = self.create().data["ruleId"]
        response = self.client.get(self.url("automation_rule_detail", self.x, rule_id))
        self.assertEqual(response.status_code, 404)

    def test_an_editor_who_saves_takes_the_rule_over(self):
        rule_id = self.create().data["ruleId"]
        editor = self.person("ed")
        self.join(self.a, editor, "editor")
        self.authenticate(editor)
        # Not on the destination: any change but "off" is refused, and says no name.
        refused = self.client.patch(self.detail(rule_id), {"name": "Mine"}, format="json")
        self.assert_code(refused, "destination_not_found")
        shown = self.client.get(self.detail(rule_id))
        self.assertEqual(
            shown.data["destination"],
            {"projectId": None, "name": None, "visible": False, "gone": False},
        )
        self.assertIsNone(shown.data["addTag"])
        self.assertNotIn("Xray", json.dumps(self.client.get(self.url()).data))
        self.assert_code(
            self.client.post(
                self.url("automation_rule_dry_run"), {"rule_id": rule_id}, format="json"
            ),
            "destination_not_found",
        )
        off = self.client.patch(self.detail(rule_id), {"enabled": False}, format="json")
        self.assertEqual((off.status_code, off.data["ownerId"]), (200, str(self.user.id)))

        self.join(self.x, editor, "viewer")
        on = self.client.patch(self.detail(rule_id), {"enabled": True}, format="json")
        self.assertEqual((on.status_code, on.data["ownerId"]), (200, str(editor.id)))

    def test_save_time_guards(self):
        hidden = self.project("Secret", "SE", owner=self.person("sec"), is_private=True)
        archived = self.project("Old", "OL", archived_at=timezone.now())
        missing = self.create(destination_project_id=987654)
        for response in (missing, self.create(destination_project_id=hidden.project_id)):
            self.assert_code(response, "destination_not_found")
        self.assertEqual(missing.data["error"], "Destination project not found.")
        self.assert_code(
            self.create(destination_project_id=archived.project_id), "destination_archived"
        )
        self.assert_code(self.create(destination_project_id=self.a.project_id), "same_project")
        self.x.is_private = True
        self.x.save()
        self.join(self.x, self.person("extra"))
        # A public source: everyone on the team already may join it, so a private
        # destination with a team member is fine.
        self.assertEqual(self.create().status_code, 201)
        guest = self.person("guest", team_member=False)
        self.join(self.x, guest, "guest")
        self.assert_code(self.create(), "readers_outside_source")
        self.a.is_private = True
        self.a.save()
        self.x.is_private = False
        self.x.save()
        self.assert_code(self.create(), "private_to_public")

    def test_validation(self):
        review = create_status(self.x, name="Accepted", category="Open", actor=self.user)
        cases = [
            ({"name": ""}, "bad_name"),
            ({"trigger_type": "deleted"}, "invalid_trigger"),
            ({"trigger_value": {"status": "Deleted"}}, "unknown_status"),
            ({"trigger_type": "tag_added", "trigger_value": {"tag": "nope"}}, "unknown_tag"),
            ({"conditions": "x"}, "invalid_condition"),
            ({"conditions": [{"field": "nope"}]}, "invalid_condition"),
            ({"conditions": [{"field": "priority", "value": "Huge"}]}, "invalid_condition"),
            ({"conditions": [{"field": "tag", "value": "nope"}]}, "unknown_tag"),
            ({"conditions": [{"field": "milestone", "value": 99}]}, "invalid_condition"),
            ({"conditions": [{"field": "assignee", "value": "x"}]}, "invalid_condition"),
            ({"add_tag": "nope"}, "unknown_tag"),
            ({"set_status": "Deleted"}, "unknown_status"),
            ({"set_status": "WIP", "set_custom_status_id": review.status_id}, "unknown_status"),
            ({"set_assignee_id": str(uuid.uuid4())}, "bad_assignee"),
            ({"destination_project_id": "abc"}, "destination_not_found"),
            ({"destination_project_id": None, "set_status": ""}, "no_action"),
        ]
        for extra, code in cases:
            with self.subTest(extra=extra):
                self.assert_code(self.create(**extra), code)
        self.assertFalse(AutomationRule.objects.exists())

    def test_a_custom_status_implies_its_category(self):
        review = create_status(self.x, name="Accepted", category="Open", actor=self.user)
        created = self.create(set_status=None, set_custom_status_id=review.status_id)
        self.assertEqual(
            (created.data["setStatus"], created.data["setCustomStatusId"]),
            ("Open", review.status_id),
        )
        patched = self.client.patch(
            self.detail(created.data["ruleId"]), {"set_status": "Closed"}, format="json"
        )
        self.assertIsNone(patched.data["setCustomStatusId"])

    def test_the_rule_cap(self):
        with mock.patch.object(automation_rules, "MAX_RULES", 1):
            self.assertEqual(self.create().status_code, 201)
            self.assert_code(self.create(), "too_many_rules")

    def test_plan(self):
        rule_id = self.create().data["ruleId"]
        self.plan.return_value = False
        self.assert_code(self.create(), "plan_required", 403)
        self.assert_code(
            self.client.patch(self.detail(rule_id), {"name": "n"}, format="json"),
            "plan_required",
            403,
        )
        self.assertFalse(self.client.get(self.url()).data["planAllows"])
        off = self.client.patch(self.detail(rule_id), {"enabled": False}, format="json")
        self.assertEqual(off.status_code, 200)

    def test_an_archived_project_is_read_only(self):
        self.create()
        self.a.archived_at = timezone.now()
        self.a.save()
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        self.assertNotEqual(self.create().status_code, 201)

    def test_dry_run_matches_current_tasks_and_writes_nothing(self):
        hit = self.task(status="WIP", tags=self.tags("tag-a"))
        self.task(status="WIP")
        self.task(status="Open", tags=self.tags("tag-a"))
        parent = self.task(title="p")
        nested = self.task(status="WIP", tags=self.tags("tag-a"), parent_task_id=parent.task_id)
        before = (TaskActivity.objects.count(), AutomationRun.objects.count())
        response = self.client.post(self.url("automation_rule_dry_run"), self.body(), format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["matched"], 2)
        by_task = {i["taskId"]: (i["outcome"], i["reason"]) for i in response.data["items"]}
        self.assertEqual(by_task[hit.task_id], ("would_apply", ""))
        self.assertEqual(by_task[nested.task_id], ("skipped", "nested"))
        self.assertEqual((TaskActivity.objects.count(), AutomationRun.objects.count()), before)
        self.assertFalse(AutomationRule.objects.exists())
        self.assertEqual(self.fresh(hit).project_id, self.a.project_id)

    def test_runs_log_hides_a_task_the_reader_cannot_open(self):
        rule_id = self.create().data["ruleId"]
        task = self.task(tags=self.tags("tag-a"))
        self.start(task)
        url = self.url("automation_rule_runs", None, rule_id)
        (run,) = self.client.get(url).data["runs"]
        self.assertEqual((run["state"], run["taskId"]), ("applied", task.task_id))

        editor = self.person("ed")
        self.join(self.a, editor, "editor")
        self.authenticate(editor)
        (run,) = self.client.get(url).data["runs"]
        self.assertEqual((run["taskId"], run["displayId"]), (None, None))


class PlanGateTests(BaseAPITestCase):
    def test_free_is_refused_and_core_allowed(self):
        from origin.search_engine import quota

        for tier, allowed in (("free", False), ("core", True), ("pro", True)):
            with mock.patch.object(quota, "get_effective_tier", return_value=tier):
                self.assertEqual(automation_rules.plan_allows(self.user.id), allowed)


class RulesRejectedTests(AutomationTestBase):
    """Rejected (MIP-2494) as a trigger and an action, built in or through a custom status."""

    def url(self, name):
        return reverse(name, args=[self.a.project_id])

    def body(self, **extra):
        return {
            "name": "Rejected",
            "trigger_type": "status_changed",
            "trigger_value": {"status": "Rejected"},
            "set_status": "Rejected",
            **extra,
        }

    def test_the_api_takes_rejected_as_trigger_and_action(self):
        created = self.client.post(self.url("automation_rules"), self.body(), format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(
            (created.data["triggerValue"], created.data["setStatus"]),
            ({"status": "Rejected"}, "Rejected"),
        )

    def test_a_rejected_task_sets_off_a_move(self):
        self.rule(trigger_value={"status": "Rejected"}, set_status="")
        task = self.task()
        self.put(task, status="Rejected")
        task = self.fresh(task)
        self.assertEqual((task.project_id, task.status), (self.x.project_id, "Rejected"))

    def test_a_rule_rejects_a_task(self):
        self.rule(destination_project=None, set_status="Rejected")
        task = self.task()
        self.start(task)
        self.assertEqual(self.fresh(task).status, "Rejected")
        self.assertEqual(len(self.history(task, APPLIED)), 1)

    def test_rejecting_by_rule_frees_what_the_task_blocked(self):
        self.rule(destination_project=None, set_status="Rejected")
        blocker, waiting = self.task(), self.task()
        TaskDependency.objects.create(team=self.team, blocker_task=blocker, blocked_task=waiting)
        self.assertEqual(self.fresh(waiting).status, "Blocked")
        self.start(blocker)
        self.assertEqual(self.fresh(blocker).status, "Rejected")
        self.assertEqual(self.fresh(waiting).status, "Open")

    def test_a_custom_status_of_the_rejected_category(self):
        wont = create_status(self.a, name="Won't do", category="Rejected", actor=self.user)
        dropped = create_status(self.x, name="Dropped", category="Rejected", actor=self.user)
        self.rule(
            trigger_value={"custom_status_id": wont.status_id},
            set_status="Rejected",
            set_custom_status=dropped,
        )
        task = self.task()
        self.put(task, status="Rejected", custom_status=wont.status_id)
        task = self.fresh(task)
        self.assertEqual(
            (task.project_id, task.status, task.custom_status_id),
            (self.x.project_id, "Rejected", dropped.status_id),
        )

    def test_a_custom_rejected_status_also_matches_the_built_in_trigger(self):
        wont = create_status(self.a, name="Won't do", category="Rejected", actor=self.user)
        self.rule(trigger_value={"status": "Rejected"}, set_status="")
        task = self.task()
        self.put(task, status="Rejected", custom_status=wont.status_id)
        self.assertEqual(self.fresh(task).project_id, self.x.project_id)

    def test_dry_run_lists_rejected_tasks_with_the_guards(self):
        task = self.task(status="Rejected")
        self.task()
        body = self.body(set_status="", destination_project_id=self.x.project_id)
        result = self.client.post(self.url("automation_rule_dry_run"), body, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(
            [(i["taskId"], i["status"], i["outcome"]) for i in result.data["items"]],
            [(task.task_id, "Rejected", "would_apply")],
        )
        rule = self.rule(trigger_value={"status": "Rejected"}, set_status="", enabled=False)
        TaskMaster.objects.filter(pk=task.pk).update(parent_task_id=self.task().task_id)
        nested = self.client.post(
            self.url("automation_rule_dry_run"), {"rule_id": rule.id}, format="json"
        )
        self.assertEqual(
            [(i["outcome"], i["reason"]) for i in nested.data["items"]], [("skipped", "nested")]
        )

    def test_the_guards_hold_for_a_rejected_task(self):
        self.x.is_private = False
        self.a.is_private = True
        self.a.save(update_fields=["is_private"])
        self.x.save(update_fields=["is_private"])
        self.rule(trigger_value={"status": "Rejected"}, set_status="")
        task = self.task()
        self.put(task, status="Rejected")
        self.refused_with(task, "private_to_public")
        self.assertEqual(self.fresh(task).status, "Rejected")
