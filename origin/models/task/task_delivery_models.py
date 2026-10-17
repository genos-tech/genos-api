from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.task.task_models import TaskMaster


class TaskDeliveryState(models.TextChoices):
    """Where the work on one delivery link stands.

    `BRANCH` is a link recorded before any PR exists. `UNKNOWN` is a PR
    whose state nobody could read: a backfilled row (the old PR_LINKED
    activity never recorded state), or a manual link made by a user with
    no GitHub connection. It is excluded from both "in flight" and
    "merged" on purpose — calling it either would be a guess. The next
    webhook event for the PR, or re-adding the link, corrects it.
    """

    BRANCH = "branch", "Branch, no PR yet"
    OPEN = "open", "PR open"
    MERGED = "merged", "PR merged"
    CLOSED = "closed", "PR closed without merging"
    UNKNOWN = "unknown", "PR state unknown"


class TaskDeliverySource(models.TextChoices):
    WEBHOOK = "webhook", "GitHub webhook"
    MANUAL = "manual", "Added by a user"
    BACKFILL = "backfill", "Backfilled from PR history"


class TaskDelivery(models.Model):
    """One repo + branch + PR that is delivering a task (MIP-687).

    The display id in a branch name is the join key between Genos and
    GitHub, but until this table Genos stored nothing on its side of the
    join: the PR link lived in a comment, or in a `PR_LINKED` activity
    row, neither of which records whether the PR merged. Keeping the
    state here is what makes "what is in flight" and "merged but still
    Open" plain queries.

    Rows are written by `GithubWebhookView` (a PR on a branch carrying
    the task's display id) and by the `add_task_delivery` tool (any
    branch, for the ones whose name carries no display id). The webhook
    keeps `state` current on every `pull_request` event — but only for
    rows in the teams of the user who registered the repo's hook, the
    same tenant boundary as everything else it writes.

    `repo_owner` / `repo_name` are stored lowercase and `pr_url` is
    rebuilt from them (`task_delivery.canonical_pr_url`), because GitHub
    names are case-insensitive and the dedup key must not be.
    """

    delivery_id = models.BigAutoField(primary_key=True)
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_task_deliveries",
        to_field="team_id",
    )
    task = models.ForeignKey(
        TaskMaster,
        on_delete=models.CASCADE,
        related_name="deliveries",
        to_field="task_id",
    )
    repo_owner = models.CharField(max_length=100)
    repo_name = models.CharField(max_length=100)
    # Branch names are case-sensitive in git, so kept as given. Empty
    # for a backfilled PR whose branch was never recorded.
    branch = models.CharField(max_length=255, blank=True, default="")
    pr_url = models.CharField(max_length=300, null=True, blank=True)
    pr_number = models.IntegerField(null=True, blank=True)
    # From GitHub, so attacker-controllable text — wrap it before an LLM
    # reads it.
    pr_title = models.CharField(max_length=500, blank=True, default="")
    state = models.CharField(max_length=16, choices=TaskDeliveryState.choices)
    merged_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=16, choices=TaskDeliverySource.choices)
    created_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_task_deliveries",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Writing the same PR link twice must not duplicate it.
            models.UniqueConstraint(
                fields=["task", "pr_url"],
                name="task_delivery_unique_pr",
                condition=models.Q(pr_url__isnull=False),
            ),
            # Nor the same branch, before it has a PR. Once a PR arrives
            # it is attached to this row rather than added beside it.
            models.UniqueConstraint(
                fields=["task", "repo_owner", "repo_name", "branch"],
                name="task_delivery_unique_branch",
                condition=models.Q(pr_url__isnull=True),
            ),
        ]
        indexes = [
            # The webhook's state sync: every row for one PR.
            models.Index(fields=["pr_url"], name="task_delivery_pr_url_idx"),
            # `find_task_by_delivery` on a branch.
            models.Index(fields=["team", "branch"], name="task_delivery_branch_idx"),
        ]
