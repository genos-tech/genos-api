"""Seed a team's custom-emoji catalog from the slackmoji collection.

Downloads packs from github.com/seanprashad/slackmoji (the community
collection of popular Slack emoji — party parrots, blobs, meow party,
…) and inserts them as normal `TeamEmojiMaster` rows, so they behave
exactly like hand-uploaded emoji (`:partyparrot:` in the `:` menu, the
picker's Team Emoji category, reactions). Rows are created with
`created_by = team owner` so the owner can delete them through the
normal uploader-only DELETE.

    python manage.py seed_team_emoji --team-id <uuid> --packs parrots,meow
    python manage.py seed_team_emoji --team-id <uuid>              # ALL packs (~1000+)
    python manage.py seed_team_emoji --all-teams --packs parrots --limit 30
    python manage.py seed_team_emoji --team-id <uuid> --dry-run

Idempotent: an emoji whose (sanitized) name is already active in the
team is skipped, so re-runs only fill gaps. Every file goes through the
same validation as the upload endpoint (extension allowlist +
magic-byte sniff + 512 KB cap); anything else is skipped with a note.

Run inside the api container; needs outbound HTTPS to github.com /
raw.githubusercontent.com. Listing uses the unauthenticated GitHub
contents API (one request per pack — well inside the rate limit).
"""

from __future__ import annotations

import re

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.common.team_models import TeamMaster

# Reuse the upload endpoint's validation so seeded emoji can't be
# anything a hand upload couldn't be.
from origin.views.common.team_emoji_views import _MAGIC_SNIFFERS, _NAME_RE, MAX_EMOJI_BYTES

REPO = "seanprashad/slackmoji"
LIST_URL = f"https://api.github.com/repos/{REPO}/contents/emoji"


def _sanitize_name(filename: str) -> str:
    """Filename -> shortcode matching the server name rule.

    "Party Parrot!.gif" -> "party-parrot"; keeps `_ + -` (the rule's
    extra characters — slackmoji names like "party-+1" survive).
    """
    base = filename.rsplit(".", 1)[0].lower()
    name = re.sub(r"[^a-z0-9_+-]+", "-", base).strip("-")
    return name[:50]


class Command(BaseCommand):
    help = (
        "Seed team custom emoji from the slackmoji collection (github.com/seanprashad/slackmoji)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--team-id", help="Target team UUID.")
        parser.add_argument(
            "--all-teams",
            action="store_true",
            help="Seed every non-deleted team instead of --team-id.",
        )
        parser.add_argument(
            "--packs",
            default="",
            help="Comma-separated pack names (repo subdirs of emoji/, e.g. "
            "'parrots,meow,blob'). Default: every pack in the repo.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max emoji to import per pack (0 = no limit).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be imported without writing anything.",
        )

    def handle(self, *args, **opts):
        import requests

        if bool(opts.get("team_id")) == bool(opts.get("all_teams")):
            raise CommandError("Pass exactly one of --team-id or --all-teams.")

        if opts["all_teams"]:
            teams = list(TeamMaster.objects.filter(is_deleted=False))
        else:
            try:
                teams = [TeamMaster.objects.get(team_id=opts["team_id"], is_deleted=False)]
            except TeamMaster.DoesNotExist:
                raise CommandError(f"Team {opts['team_id']} not found.")

        packs = [p.strip() for p in opts["packs"].split(",") if p.strip()]
        if not packs:
            listing = requests.get(LIST_URL, timeout=15)
            listing.raise_for_status()
            packs = sorted(e["name"] for e in listing.json() if e.get("type") == "dir")
        self.stdout.write(f"Packs: {', '.join(packs)}")

        # pack -> [(sanitized_name, ext, download_url)], fetched once and
        # reused for every team.
        catalog: dict[str, list[tuple[str, str, str]]] = {}
        for pack in packs:
            resp = requests.get(f"{LIST_URL}/{pack}", timeout=15)
            if resp.status_code != 200:
                self.stderr.write(f"  [skip pack] {pack}: listing HTTP {resp.status_code}")
                continue
            entries = []
            for item in resp.json():
                if item.get("type") != "file":
                    continue
                filename = item.get("name") or ""
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                if ext not in _MAGIC_SNIFFERS:
                    continue  # README.md and friends
                name = _sanitize_name(filename)
                if not name or not _NAME_RE.fullmatch(name):
                    continue
                entries.append((name, ext, item["download_url"]))
            if opts["limit"] > 0:
                entries = entries[: opts["limit"]]
            catalog[pack] = entries
            self.stdout.write(f"  {pack}: {len(entries)} candidate emoji")

        for team in teams:
            self._seed_team(requests, team, catalog, dry_run=opts["dry_run"])

    def _seed_team(self, requests, team, catalog, *, dry_run):
        existing = set(
            TeamEmojiMaster.objects.filter(team=team, is_deleted=False).values_list(
                "name", flat=True
            )
        )
        created = skipped_dup = skipped_bad = 0
        # Team owner as creator: the uploader-only DELETE rule then lets
        # the owner prune the pack through the normal Settings panel.
        owner = team.owner

        for pack, entries in catalog.items():
            for name, ext, url in entries:
                if name in existing:
                    skipped_dup += 1
                    continue
                if dry_run:
                    existing.add(name)
                    created += 1
                    continue
                try:
                    resp = requests.get(url, timeout=20)
                    resp.raise_for_status()
                except Exception as exc:
                    self.stderr.write(f"  [skip] {pack}/{name}: download failed ({exc})")
                    skipped_bad += 1
                    continue
                content = resp.content
                if len(content) > MAX_EMOJI_BYTES:
                    skipped_bad += 1
                    continue
                if not _MAGIC_SNIFFERS[ext](content[:12]):
                    self.stderr.write(f"  [skip] {pack}/{name}: magic-byte mismatch")
                    skipped_bad += 1
                    continue

                emoji = TeamEmojiMaster(team=team, name=name, created_by=owner)
                emoji.image_ext = ext
                emoji.image.save(f"{name}.{ext}", ContentFile(content), save=True)
                existing.add(name)
                created += 1

        label = "would create" if dry_run else "created"
        self.stdout.write(
            self.style.SUCCESS(
                f"{team.team_name}: {label} {created}, "
                f"skipped {skipped_dup} existing, {skipped_bad} invalid"
            )
        )
