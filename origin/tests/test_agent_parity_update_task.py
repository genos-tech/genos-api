"""`update_task` takes `links` and `reporter_id` (MIP-1561, flag `AGENT_PARITY_TOOLS`).

The task page saves a task's links and reporter with the rest of the task, through the PUT. The tool
could not write either. The core tests drive the same change through the REST PUT and through the
tool on twin tasks and compare where each ends up: the stored columns and the history rows the
signals write. The rest pin each guard: a refused call changes nothing, even when the same call
carried a valid change.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller, undo
from origin.search_engine.agent.model_fence import unfence_arguments
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.fetch_task import FETCH_TASK
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase

User = get_user_model()
PARITY = {"AGENT_PARITY_TOOLS": True}

SPEC = {"id": "link-1", "url": "https://example.com/spec", "title": "Spec", "isGitHub": False}
PR = {
    "id": "link-2",
    "url": "https://github.com/genos-tech/genos-api/pull/9",
    "title": "PR 9",
    "isGitHub": True,
    "isAutoLinked": True,
}


def _activity(task, action_type):
    return [
        (field, old, new, {k: v for k, v in (meta or {}).items() if k != "via"})
        for field, old, new, meta in TaskActivity.objects.filter(task=task, action_type=action_type)
        .order_by("activity_id")
        .values_list("field_name", "old_value", "new_value", "metadata")
    ]


class ParityUpdateBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, PARITY)
        patcher.start()
        self.addCleanup(patcher.stop)
        hook = mock.patch("origin.search_engine.agent.tools.task_links.ensure_webhooks_for_links")
        self.hook = hook.start()
        self.addCleanup(hook.stop)

        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code="WEB",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.guest = User.objects.create_user(
            username="guest", email="guest@example.com", password="x"
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.guest)
        self.stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="x"
        )
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=other_team, attendee=self.stranger)

    def task(self, title="T", **kwargs):
        kwargs.setdefault("project", self.project)
        kwargs.setdefault("team", self.team)
        kwargs.setdefault("reporter", self.user)
        return TaskMaster.objects.create(title=title, status="Open", **kwargs)

    def set(self, task, **extra):
        return UPDATE_TASK.run({"task_id": task.task_id, **extra}, self.ctx)

    def ui_put(self, task, **payload):
        self.authenticate()
        res = self.client.put("/api/v2/task/", {"task_id": task.task_id, **payload}, format="json")
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        return res

    def snapshot(self, task):
        task.refresh_from_db()
        return (task.title, task.links, task.reporter_id, task.ts_updated_at)

    def refused_and_unchanged(self, task, message=None, **extra):
        """Refused, and nothing changed even though the call also carried a valid title."""
        before = self.snapshot(task)
        with self.assertRaises(ToolError) as caught:
            self.set(task, title="A new title", **extra)
        if message:
            self.assertIn(message, str(caught.exception))
        self.assertEqual(self.snapshot(task), before)
        return str(caught.exception)


class LinksTests(ParityUpdateBase):
    def test_setting_links_matches_the_uis_put(self):
        by_tool, by_ui = self.task("by tool"), self.task("by ui")

        out = self.set(by_tool, links=[{"url": "https://example.com/spec", "title": "Spec"}])
        self.ui_put(by_ui, links=[SPEC])

        by_tool.refresh_from_db()
        by_ui.refresh_from_db()

        def shape(task):
            return [{k: v for k, v in link.items() if k != "id"} for link in task.links]

        self.assertEqual(shape(by_tool), shape(by_ui))
        self.assertEqual(out["changed_fields"], ["links"])
        # A link change writes no history row on either path.
        kinds = lambda task: set(  # noqa: E731
            TaskActivity.objects.filter(task=task).values_list("action_type", flat=True)
        )
        self.assertEqual(kinds(by_tool), kinds(by_ui))

    def test_the_task_is_findable_by_the_next_sync(self):
        task = self.task()
        before = task.ts_updated_at

        self.set(task, links=["https://example.com/a"])

        task.refresh_from_db()
        self.assertGreater(task.ts_updated_at, before)

    def test_a_link_already_on_the_task_keeps_its_row_and_only_a_given_title_changes_it(self):
        task = self.task(links=[SPEC])

        self.set(task, links=[{"url": SPEC["url"]}])
        task.refresh_from_db()
        self.assertEqual(task.links, [SPEC])

        out = self.set(task, links=[{"url": SPEC["url"], "title": "Spec v2"}])
        task.refresh_from_db()
        self.assertEqual(task.links, [{**SPEC, "title": "Spec v2"}])
        self.assertEqual(out["changed_fields"], ["links"])

    def test_the_set_replaces_what_was_there_and_a_new_link_gets_an_id(self):
        task = self.task(links=[SPEC])

        self.set(task, links=[{"url": "https://example.com/new", "title": "New"}])

        task.refresh_from_db()
        self.assertEqual(len(task.links), 1)
        self.assertEqual(task.links[0]["url"], "https://example.com/new")
        self.assertTrue(task.links[0]["id"].startswith("link-"))
        self.assertFalse(task.links[0]["isGitHub"])

    def test_an_empty_list_clears_and_says_so(self):
        task = self.task(links=[SPEC])

        out = self.set(task, links=[])

        task.refresh_from_db()
        self.assertEqual(task.links, [])
        self.assertEqual(out["changed_fields"], ["links(cleared)"])

    def test_reading_the_task_and_sending_its_links_back_changes_nothing(self):
        # `get_task` fences each title; the model copies it back. The same set is a no-op:
        # no write, no timestamp, no history.
        task = self.task(links=[SPEC, {**PR, "isAutoLinked": False}])
        seen = FETCH_TASK.run({"task_id": task.task_id}, self.ctx)["links"]
        self.assertIn("<workspace_content>", seen[0]["title"])
        before = self.snapshot(task)
        history = TaskActivity.objects.filter(task=task).count()

        sent = unfence_arguments({"task_id": task.task_id, "links": seen})
        out = UPDATE_TASK.run(sent, self.ctx)

        self.assertEqual(out["changed_fields"], [])
        self.assertEqual(self.snapshot(task), before)
        self.assertEqual(TaskActivity.objects.filter(task=task).count(), history)

    def test_a_pr_the_task_page_found_by_itself_survives_a_set_that_leaves_it_out(self):
        # `isAutoLinked` rows back the PR column; the app's own list hides them and cannot
        # remove them either.
        task = self.task(links=[SPEC, PR])

        self.set(task, links=[])

        task.refresh_from_db()
        self.assertEqual(task.links, [PR])

    def test_naming_an_auto_linked_url_keeps_its_flag(self):
        task = self.task(links=[PR])

        self.set(task, links=[{"url": PR["url"], "title": "PR 9"}])

        task.refresh_from_db()
        self.assertEqual(task.links, [PR])

    def test_only_web_links_are_accepted_and_nothing_changes(self):
        task = self.task(links=[SPEC])
        for bad in (
            "javascript:alert(1)",
            "  JaVa\tScRiPt:alert(1)",
            "data:text/html,x",
            "/rel",
            "",
        ):
            with self.subTest(url=bad):
                self.refused_and_unchanged(task, links=[{"url": bad}])

    def test_a_refused_link_changes_nothing_even_with_a_valid_reporter(self):
        task = self.task()

        self.refused_and_unchanged(
            task, links=[{"url": "javascript:1"}], reporter_id=str(self.user2.id)
        )

    def test_a_pr_link_asks_for_the_repos_webhook_as_the_task_save_does(self):
        task = self.task()

        self.set(task, links=[{"url": PR["url"]}])

        self.hook.assert_called_once()
        self.assertEqual(self.hook.call_args.args[0].id, self.user.id)

    def test_a_no_op_or_a_clear_registers_nothing(self):
        task = self.task(links=[SPEC])

        self.set(task, links=[{"url": SPEC["url"]}])
        self.set(task, links=[])

        self.hook.assert_not_called()

    def test_a_milestones_links_reach_its_milestone_and_its_backing_task(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="M", reporter=self.user
        )
        backing = ensure_backing_task(milestone)

        self.set(backing, links=[{"url": "https://example.com/m", "title": "M doc"}])

        milestone.refresh_from_db()
        backing.refresh_from_db()
        self.assertEqual(milestone.links[0]["url"], "https://example.com/m")
        self.assertEqual(backing.links, milestone.links)


class ReporterTests(ParityUpdateBase):
    def test_changing_the_reporter_matches_the_uis_put(self):
        by_tool, by_ui = self.task("by tool"), self.task("by ui")

        out = self.set(by_tool, reporter_id=str(self.user2.id))
        self.ui_put(by_ui, reporter=str(self.user2.id))

        by_tool.refresh_from_db()
        by_ui.refresh_from_db()
        self.assertEqual(by_tool.reporter_id, self.user2.id)
        self.assertEqual(by_tool.reporter_id, by_ui.reporter_id)
        self.assertEqual(out["changed_fields"], ["reporter"])
        self.assertEqual(
            _activity(by_tool, TaskActivityActionType.REPORTER),
            _activity(by_ui, TaskActivityActionType.REPORTER),
        )
        self.assertEqual(len(_activity(by_tool, TaskActivityActionType.REPORTER)), 1)

    def test_the_same_reporter_is_a_no_op_for_both(self):
        by_tool, by_ui = self.task("by tool"), self.task("by ui")

        out = self.set(by_tool, reporter_id=str(self.user.id))
        self.ui_put(by_ui, reporter=str(self.user.id))

        self.assertEqual(out["changed_fields"], [])
        self.assertEqual(_activity(by_tool, TaskActivityActionType.REPORTER), [])
        self.assertEqual(_activity(by_ui, TaskActivityActionType.REPORTER), [])

    def test_a_reporter_already_on_the_task_who_left_the_team_is_not_judged_again(self):
        # The app re-sends the reporter on every save; refusing one who has since left would fail
        # every later save of the task.
        task = self.task(reporter=self.stranger)

        out = self.set(task, reporter_id=str(self.stranger.id), title="Renamed")

        self.assertEqual(out["changed_fields"], ["title"])

    def test_a_teammate_who_is_not_on_the_project_may_be_the_reporter(self):
        task = self.task()

        self.set(task, reporter_id=str(self.user2.id))

        self.assertEqual(TaskMaster.objects.get(pk=task.pk).reporter_id, self.user2.id)

    def test_a_guest_on_the_project_may_be_the_reporter(self):
        task = self.task()

        self.set(task, reporter_id=str(self.guest.id))

        self.assertEqual(TaskMaster.objects.get(pk=task.pk).reporter_id, self.guest.id)

    def test_someone_in_another_team_is_refused_in_the_words_of_a_missing_user(self):
        # The reporter is a read grant: `can_access_task` lets them open the task.
        task = self.task()
        outsider = self.refused_and_unchanged(task, reporter_id=str(self.stranger.id))
        missing = self.refused_and_unchanged(
            task, reporter_id="00000000-0000-0000-0000-000000000000"
        )

        self.assertIn("not an active member of this team", outsider)
        self.assertEqual(
            outsider.replace(str(self.stranger.id), "X"),
            missing.replace("00000000-0000-0000-0000-000000000000", "X"),
        )

    def test_a_deleted_account_is_refused(self):
        self.user2.is_deleted = True
        self.user2.save(update_fields=["is_deleted"])

        self.refused_and_unchanged(self.task(), "deleted", reporter_id=str(self.user2.id))

    def test_an_empty_reporter_is_refused_a_task_always_has_one(self):
        for empty in ("", "null"):
            with self.subTest(value=empty):
                self.refused_and_unchanged(self.task(), "always has a reporter", reporter_id=empty)

    def test_a_task_in_another_team_is_not_found_and_the_reporter_is_not_judged(self):
        other = ProjectMaster.objects.create(
            team=TeamMembers.objects.filter(attendee=self.stranger).first().team,
            project_name="Theirs",
            code="THR",
            owner=self.stranger,
            project_system_user=self.stranger,
        )
        theirs = TaskMaster.objects.create(
            team=other.team, project=other, reporter=self.stranger, title="x", status="Open"
        )

        with self.assertRaises(ToolError) as caught:
            self.set(theirs, reporter_id=str(self.user2.id))

        self.assertIn("not found", str(caught.exception))

    def test_a_milestones_reporter_reaches_its_milestone_and_its_backing_task(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="M", reporter=self.user
        )
        backing = ensure_backing_task(milestone)

        self.set(backing, reporter_id=str(self.user2.id))

        milestone.refresh_from_db()
        backing.refresh_from_db()
        self.assertEqual(milestone.reporter_id, self.user2.id)
        self.assertEqual(backing.reporter_id, self.user2.id)


class CardTests(ParityUpdateBase):
    def card(self, args, ctx=None):
        return controller._friendly_arguments(args, "update_task", ctx=ctx or self.ctx)

    def test_the_card_shows_the_old_and_the_new_reporter_by_name(self):
        task = self.task()

        shown = self.card({"task_id": task.task_id, "reporter_id": str(self.user2.id)})

        self.assertEqual(shown["reporter_id"], "otheruser")
        self.assertEqual(shown["current"]["reporter_id"], "testuser")

    def test_the_card_shows_the_links_it_replaces(self):
        task = self.task(links=[SPEC])

        shown = self.card({"task_id": task.task_id, "links": [{"url": "https://example.com/new"}]})

        self.assertEqual(shown["current"]["links"], [{"title": "Spec", "url": SPEC["url"]}])

    def test_nothing_is_shown_for_a_task_the_caller_cannot_read(self):
        task = self.task()
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.stranger.id))

        shown = self.card({"task_id": task.task_id, "links": ["https://x.example"]}, ctx)

        self.assertNotIn("current", shown)


class UndoTests(ParityUpdateBase):
    def test_a_call_with_links_or_a_reporter_is_not_undoable_and_says_why(self):
        task = self.task()
        for args in (
            {"links": [{"url": "https://x.example"}]},
            {"reporter_id": str(self.user2.id)},
        ):
            with self.subTest(args=list(args)):
                self.assertEqual(
                    undo.proposal("update_task", {"task_id": task.task_id, **args}),
                    undo.Proposal(False, undo.UNSUPPORTED_FIELDS),
                )

    def test_such_a_call_captures_no_pre_image(self):
        task = self.task()
        ctx = ToolContext(
            team_id=str(self.team.team_id), user_id=str(self.user.id), capture_undo=True
        )

        out = UPDATE_TASK.run(
            {"task_id": task.task_id, "title": "New", "links": ["https://x.example"]}, ctx
        )

        self.assertNotIn(undo.UNDO_KEY, out)
        self.assertEqual(out["changed_fields"], ["title", "links"])

    def test_a_call_without_them_is_still_undoable(self):
        task = self.task()
        ctx = ToolContext(
            team_id=str(self.team.team_id), user_id=str(self.user.id), capture_undo=True
        )

        out = UPDATE_TASK.run({"task_id": task.task_id, "title": "New"}, ctx)

        self.assertIn(undo.UNDO_KEY, out)
