"""A member's busy day, seeded in bulk, for the briefing's query-count tests (MIP-2202).

The shape is the production one measured on 2026-11-23: hundreds of items changed "today" and
"yesterday" across tasks, comments, notes, to-dos and PRs, many written by an AI app through
OAuth. `seed_day(offset, n)` adds `n` changed items for one local day; `n` is the only knob, so a
test can ask the same briefing at two sizes and compare the number of queries.
"""

from __future__ import annotations

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.chat.unified_models import ChannelKind
from origin.models.common.item_activity_models import MemberItemDay, MemberItemDayState
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_delivery_models import TaskDeliveryState
from origin.models.task.task_models import TaskMaster
from origin.tests.test_item_activity import ActivityBase

# One item in this many is each kind (the rest are tasks changed by the member).
_COMMENTED = 4
_PERSONAL_NOTE = 12
_TASK_NOTE = 12
_PR_TASK = 9


class BriefingShape(ActivityBase):
    """`ActivityBase`'s clock: "today" is local day 3, "yesterday" day 2, `now` is noon of today."""

    def setUp(self):
        super().setUp()
        self.user.member_events_enabled = True
        self.user.save(update_fields=["member_events_enabled"])
        self._channels = [
            self.channel([self.user, self.user2], kind=ChannelKind.GM, title=f"Room {i}")
            for i in range(5)
        ]
        # Several projects, each with a milestone: a real member's work is spread over them.
        self._projects = [self.project]
        self._milestones = []
        for i in range(7):
            project = ProjectMaster.objects.create(
                team=self.team,
                project_name=f"Project {i}",
                code=f"P{i}",
                owner=self.user2,
                project_system_user=self.user2,
            )
            for u in (self.user, self.user2):
                ProjectMembers.objects.create(team=self.team, project=project, attendee=u)
            self._projects.append(project)
        for project in self._projects:
            backing = self.make_task(
                f"Milestone {project.code}", project=project, is_milestone=True
            )
            self._milestones.append(
                MilestoneMaster.objects.create(
                    team=self.team,
                    project=project,
                    title=f"Milestone {project.code}",
                    reporter=self.user,
                    task=backing,
                )
            )

    def seed_day(self, day: int, n: int) -> None:
        """`n` items the member changed on local day `day` (3 = today, 2 = yesterday)."""

        def minute(i: int) -> int:
            return self.day_minutes(day, 1 + (i * 7) % (11 * 60 if day == 3 else 23 * 60))

        for i in range(n):
            if i % _PERSONAL_NOTE == _PERSONAL_NOTE - 1:
                note = PersonalNoteMaster.objects.create(
                    team=self.team, owner=self.user, title=f"My note {day}-{i}", body=[]
                )
                for k in range(2):
                    self.version(1, note, self.user, minute(i) + k)
                self.marker(self.user, minute(i), "note", f"personal:{note.note_id}", kind="write")
                continue
            task = self.make_task(
                f"Item {day}-{i}",
                project=self._projects[i % len(self._projects)],
                priority="High" if i % 5 == 0 else "Normal",
                due_date=None,
            )
            self._ai_app_and_chat(task, i, minute(i))
            if i % _TASK_NOTE == _TASK_NOTE - 2:
                note = self.task_note(task, owner=self.user2, title=f"Task note {day}-{i}")
                self.version(2, note, self.user, minute(i))
            if i % _PR_TASK == _PR_TASK - 1:
                self.activity(task, None, minute(i), action="pr_linked", old=None, new=None)
                self.activity(
                    task, None, minute(i) + 1, action="status_changed", closedByPrMerge=True
                )
                continue
            if i % _COMMENTED == 0:
                for k in range(2):
                    self.comment(task, self.user, minute(i) + k)
                continue
            # Most of the member's day is an AI app working through OAuth.
            via = {"via": {"kind": "oauth", "app": "claude"}} if i % 3 else {}
            self.activity(task, self.user, minute(i), **via)
            if i % 2:
                self.activity(
                    task,
                    self.user,
                    minute(i) + 1,
                    action="priority_changed",
                    old="Low",
                    new="High",
                    **via,
                )
            # Others talk on the member's tasks too (ranking and "changed for me" read it).
            if i % 6 == 0:
                self.comment(task, self.user2, minute(i) + 2)
        group, first_time = ToDoGroup.objects.get_or_create(
            team=self.team, user=self.user, local_date=self.d(day)
        )
        for i in range(8 if first_time else 0):
            row = ToDoItem.objects.create(group=group, title=f"Done {day}-{i}", is_completed=True)
            ToDoItem.objects.filter(pk=row.pk).update(ts_completed_at=self.at(minute(i)))
        for i in range(30):
            self.message(self._channels[i % len(self._channels)], self.user, minute(i))

    def _ai_app_and_chat(self, task, i: int, at: int) -> None:
        """The member's AI app opened or changed things (markers), a chat thread talks about
        some tasks, and a PR is linked to others."""
        kind = "write" if i % 2 else "read"
        self.marker(self.user, at, "task", task.task_id, kind=kind, client="Claude")
        if i % 7 == 0:
            milestone = self._milestones[i % len(self._milestones)]
            self.marker(self.user, at, "milestone", milestone.milestone_id, kind=kind)
            self.marker(self.user, at, "project", milestone.project_id, kind="read")
        if i % 8 == 0:
            channel = self._channels[i % len(self._channels)]
            root = self.message(channel, self.user2, at)
            self.link_thread(task, root)
            self.message(channel, self.user, at + 1, root=root)
            self.message(channel, self.user2, at + 2, root=root)
            self.marker(self.user, at, "chat_thread", f"{channel.id}:{root.id}", kind="read")
        if i % 10 == 0:
            self.delivery(task, TaskDeliveryState.UNKNOWN, None, n=self.counter())

    def record_week(self) -> None:
        """What earlier briefings recorded: per-day rows for the member's tasks over the week, and
        the watermark at yesterday, so this briefing fills nothing and its ranking is engaged
        (rows on the week's first two days)."""
        ids = list(
            TaskMaster.objects.filter(title__startswith="Item", assignee=self.user)
            .order_by("task_id")
            .values_list("task_id", flat=True)[:200]
        )
        rows = []
        for n, task_id in enumerate(ids):
            for back in range(7, 0, -1):
                if (n + back) % 3 == 0:
                    continue
                rows.append(
                    MemberItemDay(
                        user=self.user,
                        team=self.team,
                        task_id=task_id,
                        day=self.d(3 - back),
                        mine=1 + n % 3,
                        others=n % 4,
                        notes=n % 2,
                        chat=n % 2,
                        prs=0,
                    )
                )
        MemberItemDay.objects.bulk_create(rows)
        MemberItemDayState.objects.create(user=self.user, team=self.team, built_through=self.d(2))

    def seed_busy_member(self, today: int, yesterday: int) -> None:
        self.seed_day(3, today)
        self.seed_day(2, yesterday)
        self.record_week()
        assert self.rows(), "the week should have been recorded"
