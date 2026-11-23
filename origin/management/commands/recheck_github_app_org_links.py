"""Re-check the Organization installation links made before the membership check (MIP-2178).

[genos-api #1146](https://github.com/genos-tech/genos-api/pull/1146) (MIP-2169) made a new link of
an Organization installation need an active member of that organization, and MIP-2178 made the
FIRST link need an admin. Links made earlier were never checked, and a person who later leaves the
organization keeps theirs. This asks GitHub again, for each such link, whether the person who made
it (`linked_by`) is still an active member of the installation's organization, and for the first
link of an installation whether they are an admin.

    python manage.py recheck_github_app_org_links                # report only (the default)
    python manage.py recheck_github_app_org_links --all          # every link, not only the old ones
    python manage.py recheck_github_app_org_links --before 2026-11-23T01:44:16+00:00
    python manage.py recheck_github_app_org_links --unlink       # also remove the links GitHub refused

It uses the linker's own stored GitHub App user token (refreshed the usual way, which saves the
rotated token back, the only write a report makes) and the same `org_membership` call the link flow
uses. Nothing is unlinked without `--unlink`, and `--unlink` removes only a link whose verdict is
definitive: GitHub answered "not an active member" (`not_member`). A first link made by a plain
member (`first_claim_not_admin`) is only reported: the first team may have disconnected since, so
the earliest link still held is not always the first claim. A link that could not be checked (no
linker, no usable token, GitHub unreachable, the App's "Members: read" not accepted yet) is listed
as `unverified` and never removed. The organization is asked under its CURRENT login, taken from
the linker's own `GET /user/installations`, so a rename is not read as "left the organization". Unlinking removes the team's link only: the installation row, and
other teams' links, stay. It writes an audit event per removed link.

Output names a team id and the organization login, never a token, a name or an email. Production:
run it only with the maintainer's go.
"""

from __future__ import annotations

from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from origin.models.common.github_app_models import GithubAppInstallationTeam
from origin.models.common.security_event_models import SecurityAction
from origin.models.common.user_models import ConnectedAccount
from origin.services.github_app_link import (
    Installation,
    LinkError,
    list_user_installations,
    org_membership,
)
from origin.services.oauth.tokens import get_valid_access_token
from origin.services.security_events import record_security_event

# When genos-api #1146 merged: links made before it were never membership-checked.
DEFAULT_BEFORE = datetime(2026, 11, 23, 1, 44, 16, tzinfo=timezone.utc)

OK = "ok"
NOT_MEMBER = "not_member"
FIRST_CLAIM_NOT_ADMIN = "first_claim_not_admin"
UNVERIFIED = "unverified"
#: The verdicts `--unlink` acts on. Everything else is only reported.
#: `first_claim_not_admin` is only REPORTED: the earliest link still held is not always the first
#: claim ever made (the first team may have disconnected since), so it is never definitive.
UNLINKABLE = {NOT_MEMBER}


def verdict_for(link: GithubAppInstallationTeam, first_link_ids: set) -> tuple[str, str]:
    """(verdict, short reason) for one link. `first_link_ids` is the snapshot, taken before any
    link is removed, of each installation's earliest link."""
    user = link.linked_by
    if user is None:
        return UNVERIFIED, "no linker on record"
    account = ConnectedAccount.objects.filter(user=user, provider="github_app").first()
    if account is None:
        return UNVERIFIED, "the linker has no GitHub App connection"
    try:
        token = get_valid_access_token(account)
    except Exception as exc:  # noqa: BLE001 - revoked, expired, undecryptable: all "cannot check"
        return UNVERIFIED, f"no usable user token ({type(exc).__name__})"
    inst = link.installation
    item = Installation(
        installation_id=inst.installation_id,
        account_login=inst.account_login,
        account_type=inst.account_type,
        repository_selection=inst.repository_selection,
        suspended_at=inst.suspended_at,
    )
    # The stored login is from link time: an organization renamed since answers 404 under the old
    # name for everyone. The linker's own installation list carries the CURRENT login and id, so
    # ask under those when the installation is listed. An installation the linker no longer sees
    # (they lost access) falls back to the stored login: a 404 then is evidence, not a rename.
    try:
        listed = {i.installation_id: i for i in list_user_installations(token)}
    except LinkError:
        return UNVERIFIED, "GitHub could not be asked for the linker's installations (see the log)"
    item = listed.get(item.installation_id, item)
    membership = org_membership(token, item)
    if membership.verdict == "not_member":
        return NOT_MEMBER, "GitHub says the linker is not an active member"
    if not membership.active:
        return UNVERIFIED, "GitHub could not be asked (see the log)"
    if link.id in first_link_ids and membership.role != "admin":
        return FIRST_CLAIM_NOT_ADMIN, "the first link still held was made by a non-admin member"
    return OK, ""


class Command(BaseCommand):
    help = "Re-check Organization GitHub App installation links made before the membership check."

    def add_arguments(self, parser):
        parser.add_argument(
            "--before",
            help="Check links made before this ISO datetime (default: when #1146 merged).",
        )
        parser.add_argument("--all", action="store_true", help="Check every Organization link.")
        parser.add_argument(
            "--unlink",
            action="store_true",
            help="Also remove the links whose verdict is definitive (not_member). "
            "Without it nothing is changed.",
        )

    def handle(self, *args, **options):
        before = DEFAULT_BEFORE
        if options["before"]:
            try:
                before = parse_datetime(options["before"])
            except ValueError:
                before = None
            if before is None or before.tzinfo is None:
                raise CommandError("--before must be an ISO datetime with a UTC offset.")
        links = GithubAppInstallationTeam.objects.filter(
            installation__account_type="Organization"
        ).select_related("installation", "linked_by")
        if not options["all"]:
            links = links.filter(ts_linked_at__lt=before)
        links = list(links.order_by("ts_linked_at", "id"))

        # Each installation's earliest link, read before anything is removed, so a run that
        # unlinks cannot make a later link look like the first one (and a report shows what
        # --unlink would do).
        first_link_ids = set()
        seen = set()
        for link_id, installation_id in (
            GithubAppInstallationTeam.objects.filter(installation__account_type="Organization")
            .order_by("ts_linked_at", "id")
            .values_list("id", "installation_id")
        ):
            if installation_id not in seen:
                seen.add(installation_id)
                first_link_ids.add(link_id)

        counts: dict[str, int] = {}
        removed = 0
        for link in links:
            verdict, reason = verdict_for(link, first_link_ids)
            counts[verdict] = counts.get(verdict, 0) + 1
            if verdict == OK:
                continue
            action = ""
            if options["unlink"] and verdict in UNLINKABLE:
                self._unlink(link, verdict)
                removed += 1
                action = " [unlinked]"
            elif verdict in UNLINKABLE:
                action = " [would unlink with --unlink]"
            self.stdout.write(
                f"{verdict}: {link.installation.account_login} -> team {link.team_id}: "
                f"{reason}{action}"
            )
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
        self.stdout.write(f"{len(links)} Organization link(s) checked ({summary})")
        if options["unlink"]:
            self.stdout.write(f"{removed} link(s) removed")
        else:
            self.stdout.write("Report only: nothing was changed. Pass --unlink to remove.")

    def _unlink(self, link: GithubAppInstallationTeam, verdict: str) -> None:
        record_security_event(
            SecurityAction.OAUTH_DISCONNECTED,
            actor=None,
            actor_label="recheck_github_app_org_links",
            team_id=link.team_id,
            target_type="github_app_installation",
            target_id=str(link.installation.installation_id),
            target_label=f"github_app installation of {link.installation.account_login}"[:254],
            metadata={"provider": "github_app", "team_id": str(link.team_id), "verdict": verdict},
        )
        link.delete()
