"""Empty the bodies that are only an untouched template (MIP-799).

Until the create paths stopped it, a task or milestone whose author never
wrote in the template was stored with the template's italic gray prompts as
its body ("What outcome does this milestone deliver, and for whom?"). Search
and the agent no longer read those (the chunkers and `get_task` strip them),
but the stored rows still carry them, and a milestone that looks filled in
hides which ones still need a goal. This command stores `[]` instead.

A body is cleaned only if it has at least one untouched prompt AND nothing
else the author wrote (`is_untouched_template`). A single character typed
into any section keeps the whole body as it is. Custom project templates
are not recognised as prompts, so they are never touched.

Two traps this handles:

  * A milestone's body lives twice: `MilestoneMaster.description` and its
    backing task's `content`. Both are cleaned; the backing task row is also
    reached by the plain task pass if only its own copy is untouched.
  * A task body also lives in the Yjs doc the editor renders, and the next
    autosave would write the old body back over a DB-only change, so every
    changed task is pushed to genos-collab (`push_task_body`) once all rows have committed.

`ts_updated_at` is left alone (queryset `.update()` skips `auto_now`), or
every cleaned task would jump to the top of Recents. Search's incremental
indexing keys on `ts_updated_at`, so run a full
`opensearch_reindex --entity-types task milestone` afterwards.

Dry-run by default; pass --apply to write.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.template_prompts import is_untouched_template
from origin.services.collab_push import push_task_body


class Command(BaseCommand):
    help = "Store an empty body for tasks and milestones whose body is only the untouched template."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without this flag the command only lists the rows.",
        )
        parser.add_argument(
            "--team",
            default=None,
            help="Optional team_id to scope the scan.",
        )

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        team = options["team"]

        milestones = tasks = 0
        handled_task_ids: set[int] = set()
        # Pushed once every row below has committed, never inside a
        # transaction: a push that raced an uncommitted write could be
        # undone by the editor's autosave reading the old row.
        to_push: list[int] = []

        # Milestones first: description + the backing task's copy together.
        ms_qs = MilestoneMaster.objects.filter(is_deleted=False).select_related("task")
        if team:
            ms_qs = ms_qs.filter(team_id=team)
        for m in ms_qs.iterator():
            if not is_untouched_template(m.description):
                continue
            milestones += 1
            backing = m.task
            clean_backing = backing is not None and is_untouched_template(backing.content)
            self.stdout.write(
                f"  milestone {m.milestone_id} ({m.title!r}): description is only the template"
                + (f"; backing task {backing.task_id} too" if clean_backing else "")
            )
            if clean_backing:
                handled_task_ids.add(backing.task_id)
            if apply_changes:
                with transaction.atomic():
                    MilestoneMaster.objects.filter(pk=m.pk).update(description=[])
                    if clean_backing:
                        TaskMaster.objects.filter(pk=backing.pk).update(content=[])
                if clean_backing:
                    to_push.append(backing.task_id)

        # Every other task (and any backing task whose own copy is untouched
        # but whose milestone description was not matched above). Backing
        # tasks already handled with their milestone are skipped, so dry-run
        # and apply report the same rows.
        task_qs = TaskMaster.objects.filter(is_deleted=False, is_init_task=False).only(
            "task_id", "title", "content", "team_id"
        )
        if team:
            task_qs = task_qs.filter(team_id=team)
        for t in task_qs.iterator():
            if t.task_id in handled_task_ids or not is_untouched_template(t.content):
                continue
            tasks += 1
            self.stdout.write(f"  task {t.task_id} ({t.title!r}): body is only the template")
            if apply_changes:
                with transaction.atomic():
                    TaskMaster.objects.filter(pk=t.pk).update(content=[])
                to_push.append(t.task_id)

        for task_id in to_push:
            push_task_body(task_id, [])

        mode = "APPLIED" if apply_changes else "DRY RUN (pass --apply to write)"
        self.stdout.write(
            f"{mode}: {milestones} milestone description(s) and {tasks} task body(ies) "
            "that are only the untouched template"
        )
