"""Old display ids keep working after a task moves project (MIP-2497).

* Every move path keeps the number it frees as a `TaskNumberAlias`: the app's PUT, the agent's /
  MCP move (with the sub-tree) and a milestone's move.
* The number stays reserved, and a task moving back gets it again.
* Everything that turns a display id into a task finds it by the old one, under the same team and
  access rules as the current one.
* A pull request whose branch or description carries the old id still links and closes the task.
"""

from unittest import mock

from origin.models.common.team_models import TeamMaster
from origin.models.task.task_models import TaskMaster, TaskNumberAlias
from origin.search_engine import search as S
from origin.search_engine.agent.tools import ToolError
from origin.search_engine.agent.tools.add_task_delivery import ADD_TASK_DELIVERY
from origin.search_engine.agent.tools.entity_chips import chip_resolver
from origin.search_engine.agent.tools.find_task_by_delivery import FIND_TASK_BY_DELIVERY
from origin.services.task_number_alias import display_ids
from origin.services.task_reparent import move_task_to_project
from origin.services.timeline import _task_id_from
from origin.views.common.github_views import (
    _branch_match_re,
    _record_pr_link_activity,
    _tasks_for_branch,
    apply_pull_request,
)
from origin.views.public.mcp.resolve import resolve_task_ref

from .test_task_delivery import DeliveryBase


class AliasBase(DeliveryBase):
    def setUp(self):
        super().setUp()  # self.project "HK", self.task HK-42
        self.dest = self._project(self.team, "Web", "WEB")
        self.team_id = str(self.team.team_id)
        self.team_ids = [self.team.team_id]

    def _move(self, task=None, project=None):
        task = task or self.task
        move_task_to_project(task, (project or self.dest).project_id)
        task.refresh_from_db()
        return task

    def _aliases(self, task):
        return sorted(
            TaskNumberAlias.objects.filter(task=task).values_list("project__code", "number")
        )


class MovePathTests(AliasBase):
    def test_the_agent_move_keeps_the_ids_of_the_task_and_its_sub_tasks(self):
        child = self._task(43)
        TaskMaster.objects.filter(pk=child.pk).update(parent_task_id=self.task.task_id)

        self._move()
        child.refresh_from_db()

        self.assertEqual(self.task.display_id, "WEB-1")
        self.assertEqual(self._aliases(self.task), [("HK", 42)])
        self.assertEqual(self._aliases(child), [("HK", 43)])
        self.assertEqual(display_ids(child), [child.display_id, "HK-43"])

    def test_the_apps_put_keeps_the_id(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(is_init_task=False)
        self.authenticate()

        res = self.client.put(
            "/api/v2/task/",
            {"team": self.team_id, "task_id": self.task.task_id, "project": self.dest.project_id},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._aliases(self.task), [("HK", 42)])

    def test_a_milestone_move_keeps_the_ids_of_what_it_carries(self):
        self.authenticate()
        res = self.client.post(
            "/api/v2/milestone/",
            {"team_id": self.team_id, "project_id": self.project.project_id, "title": "M"},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201), res.data)
        from origin.models.task.milestone_models import MilestoneMaster

        milestone = MilestoneMaster.objects.get(title="M")
        TaskMaster.objects.filter(pk=self.task.pk).update(milestone=milestone)
        backing_number = milestone.task.project_task_number

        res = self.client.patch(
            f"/api/v2/milestone/{milestone.milestone_id}/",
            {"project_id": self.dest.project_id},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._aliases(self.task), [("HK", 42)])
        self.assertEqual(self._aliases(milestone.task), [("HK", backing_number)])

    def test_a_draft_the_member_never_saved_leaves_nothing_behind(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(is_init_task=True)
        self.task.refresh_from_db()

        self._move()

        self.assertFalse(TaskNumberAlias.objects.exists())

    def test_the_freed_number_is_not_given_to_the_next_task(self):
        self._move()  # HK-42 was the highest number of HK

        fresh = TaskMaster.objects.create(team=self.team, project=self.project, title="next")
        fresh.refresh_from_db()

        self.assertEqual(fresh.project_task_number, 43)

    def test_a_deleted_tasks_old_number_stays_reserved_and_opens_nothing(self):
        self._move()
        TaskMaster.objects.filter(pk=self.task.pk).delete()

        fresh = TaskMaster.objects.create(team=self.team, project=self.project, title="next")
        fresh.refresh_from_db()

        self.assertEqual(fresh.project_task_number, 43)
        with self.assertRaises(ToolError):
            resolve_task_ref("HK-42", self.team_id)

    def test_moving_back_gives_the_old_number_again(self):
        self._task(50)  # so MAX + 1 would not be 42 by chance
        self._move()
        self._move(project=self.project)

        self.assertEqual(self.task.display_id, "HK-42")
        self.assertEqual(self._aliases(self.task), [("WEB", 1)])
        self.assertEqual(resolve_task_ref("WEB-1", self.team_id), self.task.task_id)


class LookupTests(AliasBase):
    def setUp(self):
        super().setUp()
        self._move()

    def test_mcp_and_timeline_refs(self):
        for ref in ("HK-42", "hk-42", "#HK-42"):
            with self.subTest(ref=ref):
                self.assertEqual(
                    resolve_task_ref(ref, self.team_id, str(self.user.id)), self.task.task_id
                )
        self.assertEqual(_task_id_from("HK-42", self.team_id), self.task.task_id)

    def test_the_link_lookup_answers_with_the_current_id(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(is_init_task=False)
        self.authenticate()

        res = self.client.get(
            "/api/v2/search/taskByDisplayId/", {"team_id": self.team_id, "display_id": "HK-42"}
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["taskId"], self.task.task_id)
        self.assertEqual(res.data["displayId"], "WEB-1")

    def test_the_task_search_box(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(is_init_task=False)
        self.authenticate()

        res = self.client.get("/api/v2/search/tasks/", {"team_id": self.team_id, "query": "HK-42"})

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn(str(self.task.task_id), str(res.data))

    def test_a_chip_written_with_the_old_id_shows_the_current_one(self):
        resolve = chip_resolver(team_id=self.team_id, user_id=str(self.user.id))

        chip = resolve("display:HK-42")

        self.assertEqual(str(self.task.task_id), chip["props"]["taskId"])
        self.assertEqual(chip["props"]["displayId"], "WEB-1")

    def test_find_task_by_delivery_reads_the_old_id_in_a_branch(self):
        out = FIND_TASK_BY_DELIVERY.run({"branch": "feature/HK-42-fix"}, self.ctx)

        self.assertEqual([m["task_id"] for m in out["matches"]], [self.task.task_id])

    def test_a_renamed_code_carries_the_alias(self):
        self.project.code = "OPS"
        self.project.save(update_fields=["code"])

        self.assertEqual(resolve_task_ref("OPS-42", self.team_id), self.task.task_id)

    def test_someone_without_access_is_told_it_does_not_exist(self):
        with self.assertRaises(ToolError):
            resolve_task_ref("HK-42", self.team_id, str(self.user2.id))
        self.authenticate(self.user2)
        res = self.client.get(
            "/api/v2/search/taskByDisplayId/", {"team_id": self.team_id, "display_id": "HK-42"}
        )
        self.assertEqual(res.status_code, 404)

    def test_another_team_finds_nothing(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="other@alias.test", owner=self.user
        )
        with self.assertRaises(ToolError):
            resolve_task_ref("HK-42", str(other.team_id))
        self.assertEqual(_tasks_for_branch("HK-42", [other.team_id]), [])

    def test_an_alias_does_not_follow_a_task_into_another_team(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="other@alias.test", owner=self.user
        )
        TaskMaster.objects.filter(pk=self.task.pk).update(team=other)

        with self.assertRaises(ToolError):
            resolve_task_ref("HK-42", str(other.team_id))
        self.assertEqual(_tasks_for_branch("HK-42", [other.team_id, self.team.team_id]), [])

    def test_the_current_id_wins_over_an_alias(self):
        # Cannot happen through a move (the number is reserved); the order is the backstop.
        holder = self._task(42)

        self.assertEqual(resolve_task_ref("HK-42", self.team_id), holder.task_id)


class SearchPinTests(AliasBase):
    def setUp(self):
        super().setUp()
        self._move()

    def _pin(self, query, moved_hits, prefix_hits=()):
        client = mock.Mock(spec=["search"])
        client.search.side_effect = [
            {"hits": {"hits": list(prefix_hits)}},
            {"hits": {"hits": list(moved_hits)}},
        ]
        base = [{"term": {"team_id": self.team_id}}]
        with mock.patch.object(S, "resolve_milestone_mirrors", side_effect=lambda rows: rows):
            out = S._pin_display_id_matches(
                [],
                query,
                client=client,
                index="rag",
                base_filter=base,
                for_agent=False,
                team_id=self.team_id,
            )
        return out, client

    def _hit(self, task_id, display_id):
        return {
            "_id": f"task:{task_id}:title_content",
            "_source": {
                "chunk_id": f"task:{task_id}:title_content",
                "entity_type": "task",
                "entity_id": f"task:{task_id}",
                "chunk_type": "task_title_content",
                "title": display_id,
                "task_display_id": display_id,
                "task_id": str(task_id),
            },
        }

    def test_the_old_id_pins_the_moved_task_above_prefix_matches(self):
        out, client = self._pin(
            "HK-42", [self._hit(self.task.task_id, "WEB-1")], [self._hit(999999, "HK-420")]
        )

        self.assertEqual([e["task_display_id"] for e in out], ["WEB-1", "HK-420"])
        body = client.search.call_args.kwargs["body"]
        filters = body["query"]["bool"]["filter"]
        self.assertIn({"term": {"team_id": self.team_id}}, filters)
        self.assertIn({"terms": {"task_id": [str(self.task.task_id)]}}, filters)

    def test_an_id_nobody_left_costs_no_second_search(self):
        _out, client = self._pin("HK-7", [])

        self.assertEqual(client.search.call_count, 1)


class GithubTests(AliasBase):
    PR = "https://github.com/owner/repo/pull/5"

    def setUp(self):
        super().setUp()
        type(self.user).objects.filter(pk=self.user.pk).update(auto_close_on_pr_merge=True)
        self._move()

    def _merge(self, ref, *, title="Fix", body=""):
        pr = {
            "html_url": self.PR,
            "number": 5,
            "title": title,
            "body": body,
            "state": "closed",
            "merged": True,
            "merged_at": "2026-11-28T00:00:00Z",
            "head": {"sha": "abc", "ref": ref},
        }
        out = apply_pull_request("owner", "repo", pr, "closed", self.team_ids)
        self.task.refresh_from_db()
        return out

    def test_a_merged_branch_with_the_old_id_closes_the_task(self):
        out = self._merge("feature/HK-42-fix")

        self.assertEqual(out["updated"], 1)
        self.assertEqual(self.task.status, "Closed")

    def test_closes_and_part_of_in_the_description_read_the_old_id(self):
        self._merge("chore/deps", body="Part of HK-42")
        self.assertEqual(self.task.status, "Open")

        self._merge("chore/deps", body="Closes HK-42")
        self.assertEqual(self.task.status, "Closed")

    def test_part_of_wins_when_both_ids_of_one_task_are_named(self):
        self._merge("feature/HK-42-fix", body="Closes HK-42\n\nPart of WEB-1")

        self.assertEqual(self.task.status, "Open")

    def test_the_branch_links_the_task(self):
        created = _record_pr_link_activity("HK-42-fix", self.PR, 5, "Fix", self.team_ids)

        self.assertEqual(created, 1)

    def test_one_pattern_matches_either_id_at_a_boundary(self):
        pattern = _branch_match_re(*display_ids(self.task))

        self.assertTrue(pattern.search("fix/hk-42"))
        self.assertTrue(pattern.search("WEB-1_fix"))
        self.assertFalse(pattern.search("HK-420"))
        self.assertFalse(pattern.search("xHK-42"))

    def test_a_delivery_on_the_old_branch_name_raises_no_warning(self):
        out = ADD_TASK_DELIVERY.run(
            {"task_id": self.task.task_id, "repo": "owner/repo", "branch": "feature/HK-42-fix"},
            self.ctx,
        )

        self.assertNotIn("display_id_warning", out)


class TakenFormerNumberTests(AliasBase):
    def test_a_former_number_someone_else_holds_is_given_up_for_a_new_one(self):
        self._move()
        holder = self._task(42)  # not possible through the app; the claim must still finish

        self._move(project=self.project)

        self.assertEqual(self.task.display_id, "HK-43")
        self.assertEqual(resolve_task_ref("HK-42", self.team_id), holder.task_id)
