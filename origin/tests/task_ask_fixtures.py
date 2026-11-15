"""Tasks, milestones and every way the asker may NOT read one, for the MIP-1057 tests (a conversation per task).

Not a test module. `TaskWorld` is `AskRefWorld` (a host team A, a guest team B, a stranger team C, and an `ask()` that
posts to the real view with the model faked) plus the objects an Ask about a task or milestone is about. The asker is
`a_owner`; `a_editor` and `a_viewer` are the other members of team A.
"""

from __future__ import annotations

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.tests.ask_ref_fixtures import AskRefWorld

TASK_URL = "/api/v2/agent/task-session/"
SECRET = "SECRET-TASK-WORDS-4711"
MISSING_ID = 987654321


def doc(*paragraphs):
    """A BlockNote body of plain paragraphs."""
    return [
        {"type": "paragraph", "content": [{"type": "text", "text": text}]} for text in paragraphs
    ]


class TaskWorld(AskRefWorld):
    def setUp(self):
        super().setUp()
        self.project.code = "HOST"
        self.project.save(update_fields=["code"])
        self.join(self.a_owner)

    # -- people and projects -------------------------------------------------

    def join(self, user, project=None, team=None):
        project = project or self.project
        ProjectMembers.objects.get_or_create(
            team=team or project.team, project=project, attendee=user
        )

    def remove_asker(self, project=None):
        """Take the asker off the project as a MEMBER. A project's owner cannot be removed (and with
        no member row they are still a reader: `can_access_task`, and the search ACL since MIP-1591),
        so the project goes to `a_editor` first."""
        project = project or self.project
        ProjectMaster.objects.filter(pk=project.pk).update(owner=self.a_editor)
        ProjectMembers.objects.filter(project=project, attendee=self.a_owner).delete()

    def private_project(self, owner=None, members=(), team=None, **kw):
        owner = owner or self.a_editor
        self._projects = getattr(self, "_projects", 0) + 1
        project = ProjectMaster.objects.create(
            team=team or self.team_a,
            project_name=f"Private {self._projects}",
            owner=owner,
            project_system_user=owner,
            is_private=True,
            **kw,
        )
        for user in {owner, *members}:
            self.join(user, project)
        return project

    # -- tasks ---------------------------------------------------------------

    def make_task(self, title="Fix the login flow", *, project=None, **kw):
        kw.setdefault("status", "WIP")
        kw.setdefault("priority", "High")
        kw.setdefault("reporter", self.a_editor)
        kw.setdefault("assignee", self.a_editor)
        project = project or self.project
        return TaskMaster.objects.create(team=project.team, project=project, title=title, **kw)

    def make_milestone(self, title="Q4 launch", *, project=None, assignees=(), **kw):
        """A milestone: its backing task row (`is_milestone`) and the milestone row on it. `(task, milestone)`."""
        project = project or self.project
        kw.setdefault("status", "WIP")
        kw.setdefault("reporter", self.a_editor)
        task = TaskMaster.objects.create(
            team=project.team, project=project, title=title, is_milestone=True, **kw
        )
        milestone = MilestoneMaster.objects.create(
            team=project.team, project=project, title=title, task=task, reporter=self.a_editor
        )
        for user in assignees:
            MilestoneAssignees.objects.create(team=project.team, milestone=milestone, user=user)
        return task, milestone

    def ctx(self, task):
        return {"task_id": task.task_id}

    # -- the unreadable ones, one per way a task can be refused --------------

    def unreadable(self, title=SECRET):
        """`{name: task id}`: every way the asker cannot read a task. All of them hold `title`."""
        out = {"missing": MISSING_ID}
        out["deleted"] = self.make_task(title, is_deleted=True).task_id
        out["another team's task"] = self.make_task(
            title, project=self.foreign_project, assignee=self.c_owner, reporter=self.c_owner
        ).task_id
        private = self.private_project()
        out["a private project they are not on"] = self.make_task(title, project=private).task_id
        collaborating = self.make_task(title, project=private)
        collaborating.collaborators.add(self.a_owner)
        out["only a collaborator"] = collaborating.task_id
        gone = self.private_project(members=[self.a_owner])
        out["a deleted project"] = self.make_task(title, project=gone).task_id
        ProjectMaster.objects.filter(pk=gone.pk).update(is_deleted=True)
        left = self.private_project(members=[self.a_owner])
        left_task = self.make_task(title, project=left)
        ProjectMembers.objects.filter(project=left, attendee=self.a_owner).delete()
        out["removed from the project"] = left_task.task_id
        return out

    # -- the restore endpoint --------------------------------------------------

    def restore(self, task_id, *, team=None, **extra):
        team = self.team_a if team is None else team
        params = {"team_id": str(team.team_id), "task_id": task_id, **extra}
        return self.client.get(TASK_URL, params)
