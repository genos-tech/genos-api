"""`python manage.py adopt_general_chats` — mark existing teams' `general` chat as their default (MIP-1708).

New teams get a default chat marked at creation (`team_setup`), and everybody who joins them is put
into it. Teams made before that have a `general` chat nobody marked, so nothing is added to it when
someone joins. Whether existing teams should get the same is the owner's call, which is why this is
a command and not part of the migration: the migration adds the table and touches no team.

For each live team with no default chat yet and exactly ONE open `general` group chat, it marks
that chat as the default. A team with several open `general` chats is reported and skipped; one
with none is counted. `--add-members` also puts every current live member into the chat (anyone who
already has a row in it, left or not, is skipped).

DRY RUN IS THE DEFAULT: it reports, writes nothing. `--apply` needs `--yes`. Safe to run twice.

    python manage.py adopt_general_chats                          # what would be marked
    python manage.py adopt_general_chats --apply --yes            # mark only: later joiners are added
    python manage.py adopt_general_chats --apply --yes --add-members
    python manage.py adopt_general_chats --team <team id> ...     # one team
"""

from django.core.management.base import BaseCommand, CommandError

from origin.services.team_default_chats import adopt_general_chats


class Command(BaseCommand):
    help = "Mark existing teams' open `general` chat as their default chat (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write. Without it, only report.")
        parser.add_argument("--yes", action="store_true", help="Confirm --apply.")
        parser.add_argument(
            "--add-members",
            action="store_true",
            help="Also put every current live member into the chat.",
        )
        parser.add_argument("--team", help="Only this team id.")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        if apply_changes and not options["yes"]:
            raise CommandError("--apply writes to production data: add --yes.")
        if options["add_members"] and not apply_changes:
            self.stdout.write("  (--add-members counts nothing in a dry run)")

        report = adopt_general_chats(
            apply=apply_changes, add_members=options["add_members"], team_id=options["team"]
        )
        verb = "marked" if apply_changes else "would be marked"
        self.stdout.write(f"  {len(report.marked)} team(s) {verb}")
        for team_id, chat_id in report.marked:
            self.stdout.write(f"    team={team_id} chat={chat_id}")
        for team_id, count in report.ambiguous:
            self.stdout.write(f"  SKIPPED team={team_id}: {count} open `general` chats")
        self.stdout.write(f"  {report.already} team(s) already have a default chat")
        self.stdout.write(f"  {report.without_chat} team(s) have no open `general` chat")
        if apply_changes and options["add_members"]:
            self.stdout.write(f"  {report.added} member(s) added")
