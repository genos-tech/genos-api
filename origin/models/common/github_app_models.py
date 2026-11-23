"""GitHub App installations (MIP-2086, design MIP-2057).

An installation is GitHub's record that an account (a user or an organization) installed the
Genos GitHub App on some of its repositories. It belongs to GitHub, not to a Genos team: the
link to the teams that may receive its events is a separate row, written only after the
installing person proved (with their own user token) that the installation is theirs. A webhook
payload never names a team; the tenant comes from `GithubAppInstallationTeam`.

Nothing here holds a credential. Installation access tokens (1 hour) are minted on demand by
`origin.services.github_app` and live only, encrypted, in the cache.
"""

import uuid

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

INSTALLATION_ACCOUNT_TYPE_CHOICES = [
    ("User", "User"),
    ("Organization", "Organization"),
]
REPOSITORY_SELECTION_CHOICES = [
    ("all", "all"),
    ("selected", "selected"),
]


class GithubAppInstallation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # GitHub's installation id (an integer well past 32 bits for new installs).
    installation_id = models.BigIntegerField(unique=True)
    # Who installed it, as GitHub names them: informational (shown, never used to authorize).
    account_login = models.CharField(max_length=255)
    account_type = models.CharField(max_length=16, choices=INSTALLATION_ACCOUNT_TYPE_CHOICES)
    repository_selection = models.CharField(
        max_length=16, choices=REPOSITORY_SELECTION_CHOICES, default="selected"
    )
    # Set by the `installation.suspend` event, cleared by `unsuspend`. A suspended installation
    # writes nothing and mints no token.
    suspended_at = models.DateTimeField(null=True, blank=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.account_login} (installation {self.installation_id})"

    @property
    def is_active(self) -> bool:
        return self.suspended_at is None


class GithubAppInstallationTeam(models.Model):
    """Which Genos team an installation's events may write into.

    One installation can serve several teams (a person in two teams links it twice), and a team
    can hold several installations (a personal account and an organization). The pair is unique.
    `linked_by` is informational, like `GithubWebhookRegistration.registered_by`: the link belongs
    to the team, so it stays when that person leaves.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(
        GithubAppInstallation, on_delete=models.CASCADE, related_name="team_links"
    )
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.CASCADE,
        related_name="github_app_installations",
        to_field="team_id",
    )
    linked_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    ts_linked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["installation", "team"],
                name="github_app_installation_team_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"installation {self.installation_id} -> team {self.team_id}"


class GithubAppInstallationRepository(models.Model):
    """One repository an installation covers (MIP-2088), kept in step by the `installation` and
    `installation_repositories` events. A delivery is routed only if its repository is listed here
    (or the installation covers every repository of its account): the payload's `installation.id`
    alone never widens what an installation may reach."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    installation = models.ForeignKey(
        GithubAppInstallation, on_delete=models.CASCADE, related_name="repositories"
    )
    # GitHub's repository id: survives a rename. Null when an event did not carry it.
    github_repo_id = models.BigIntegerField(null=True, blank=True)
    # "owner/name", lower-cased: GitHub names are case-insensitive.
    full_name = models.CharField(max_length=255)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["installation", "full_name"],
                name="github_app_installation_repo_unique",
            ),
        ]
        indexes = [
            models.Index(fields=["full_name"], name="github_app_repo_full_name_idx"),
            models.Index(fields=["github_repo_id"], name="github_app_repo_id_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} (installation {self.installation_id})"


class GithubAppWebhookDelivery(models.Model):
    """A delivery of the App webhook that was processed (MIP-2088): the replay guard.

    `X-GitHub-Delivery` is unique per delivery and unchanged by a redelivery. A row is written
    before a delivery is processed and removed if processing fails, so a signed request captured
    and replayed is answered `duplicate` and writes nothing, while a failed delivery (answered
    5xx) can still be redelivered. Pruned after 30 days by the redeliver cron."""

    delivery_id = models.CharField(max_length=64, primary_key=True)
    event = models.CharField(max_length=64, blank=True, default="")
    ts_received_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self) -> str:
        return f"{self.event} {self.delivery_id}"
