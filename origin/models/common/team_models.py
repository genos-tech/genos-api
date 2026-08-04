import os
import uuid

from django.db import models

from origin.models.common.user_models import (
    TIER_CHOICES,
    TIER_SET_BY_CHOICES,
    TIER_SET_BY_STRIPE,
    CustomUser,
)


def profile_image_path(instance, filename):
    return os.path.join(
        "team_profiles",
        str(instance.team_id),
        filename,
    )


class TeamMaster(models.Model):
    team_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team_name = models.CharField(unique=True, blank=False)
    team_email = models.EmailField(unique=True)
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="own_teams",
        to_field="id",
    )
    profile_image_file = models.FileField(upload_to=profile_image_path, blank=True, null=True)
    profile_image_file_name = models.CharField(blank=True, null=True)
    # Team subscription plan ("one member pays, every member benefits").
    # Same ladder as CustomUser.tier; a member's effective tier is the
    # best of their own tier and their teams' plans — see
    # `origin.search_engine.quota.get_effective_tier`. Set via
    # `manage.py feature_access set-team-plan` (later: Stripe per-seat
    # subscription webhook).
    plan = models.CharField(
        max_length=16,
        choices=TIER_CHOICES,
        default="free",
        db_index=True,
    )
    # Who last wrote `plan` — the team twin of `CustomUser.tier_set_by`,
    # same rules and same reason (a hand-set team plan is not Stripe's to
    # take away). See TIER_SET_BY_CHOICES.
    plan_set_by = models.CharField(
        max_length=16,
        choices=TIER_SET_BY_CHOICES,
        default=TIER_SET_BY_STRIPE,
    )
    # The team's Stripe customer (per-seat subscription). Mirrors
    # CustomUser.stripe_customer_id: bound on first team checkout,
    # never cleared on cancellation — the customer is reused for
    # re-subscribes, and subscription webhooks resolve back to the
    # team through this column.
    stripe_customer_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        db_index=True,
    )
    is_deleted = models.BooleanField(default=False)
    is_demo = models.BooleanField(default=False, db_index=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)


class TeamMembers(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_members",
        to_field="team_id",
    )
    attendee = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_attendees",
        to_field="id",
    )
    is_deleted = models.BooleanField(default=False)
    # Permission role within this team: "editor" or "viewer". See
    # `origin/services/member_roles.py` for the full contract.
    #
    # NOT `role` — `CustomUser.role` already exists and means the user's
    # self-declared JOB TITLE. The two are serialized side by side as
    # `role` and `memberRole`.
    #
    # "owner" is deliberately NOT stored here: `TeamMaster.owner` is the
    # single source of truth for ownership, so the owner's own row keeps
    # the `viewer` default. Read this column only through
    # `resolve_team_role`, never directly, or you will deny the owner.
    #
    # The default backfills every existing member as `viewer`, which is
    # the intended migration behaviour.
    member_role = models.CharField(max_length=16, default="viewer")
    # When the Genos Guide notes were seeded into this member's My Notes
    # for this team (services/guide_notes.py). A durable stamp, NOT a
    # folder-existence check: personal-folder deletion is a hard
    # recursive delete, so without this a user who deleted their guide
    # would get it re-seeded on every team switch. NULL = never seeded.
    guide_seeded_at = models.DateTimeField(null=True, blank=True)
    ts_joined_at = models.DateTimeField(auto_now_add=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["team", "attendee"], name="unique_team_member")
        ]


class ShareStatus(models.TextChoices):
    """Lifecycle shared by `TeamConnection` and `ExternalGrant`.

    One vocabulary for both deliberately: the two rows are the same kind
    of object at different scopes (a relationship, and an object inside
    it), they are settled by the same one-time gesture, and every read
    path asks the same question of them — `status == ACTIVE`. Two enums
    would drift.

    `DECLINED` and `REVOKED` are distinct because they are different
    facts: declined was never agreed to, revoked was agreed to and then
    withdrawn. Neither is a delete — the row survives so a re-request
    reuses it instead of accumulating a log of attempts.
    """

    PENDING = "pending", "pending"
    ACTIVE = "active", "active"
    DECLINED = "declined", "declined"
    REVOKED = "revoked", "revoked"


class TeamConnection(models.Model):
    """An agreed relationship between two teams. Grants nothing by itself.

    The prerequisite for every cross-team share, and deliberately inert:
    being connected means the two teams may now name each other in an
    `ExternalGrant`, and nothing more. No data becomes visible and no
    roster is exposed. That separation is what keeps "connect with our
    client" from meaning "hand them the workspace".

    ## The pair is normalized, so the row IS the relationship

    A connection is symmetric — "A is connected to B" and "B is connected
    to A" are one fact — so the two teams are stored ordered
    (`team_lo` < `team_hi`, compared as strings) under a unique
    constraint on the pair. Same device as `ChannelDirectPair`, for the
    same reason: unordered-pair uniqueness cannot be expressed as a
    constraint, so the ordering is imposed on write instead. Always go
    through `services/team_connection.normalize_team_pair`; a row written
    with the pair the other way round is a second, invisible
    relationship that no lookup would find.

    `requested_by_team` exists BECAUSE of that normalization — once the
    pair is sorted the row no longer remembers who asked, and both the
    approval gate ("the other side approves, never the asker") and the UI
    ("incoming" vs "sent") need to know.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT, not SET_NULL, on all three team FKs below. Elsewhere in
    # this file a lost team FK is survivable; here a connection whose
    # side is NULL is a security object that no longer names what it
    # authorizes, and `connected_team_ids` would have to guess. Team
    # deletion is soft (`is_deleted`), so PROTECT costs nothing in
    # practice and refuses the one operation that would leave a grant
    # chain dangling.
    team_lo = models.ForeignKey(
        TeamMaster,
        on_delete=models.PROTECT,
        related_name="connections_as_lo",
        to_field="team_id",
    )
    team_hi = models.ForeignKey(
        TeamMaster,
        on_delete=models.PROTECT,
        related_name="connections_as_hi",
        to_field="team_id",
    )
    status = models.CharField(
        max_length=16,
        choices=ShareStatus.choices,
        default=ShareStatus.PENDING,
        db_index=True,
    )
    # Which side asked — see the docstring on why normalization loses it.
    requested_by_team = models.ForeignKey(
        TeamMaster,
        on_delete=models.PROTECT,
        related_name="connection_requests_sent",
        to_field="team_id",
    )
    requested_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="team_connection_requests",
        to_field="id",
    )
    approved_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="team_connection_approvals",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["team_lo", "team_hi"], name="uniq_team_connection"),
            # A team connected to itself would make every "is the other
            # side connected" guard answer yes about the host.
            models.CheckConstraint(
                check=~models.Q(team_lo=models.F("team_hi")),
                name="team_connection_not_self",
            ),
        ]


class ExternalGrant(models.Model):
    """"Team B may participate in object X, which team A owns."

    The unit of consent, and the only thing either side approves. Once
    active, the guest team's own owner/editors decide WHICH of their
    people participate — freely, repeatedly, without asking again. See
    `services/external_grants`. Approval gates the relationship and the
    object; it never gates a person.

    ## The grant is not the access

    No read path consults this table. Access is carried by row types that
    predate cross-team sharing — `ChannelMember`, `ProjectMembers`,
    `NoteFolderPermission` — so every existing check keeps working with
    no knowledge of this feature: `can_access_task`, `get_effective_role`,
    `serve_media`, the OpenSearch `acl_user_ids` filter, the collab
    access check. This row is the AUTHORITY to write those rows, plus the
    record of who consented so it can be withdrawn later.

    The direct consequence, and the likeliest way to introduce a security
    bug here: **revoking must DELETE the derived rows.** A grant flipped
    to `REVOKED` while its `ChannelMember` rows survive revokes nothing
    at all. `services/external_grants.revoke_grant` is that cascade.

    ## `object_id` is a string

    The three object types have different id types — channel UUID,
    project integer, folder integer — and one nullable FK per type would
    push the "exactly one is set" invariant into application code anyway.
    A single stringified id keeps the table honest about being a generic
    grant. Same shape, and same reasoning, as
    `NoteFolderPermission.via_group_id`.
    """

    class ObjectType(models.TextChoices):
        CHANNEL = "channel", "channel"
        PROJECT = "project", "project"
        NOTE_FOLDER = "note_folder", "note folder"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # PROTECT: a grant whose connection vanished would authorize access
    # with no relationship behind it. Revoke the connection — which
    # cascades to its grants — rather than deleting the row.
    connection = models.ForeignKey(
        TeamConnection,
        on_delete=models.PROTECT,
        related_name="grants",
    )
    # The team that owns the object and is lending it out.
    owner_team = models.ForeignKey(
        TeamMaster,
        on_delete=models.PROTECT,
        related_name="external_grants_given",
        to_field="team_id",
    )
    # The team being let in. Its owner/editors administer the roster.
    guest_team = models.ForeignKey(
        TeamMaster,
        on_delete=models.PROTECT,
        related_name="external_grants_received",
        to_field="team_id",
    )
    object_type = models.CharField(max_length=16, choices=ObjectType.choices)
    object_id = models.CharField(max_length=64)
    # The most the guest team may hand its own people: `viewer` or
    # `editor` from `services/member_roles`. The host sets the ceiling,
    # the guest chooses within it, and `add_external_participants`
    # clamps — so widening later stays the host's decision alone.
    role_ceiling = models.CharField(max_length=16, default="viewer")
    status = models.CharField(
        max_length=16,
        choices=ShareStatus.choices,
        default=ShareStatus.PENDING,
        db_index=True,
    )
    invited_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        related_name="external_grants_offered",
        to_field="id",
    )
    approved_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="external_grants_accepted",
        to_field="id",
    )
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # One grant per (object, guest team). Re-offering after a
            # decline or revoke flips this row, mirroring how a
            # connection is re-requested.
            models.UniqueConstraint(
                fields=["guest_team", "object_type", "object_id"],
                name="uniq_external_grant",
            ),
            models.CheckConstraint(
                check=~models.Q(owner_team=models.F("guest_team")),
                name="external_grant_not_self",
            ),
        ]
        indexes = [
            models.Index(
                fields=["object_type", "object_id", "status"],
                name="external_grant_object_idx",
            ),
            models.Index(
                fields=["guest_team", "status"],
                name="external_grant_guest_idx",
            ),
        ]
