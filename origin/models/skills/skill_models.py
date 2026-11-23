"""Genos Skills (MIP-2040): a skill is a note plus a metadata row.

The body is a `PersonalNoteMaster` owned by the author and filed in a hidden folder whose
`scope` is `"skill"` (`PersonalNoteFolder.SCOPE_SKILL`), so the owner gets the collaborative
editor, version history and markdown in and out for free, and every sidebar list that filters
by folder scope keeps skills out of My Notes and Team Notes. The `Skill` row holds what the
note cannot: the name typed after `/`, the description, tags, visibility, version counters and
the star / install counters.

Published versions are IMMUTABLE: `SkillVersion` refuses an update and a delete of a single
row (the whole skill going away cascades). A team or public skill is read, run and installed
from a version, never from the live note, so the author cannot change the text under a reader
without cutting a new version.

Design: "Design: Genos Skills" (note 274 on MIP-2039); decisions K1-K14 (note 276).
"""

from __future__ import annotations

import uuid

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

# Only the models are exported by `from .skills.skill_models import *`: the constants below are
# short generic names (VISIBILITY_*, STATUS_*) that would shadow other models'.
__all__ = [
    "PublicProfile",
    "Skill",
    "SkillGrant",
    "SkillInstall",
    "SkillReport",
    "SkillRunLog",
    "SkillStar",
    "SkillTag",
    "SkillVersion",
]

VISIBILITY_PRIVATE = "private"
VISIBILITY_TEAM = "team"
VISIBILITY_SPECIFIC = "specific"
VISIBILITY_PUBLIC = "public"
VISIBILITY_CHOICES = [
    (VISIBILITY_PRIVATE, "private: the owner only"),
    (VISIBILITY_TEAM, "team: every member of the owner's team, not guests"),
    (
        VISIBILITY_SPECIFIC,
        "specific: the people, mention groups, group chats and projects the owner chose "
        "(`SkillGrant`), when they are on the owner's team",
    ),
    (VISIBILITY_PUBLIC, "public: any signed-in Genos user"),
]

STATUS_ACTIVE = "active"
STATUS_HIDDEN_BY_TEAM = "hidden_by_team"
STATUS_REPORTED_HIDDEN = "reported_hidden"
STATUS_IN_REVIEW = "in_review"
STATUS_TAKEN_DOWN = "taken_down"
STATUS_CHOICES = [
    (STATUS_ACTIVE, "active"),
    (STATUS_HIDDEN_BY_TEAM, "hidden from the team by the team owner"),
    (STATUS_REPORTED_HIDDEN, "hidden after reports, waiting for an operator"),
    (STATUS_IN_REVIEW, "held after the publish-time scan, waiting for an operator"),
    (STATUS_TAKEN_DOWN, "taken down by an operator"),
]

SOURCE_USER = "user"
SOURCE_MCP = "mcp"
SOURCE_AGENT = "agent"
SOURCE_OFFICIAL = "official"
SOURCE_CHOICES = [
    (SOURCE_USER, "written by a person in the app"),
    (SOURCE_MCP, "written by a connected AI app"),
    (SOURCE_AGENT, "written by the in-app agent"),
    (SOURCE_OFFICIAL, "shipped by Genos"),
]

TOOL_LEVEL_READ = "read"
TOOL_LEVEL_ACT = "act"
TOOL_LEVEL_CHOICES = [
    (TOOL_LEVEL_READ, "reads only"),
    (TOOL_LEVEL_ACT, "may propose changes (the caller approves each)"),
]

EFFORT_CHOICES = [("low", "low"), ("medium", "medium"), ("high", "high")]


class Skill(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(
        TeamMaster, on_delete=models.CASCADE, to_field="team_id", related_name="skills"
    )
    owner = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, to_field="id", related_name="skills"
    )
    # The name typed after `/`: lower case, a-z0-9 and single hyphens, 2 to 40 characters, unique
    # per owner within a team (the constraint below). Rules in `services/skills/text.py`.
    slug = models.CharField(max_length=40)
    title = models.CharField(max_length=120)
    description = models.CharField(max_length=280, blank=True, default="")
    # Up to 5 lower-case tag NAMES, each one a `SkillTag` in the OWNER's catalog (MIP-2211: the
    # editor only picks from the catalog). Names, not foreign keys, so versions, the SKILL.md
    # file and the Hub keep reading plain strings; `services/skills/tags.py` keeps the two in step.
    tags = models.JSONField(default=list, blank=True)
    icon = models.CharField(max_length=16, blank=True, default="")
    # The live body: `PersonalNoteMaster.note_id`, filed in the owner's hidden skill folder. A
    # plain integer like the note tree's other ids, no FK.
    note_id = models.BigIntegerField(unique=True, null=True, blank=True)
    # The placeholder shown after `/name`, for example `[project] [week]`.
    argument_hint = models.CharField(max_length=120, blank=True, default="")
    # The most the skill may use. Always capped by the CALLER's own level when it runs.
    tool_level = models.CharField(max_length=8, choices=TOOL_LEVEL_CHOICES, default=TOOL_LEVEL_READ)
    # "" = the caller's own effort setting.
    effort = models.CharField(max_length=8, choices=EFFORT_CHOICES, blank=True, default="")
    language = models.CharField(max_length=16, blank=True, default="")

    visibility = models.CharField(
        max_length=8, choices=VISIBILITY_CHOICES, default=VISIBILITY_PRIVATE, db_index=True
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    # The newest cut version (0 = none yet) and the one readers outside the owner's reach get.
    # They move together at every publish; a version is cut only by `publish` or by sharing the
    # skill, never by saving an edit (MIP-2177).
    current_version_no = models.PositiveIntegerField(default=0)
    published_version_no = models.PositiveIntegerField(default=0)
    # The first version a reader OUTSIDE the team may open once the skill is public. Versions cut
    # while it was team-only stay team-only: they were never checked for what a stranger must not
    # see (chips, workspace links, names). Set when the skill becomes public.
    public_from_version_no = models.PositiveIntegerField(default=0)
    forked_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="forks"
    )
    forked_from_version_no = models.PositiveIntegerField(null=True, blank=True)

    source = models.CharField(max_length=8, choices=SOURCE_CHOICES, default=SOURCE_USER)
    is_official = models.BooleanField(default=False)

    # Denormalised counters, kept by the service (never by a signal): the directory sorts on them.
    star_count = models.PositiveIntegerField(default=0)
    run_count_30d = models.PositiveIntegerField(default=0)
    copy_count = models.PositiveIntegerField(default=0)
    install_count = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)

    # What an operator last decided about a held, reported or taken-down skill (MIP-2044): the
    # sentence the author is shown, who decided and when. Empty while the skill was never moderated.
    moderation_reason = models.CharField(max_length=500, blank=True, default="")
    moderated_at = models.DateTimeField(null=True, blank=True)
    moderated_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    # The Hub (MIP-2047). What a stranger searches and sorts is the PUBLISHED version, never the
    # owner's draft, so it is kept here, written by `services/skills/hub.refresh_listing` whenever a
    # version is cut: one indexed read answers a directory page, no join to the versions table.
    # `hub_text` is the lower-cased title, name, description and tags (substring match, which also
    # finds Japanese and Chinese, that no full-text config splits into words); `search_vector` adds
    # the body for ranked word search.
    hub_text = models.TextField(blank=True, default="")
    hub_tags = models.JSONField(default=list, blank=True)
    search_vector = SearchVectorField(null=True, blank=True)
    # When the newest version was cut: the Hub's "recent". `published_at` is the first one, and
    # `updated_at` moves with every draft save, so neither says when readers last got new text.
    last_published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # A name is unique among one owner's skills in a team (any visibility); a TEAM skill's
            # name is also unique in its team, and a PUBLIC skill's among all public skills
            # (MIP-2210). A private skill is compared with nobody else's. The rules and their
            # sentences are `service.name_conflict`; these are the net for two saves at once.
            models.UniqueConstraint(fields=["team", "owner", "slug"], name="skill_slug_per_owner"),
            models.UniqueConstraint(
                fields=["team", "slug"],
                condition=models.Q(visibility="team"),
                name="skill_slug_unique_in_team",
            ),
            models.UniqueConstraint(
                fields=["slug"],
                condition=models.Q(visibility="public"),
                name="skill_slug_unique_public",
            ),
        ]
        indexes = [
            models.Index(fields=["team", "visibility"], name="skill_team_visibility_idx"),
            models.Index(fields=["visibility", "-star_count"], name="skill_visibility_stars_idx"),
            # The directory's three sorts, over the public active skills only (a small slice of the
            # table): each is one index scan of a page, whatever the search or tag filter adds.
            models.Index(
                fields=["-star_count", "-last_published_at"],
                name="skill_hub_stars_idx",
                condition=models.Q(visibility="public", status="active"),
            ),
            models.Index(
                fields=["-last_published_at"],
                name="skill_hub_recent_idx",
                condition=models.Q(visibility="public", status="active"),
            ),
            models.Index(
                fields=["-run_count_30d", "-star_count"],
                name="skill_hub_uses_idx",
                condition=models.Q(visibility="public", status="active"),
            ),
            GinIndex(fields=["search_vector"], name="skill_hub_vector_idx"),
            GinIndex(fields=["hub_tags"], name="skill_hub_tags_idx"),
        ]

    def __str__(self) -> str:
        return f"/{self.slug} ({self.visibility})"


class SkillGrant(models.Model):
    """One chosen reader of a `specific` skill (MIP-2212): a person, or a LIVE link to a mention group,
    a group chat or a project. Exactly one target column is set.

    A group is not expanded when it is picked: who is in it is asked every time the skill is read or
    run (`service.can_read` / `visible_to`), so a person added to the chat gains the skill, and a person
    who leaves it loses the skill, with nothing to sync. A grant never widens WHERE the skill runs: the
    reader must still be a member of the skill's team (not a guest), as for a team skill.
    Deleting the group, chat or project, or the person's account, deletes the grant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, to_field="id", null=True, blank=True, related_name="+"
    )
    mention_group = models.ForeignKey(
        "origin.MentionGroupMaster",
        on_delete=models.CASCADE,
        to_field="group_id",
        null=True,
        blank=True,
        related_name="+",
    )
    channel = models.ForeignKey(
        "origin.Channel", on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    project = models.ForeignKey(
        "origin.ProjectMaster",
        on_delete=models.CASCADE,
        to_field="project_id",
        null=True,
        blank=True,
        related_name="+",
    )
    created_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        user__isnull=False,
                        mention_group__isnull=True,
                        channel__isnull=True,
                        project__isnull=True,
                    )
                    | models.Q(
                        user__isnull=True,
                        mention_group__isnull=False,
                        channel__isnull=True,
                        project__isnull=True,
                    )
                    | models.Q(
                        user__isnull=True,
                        mention_group__isnull=True,
                        channel__isnull=False,
                        project__isnull=True,
                    )
                    | models.Q(
                        user__isnull=True,
                        mention_group__isnull=True,
                        channel__isnull=True,
                        project__isnull=False,
                    )
                ),
                name="skill_grant_one_target",
            ),
            models.UniqueConstraint(
                fields=["skill", "user"],
                condition=models.Q(user__isnull=False),
                name="skill_grant_user_once",
            ),
            models.UniqueConstraint(
                fields=["skill", "mention_group"],
                condition=models.Q(mention_group__isnull=False),
                name="skill_grant_group_once",
            ),
            models.UniqueConstraint(
                fields=["skill", "channel"],
                condition=models.Q(channel__isnull=False),
                name="skill_grant_channel_once",
            ),
            models.UniqueConstraint(
                fields=["skill", "project"],
                condition=models.Q(project__isnull=False),
                name="skill_grant_project_once",
            ),
        ]
        indexes = [
            # "Which skills am I granted": the list and the `/name` lookups read it by target.
            models.Index(
                fields=["user"], name="skill_grant_user_idx", condition=models.Q(user__isnull=False)
            ),
        ]


class SkillTag(models.Model):
    """One entry of a member's skill tag catalog (MIP-2211): a name and a chip colour, picked in the
    skill editor and managed in the same Manage tags dialog as to-do tags. Scoped per owner and
    team like `ToDoCategory`; a skill's tags come from its OWNER's catalog, so a co-editor picks
    from the owner's list. Names keep the skill tag rules (`services/skills/text.py`) because they
    travel in the SKILL.md file and the Hub's filter."""

    tag_id = models.BigAutoField(primary_key=True)
    # CASCADE like `Skill.team`: a NULL team would slip past the unique constraint (NULLs differ).
    team = models.ForeignKey(
        TeamMaster, on_delete=models.CASCADE, to_field="team_id", related_name="skill_tags"
    )
    owner = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, to_field="id", related_name="skill_tags"
    )
    name = models.CharField(max_length=24)
    # Same shape as `ToDoCategory`: a preset hex and the text colour that reads on it.
    color = models.CharField(max_length=10, default="#808080")
    text_color = models.CharField(max_length=10, default="white")
    sort_order = models.IntegerField(default=0)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["team", "owner", "name"], name="uniq_skill_tag_per_owner"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.owner_id})"


class SkillVersion(models.Model):
    """An immutable snapshot of a skill, cut when the owner publishes or first shares the
    skill (saving an edit does not cut one: the edit is the owner's draft). The row never changes
    once written."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="versions")
    # Monotonic per skill, starts at 1.
    version_no = models.PositiveIntegerField()
    body = models.JSONField(default=list, blank=True)
    body_markdown = models.TextField(blank=True, default="")
    content_hash = models.CharField(max_length=64)
    changelog = models.CharField(max_length=280, blank=True, default="")
    # The metadata as it was when the version was cut: what a reader sees must not move either.
    slug = models.CharField(max_length=40, blank=True, default="")
    title = models.CharField(max_length=120)
    description = models.CharField(max_length=280, blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    icon = models.CharField(max_length=16, blank=True, default="")
    tool_level = models.CharField(max_length=8, choices=TOOL_LEVEL_CHOICES)
    argument_hint = models.CharField(max_length=120, blank=True, default="")
    effort = models.CharField(max_length=8, choices=EFFORT_CHOICES, blank=True, default="")
    language = models.CharField(max_length=16, blank=True, default="")
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # What the publish-time scan found (the Safety task, MIP-2044, fills it in).
    scan_result = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["skill", "version_no"], name="skill_version_no_unique")
        ]
        ordering = ["-version_no"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("A published skill version is immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("A published skill version is immutable.")


class SkillInstall(models.Model):
    """How a public or official skill joins "my skills" without copying it: pinned to one version,
    so the author cannot swap the text after the fact."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, to_field="id", related_name="skill_installs"
    )
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="installs")
    version_no = models.PositiveIntegerField()
    auto_update = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "skill"], name="skill_install_per_user")
        ]


class SkillStar(models.Model):
    """One star per account; it also bookmarks the skill (the Starred tab)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, to_field="id", related_name="skill_stars"
    )
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="stars")
    # Only a star from an account at least 3 days old with a verified email, and not the author's
    # own, moves `Skill.star_count` (design section 8). Any other account may still star: it is
    # their bookmark (the Starred tab), it just does not count toward the public total.
    counted = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "skill"], name="skill_star_per_user")
        ]
        # "How many stars did this account give today": the daily limit reads it.
        indexes = [models.Index(fields=["user", "-created_at"], name="skill_star_user_day_idx")]


class SkillRunLog(models.Model):
    """One run of a skill by one account (the Hub's "uses", MIP-2047). `run_count_30d` is the
    number of DISTINCT accounts that ran the skill in 30 days; the nightly `skills_refresh_hub`
    command recomputes it and drops rows older than 90 days. An author only ever sees that total,
    never who ran it: nothing that is not needed is kept (counsel's principle, 2026-11-21)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="run_logs")
    user = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, to_field="id", related_name="skill_run_logs"
    )
    version_no = models.PositiveIntegerField(default=0)
    surface = models.CharField(max_length=16, default="ask")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["skill", "user", "-created_at"], name="skill_run_skill_idx")
        ]


class PublicProfile(models.Model):
    """The public identity of an author in the Hub (design K8): a handle chosen when they first make
    a skill public, and an optional display name. The email and the team name are never shown."""

    user = models.OneToOneField(
        CustomUser,
        on_delete=models.CASCADE,
        to_field="id",
        primary_key=True,
        related_name="skill_profile",
    )
    # Lower case and unique (rules in `services/skills/hub.py`).
    handle = models.CharField(max_length=30, unique=True)
    display_name = models.CharField(max_length=60, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)


REPORT_HARMFUL = "harmful"
REPORT_SPAM = "spam"
REPORT_COPYRIGHT = "copyright"
REPORT_PERSONAL_DATA = "personal_data"
REPORT_OFFENSIVE = "offensive"
REPORT_OTHER = "other"
REPORT_REASON_CHOICES = [
    (REPORT_HARMFUL, "harmful or tries to misuse my data"),
    (REPORT_SPAM, "spam"),
    (REPORT_COPYRIGHT, "copyright"),
    (REPORT_PERSONAL_DATA, "personal data"),
    (REPORT_OFFENSIVE, "offensive"),
    (REPORT_OTHER, "other"),
]

REPORT_OPEN = "open"
REPORT_UPHELD = "upheld"
REPORT_DISMISSED = "dismissed"
REPORT_STATUS_CHOICES = [
    (REPORT_OPEN, "waiting for an operator"),
    (REPORT_UPHELD, "an operator agreed: the skill was taken down"),
    (REPORT_DISMISSED, "an operator looked and left the skill up"),
]


class SkillReport(models.Model):
    """A signed-in member's report of a skill they can read (MIP-2044). Three open reports from
    different accounts within 7 days hide the skill until an operator decides; the operator queue is
    the `skills_moderate` command. The reporter's account is kept only as a link: deleting it leaves the report
    (the skill's moderation record) without a name."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    skill = models.ForeignKey(Skill, on_delete=models.CASCADE, related_name="reports")
    # The version the reporter was reading, so a decision is about the text they saw.
    version_no = models.PositiveIntegerField(default=0)
    reporter = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="skill_reports"
    )
    reason = models.CharField(max_length=16, choices=REPORT_REASON_CHOICES)
    text = models.CharField(max_length=1000, blank=True, default="")
    status = models.CharField(max_length=10, choices=REPORT_STATUS_CHOICES, default=REPORT_OPEN)
    resolved_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # One open report per account and skill: a second click is the same report.
            models.UniqueConstraint(
                fields=["skill", "reporter"],
                condition=models.Q(status="open"),
                name="skill_one_open_report_per_reporter",
            )
        ]
        indexes = [
            models.Index(fields=["skill", "status", "created_at"], name="skill_report_queue_idx")
        ]
        ordering = ["-created_at"]
