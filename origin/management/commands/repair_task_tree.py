"""Repair tasks whose milestone, sprint or root disagrees with their parent (MIP-1168).

    python manage.py repair_task_tree                      # report only (dry run)
    python manage.py repair_task_tree --team <id>          # scope to a team
    python manage.py repair_task_tree --project <id>       # scope to a project
    python manage.py repair_task_tree --apply              # write the fixes

Nothing is written without `--apply`. Every row it fixes gets a history entry
(`cascade: tree_repair`) and is stamped so open browsers pick the change up.
The rule it enforces is `origin/services/task_tree_consistency.py`; running it
twice writes nothing the second time.
"""

from django.core.management.base import BaseCommand

from origin.services.task_tree_consistency import (
    find_inconsistent_tasks,
    repair_inconsistent_tasks,
)


class Command(BaseCommand):
    help = "Report (or, with --apply, fix) tasks placed in a milestone their parent is not in."

    def add_arguments(self, parser):
        parser.add_argument("--team", default=None, help="Only this team_id.")
        parser.add_argument("--project", type=int, default=None, help="Only this project id.")
        parser.add_argument(
            "--apply", action="store_true", help="Write the fixes. Without it, report only."
        )

    def handle(self, *args, **options):
        fixes = find_inconsistent_tasks(team_id=options["team"], project_id=options["project"])
        if not fixes:
            self.stdout.write(self.style.SUCCESS("Every task agrees with its parent."))
            return
        self.stdout.write(f"{len(fixes)} task(s) disagree with where they hang:")
        for fix in fixes:
            self.stdout.write(f"  {fix.describe()}")
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing written. Pass --apply to fix."))
            return
        written = repair_inconsistent_tasks(fixes)
        self.stdout.write(self.style.SUCCESS(f"Fixed {written} task(s)."))
