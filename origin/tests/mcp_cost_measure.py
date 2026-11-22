"""Per-tool MCP cost on a typical workspace (MIP-1961). A measurement, not a test.

    docker/dt.sh <worktree> <suffix> 'PUBLIC_API_RATE=100000/min MCP_COST_RUNS=9 \
        python manage.py test origin.tests.mcp_cost_measure --noinput'

The file name has no `test_` prefix on purpose: CI discovery never runs it; naming the module runs it.
It drives every published read tool and the core write tools through the real endpoint
(`POST /api/public/v1/mcp`, so auth, plan gate, limits, result cap and logging are all in the cost)
on a TYPICAL workspace (5 people, 4 projects, 200 tasks of about 1 KB, notes of about 3 KB, one
chat), and prints, per tool, the median of `MCP_COST_RUNS` calls (first call dropped as warm-up):

    queries   database queries (Django `CaptureQueriesContext`)
    bytes     the JSON-RPC response body as sent (`len(res.content)`)
    wall_ms   wall time of the whole request, test client included
    cpu_ms    `time.process_time()` delta: CPU of this process (server work and client in one thread)

`search_workspace` is driven with the retrieval stubbed (`search_kb.search`), so its numbers are the
tool's own work; the model and embedding spend of a real search is NOT measured here, it is read from
the spend ledger (`AiSpendEvent`, surface `mcp`) and priced from `apis/llm_models.yaml`.
Keep `MCP_COST_RUNS` at 9 or below: the test user is on the Free plan, whose 50 monthly creations
run out (create_task, create_task_plan, create_note) and the later runs would be refused. Set
`PUBLIC_API_RATE=100000/min`, or the per-key throttle (120/min) answers 429 mid-run.
The test DB is tmpfs Postgres on the dev machine and the client is in-process: the numbers rank tools
against each other; they are not production latencies.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import collab_push
from origin.views.public.mcp import surfaces
from origin.views.public.mcp import tools as mcp_tools

from .mcp_big_seed import blocks, one
from .test_mcp_endpoint import McpBase

User = get_user_model()
RUNS = int(os.environ.get("MCP_COST_RUNS", "9"))


def _typical_search_hit() -> dict:
    """What a typical search returns: 10 hits of 3 chunks of 600 characters."""
    return {
        "results": [
            {
                "entity_type": "task",
                "entity_id": str(i),
                "title": f"Task {i} about the login flow",
                "snippet": "s" * 200,
                "chunks": [{"chunk_id": f"c{i}-{j}", "text": "x" * 600} for j in range(3)],
            }
            for i in range(10)
        ]
    }


@override_settings(
    COLLAB_INTERNAL_URL="http://collab.internal:8891", COLLAB_INTERNAL_SECRET="test-only-secret"
)
class MeasureMcpCost(McpBase):
    def seed_typical(self) -> None:
        team, user, user2 = self.team, self.user, self.user2
        people = [user, user2]
        for i in range(3):
            u = User.objects.create_user(username=f"p{i}", email=f"p{i}@ex.com", password="x")
            TeamMembers.objects.create(team=team, attendee=u)
            people.append(u)
        projects = [self.project]
        for i in range(3):
            projects.append(
                ProjectMaster.objects.create(
                    team=team,
                    project_name=f"Project {i}",
                    code=f"PR{i}",
                    owner=user,
                    project_system_user=user,
                )
            )
        for p in projects:
            for u in people:
                ProjectMembers.objects.get_or_create(team=team, project=p, attendee=u)
        tasks = TaskMaster.objects.bulk_create(
            [
                TaskMaster(
                    team=team,
                    project=projects[i % 4],
                    reporter=user,
                    assignee=people[i % 5],
                    title=f"Task number {i} " + "word " * 6,
                    status=("Open", "WIP", "Closed")[i % 3],
                    project_task_number=100 + i,
                    content=blocks(1000),
                )
                for i in range(200)
            ]
        )
        self.tasks = tasks
        self.milestone = MilestoneMaster.objects.create(
            team=team, project=self.project, reporter=user, title="Typical milestone", status="Open"
        )
        for i in range(
            29
        ):  # a real project has dozens of milestones; list_milestones reads them all
            MilestoneMaster.objects.create(
                team=team,
                project=self.project,
                reporter=user,
                title=f"Milestone {i}",
                status="Open",
            )
        TaskMaster.objects.filter(task_id__in=[t.task_id for t in tasks[:24]]).update(
            milestone=self.milestone
        )
        TaskComments.objects.bulk_create(
            [
                TaskComments(
                    task=t,
                    sender=people[(i + j) % 5],
                    comment_id=j + 1,
                    comment_body=one("c" * 200),
                )
                for i, t in enumerate(tasks[:40])
                for j in range(3)
            ]
        )
        TaskActivity.objects.bulk_create(
            [
                TaskActivity(
                    team=team,
                    project=t.project,
                    task=t,
                    actor=user2,
                    action_type=TaskActivityActionType.STATUS,
                    field_name="status",
                    old_value="Open",
                    new_value="WIP",
                )
                for t in tasks[:40]
                for _ in range(5)
            ]
        )
        self.note = PersonalNoteMaster.objects.create(
            team=team, owner=user, title="Typical note", body=blocks(3000)
        )
        NotePermissionMaster.objects.create(
            team=team, user=user, note_id=self.note.note_id, note_type=1, role_id=1
        )
        for i in range(29):
            PersonalNoteMaster.objects.create(
                team=team, owner=user, title=f"Note {i}", body=blocks(3000)
            )
        self.gm = Channel.objects.create(team=team, kind=ChannelKind.GM, title="Typical chat")
        for m in (user, user2):
            ChannelMember.objects.create(channel=self.gm, user=m)
        Message.objects.bulk_create(
            [
                Message(
                    channel=self.gm,
                    sender=user2,
                    seq=i + 1,
                    body=one("m" * 120),
                    body_text="m" * 120,
                )
                for i in range(60)
            ]
        )
        InboxItems.objects.bulk_create(
            [
                InboxItems(
                    team=team,
                    sender=user2,
                    receiver=user,
                    item_type=0,
                    item_body={"title": f"Item {i}", "text": "i" * 300},
                )
                for i in range(20)
            ]
        )

    def reads(self) -> dict[str, dict]:
        note = {"note_id": self.note.note_id, "note_type": "personal"}
        t = self.tasks[0]
        pid = self.project.project_id
        return {
            "get_task": {"task_id": t.task_id},
            "list_my_tasks": {},
            "list_tasks": {"project_id": pid},
            "list_projects": {},
            "list_milestones": {},
            "list_sprints": {"project_id": pid},
            "get_task_blockers": {"task_id": t.task_id},
            "get_task_history": {"task_id": self.tasks[1].task_id},
            "search_workspace": {"query": "login flow"},
            "get_note": note,
            "list_my_chats": {},
            "get_chat_thread": {"chat_type": "gm", "chat_id": str(self.gm.id)},
            "list_my_mentions": {},
            "list_channel_members": {"channel_id": str(self.gm.id)},
            "get_current_user": {},
            "get_team_members": {},
            "list_project_members": {"project_id": pid},
            "list_project_tags": {"project_id": pid},
            "list_task_dependencies": {"project_id": pid},
            "list_note_folders": {},
            "list_note_access": note,
            "get_note_history": note,
            "list_uncompleted_todos": {},
            "list_today_todos": {},
            "list_calendars": {},
            "get_my_blockers": {},
            "list_my_inbox": {},
            "get_my_throughput": {"days": 30},
            "get_workload_distribution": {},
            "get_top_task_closers": {"days": 30},
            "get_task_throughput_stats": {"days": 30},
            "get_stale_tasks": {"stale_days": 1},
            "get_progress_history": {"task_id": self.tasks[1].task_id},
            "get_team_task_summary": {},
            "get_project_activity_ranking": {"metric": "task_count"},
            "get_milestone_assignee_counts": {},
            "get_project_summary": {"project_id": pid},
            "get_my_task_summary": {},
            "get_my_briefing": {},
            "get_item_line": {"task_id": self.tasks[1].task_id},
            "get_timeline": {"anchor_type": "task", "anchor_id": str(self.tasks[1].task_id)},
            "list_my_unread": {},
            "get_my_importance": {},
            "get_my_memory": {},
            "list_my_schedules": {},
            "get_milestone_summary": {"milestone_id": str(self.milestone.milestone_id)},
            "find_task_by_delivery": {"pr_url": "https://github.com/genos-tech/genos-api/pull/1"},
        }

    def writes(self):
        """Core write tools: a callable per call, so every run changes fresh data."""
        pid = self.project.project_id
        ids = [t.task_id for t in self.tasks]
        n = iter(range(10_000))
        return {
            "create_task": lambda: {"title": f"New task {next(n)}", "project_id": pid},
            "create_task_plan": lambda: {
                "project_id": pid,
                "tasks": [{"title": f"Plan {next(n)} a"}, {"title": f"Plan {next(n)} b"}],
            },
            "update_task": lambda: {"task_id": ids[5], "title": f"Renamed {next(n)}"},
            "update_tasks_bulk": lambda: {
                "updates": [
                    {
                        "task_id": ids[6 + j],
                        "status": ("Open", "WIP", "Blocked")[(next(n) + j) % 3],
                        "start_date": f"2026-12-{(next(n) % 27) + 1:02d}",
                        "rationale": "measure",
                    }
                    for j in range(3)
                ]
            },
            "add_comment": lambda: {"task_id": ids[9], "body_text": f"A comment {next(n)}"},
            "add_task_delivery": lambda: {
                "task_id": ids[10],
                "pr_url": f"https://github.com/genos-tech/genos-api/pull/{next(n) + 100}",
            },
            "create_note": lambda: {
                "note_type": "personal",
                "title": f"Made {next(n)}",
                "content_text": "Some text.\n\nMore text.",
            },
            "edit_note_block": lambda: {
                "note_type": "personal",
                "note_id": self.note.note_id,
                "block_id": "b3",
                "op": "replace",
                "content_text": f"Edited {next(n)}",
            },
            "update_note": lambda: {
                "note_id": self.note.note_id,
                "note_type": "personal",
                "title": f"Typical note {next(n)}",
            },
        }

    def _measure(self, name: str, make_args) -> dict:
        rows = []
        for i in range(RUNS + 1):
            args = make_args()
            # The connection's query log holds 9,000 entries; once full a capture would read 0.
            connection.queries_log.clear()
            cpu0 = time.process_time()
            wall0 = time.perf_counter()
            with CaptureQueriesContext(connection) as ctx:
                res = self._call(name, args)
            wall = (time.perf_counter() - wall0) * 1000
            cpu = (time.process_time() - cpu0) * 1000
            if res.status_code != 200 or res.data.get("result", {}).get("isError", True):
                return {"error": json.dumps(res.data, default=str)[:200]}
            if i == 0:
                continue  # warm-up
            if os.environ.get("MCP_COST_SHOW") == name:
                print("RESPONSE", name, res.content[:600])
            rows.append((len(ctx), len(res.content), wall, cpu))
        med = [statistics.median(c) for c in zip(*rows)]
        return {
            "queries": int(med[0]),
            "bytes": int(med[1]),
            "wall_ms": round(med[2], 1),
            "cpu_ms": round(med[3], 1),
        }

    def test_measure(self):
        self.seed_typical()
        self._key(scope=SCOPE_WRITE)
        published = {t.name for t in mcp_tools.visible_tools(SCOPE_WRITE)}
        results: dict[str, dict] = {}
        # The collab service is stubbed as answering the block edit (a live document).
        no_doc = mock.Mock(
            status_code=200,
            json=mock.Mock(
                return_value={
                    "ok": True,
                    "op": "replace",
                    "blockId": "b3",
                    "newBlockIds": [],
                    "blocks": blocks(3000),
                }
            ),
        )
        with (
            mock.patch(
                "origin.search_engine.agent.tools.search_kb.search",
                return_value=_typical_search_hit(),
            ),
            mock.patch.object(collab_push.requests, "post", return_value=no_doc),
        ):
            for name, args in self.reads().items():
                if name in published:
                    results[name] = self._measure(name, lambda a=args: a)
            for name, make in self.writes().items():
                results[name] = self._measure(name, make)
        core = set(surfaces.CORE_TOOLS)
        print("\nMCP_COST_BEGIN")
        print("tool\tcore\tqueries\tbytes\twall_ms\tcpu_ms")
        for name, r in sorted(results.items(), key=lambda kv: -kv[1].get("cpu_ms", 0)):
            if "error" in r:
                print(f"{name}\t{'Y' if name in core else ''}\tERROR\t{r['error']}")
                continue
            print(
                f"{name}\t{'Y' if name in core else ''}\t{r['queries']}\t{r['bytes']}"
                f"\t{r['wall_ms']}\t{r['cpu_ms']}"
            )
        print("MCP_COST_END")
        print("core tools not driven:", sorted(n for n in core if n not in results))
