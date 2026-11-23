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

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

# Only the models are exported by `from .skills.skill_models import *`: the constants below are
# short generic names (VISIBILITY_*, STATUS_*) that would shadow other models'.
__all__ = ["Skill", "SkillInstall", "SkillStar", "SkillVersion"]

VISIBILITY_PRIVATE = "private"
VISIBILITY_TEAM = "team"
VISIBILITY_PUBLIC = "public"
VISIBILITY_CHOICES = [
    (VISIBILITY_PRIVATE, "private: the owner only"),
    (VISIBILITY_TEAM, "team: every member of the owner's team, not guests"),
    (VISIBILITY_PUBLIC, "public: any signed-in Genos user"),
]

STATUS_ACTIVE = "active"
STATUS_HIDDEN_BY_TEAM = "hidden_by_team"
STATUS_REPORTED_HIDDEN = "reported_hidden"
STATUS_TAKEN_DOWN = "taken_down"
STATUS_CHOICES = [
    (STATUS_ACTIVE, "active"),
    (STATUS_HIDDEN_BY_TEAM, "hidden from the team by the team owner"),
    (STATUS_REPORTED_HIDDEN, "hidden after reports, waiting for an operator"),
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
    # Up to 5 lower-case tags. A list, not a vocabulary table: the shared vocabulary and the
    # directory filters belong to the Hub (MIP-2047).
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

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["team", "owner", "slug"], name="skill_slug_per_owner")
        ]
        indexes = [
            models.Index(fields=["team", "visibility"], name="skill_team_visibility_idx"),
            models.Index(fields=["visibility", "-star_count"], name="skill_visibility_stars_idx"),
        ]

    def __str__(self) -> str:
        return f"/{self.slug} ({self.visibility})"


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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "skill"], name="skill_star_per_user")
        ]
