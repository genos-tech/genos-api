"""MIP-2589: from this release only a member's own skills and the ones they installed can be picked
or run. Before the rule applies, give installs for what already worked without one:

1. KEPT FOR CONTINUITY: a member who has a schedule using, or has run, an official skill or a team
   skill they did not write gets it installed, so no schedule starts failing.
2. DEFAULTS: every existing account gets the default skills.

A skill that is public but not official always needed an install, so a missing row there means the
member removed it: it is not brought back. Rows made here do not move the Hub's install count
(`counted=False`). Adds rows only (`ignore_conflicts`), so running it again changes nothing.

The names below are a copy of `services/skills/defaults.py`: a migration must not change when the
code does.
"""

import uuid

from django.db import migrations

DEFAULT_SKILL_SLUGS = (
    "unread-summary",
    "comments-waiting-on-me",
    "triage",
    "wrapup",
    "weekly-review",
)
BATCH = 500


def _uuid(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError):
        return None


def _used_pairs(apps):
    """`(user id, skill id)` for every skill a schedule runs or a member has run."""
    pairs = set()

    def add(user_id, skill_id):
        user_id, skill_id = _uuid(user_id), _uuid(skill_id)
        if user_id and skill_id:
            pairs.add((user_id, skill_id))

    MemberSchedule = apps.get_model("origin", "MemberSchedule")
    schedules = MemberSchedule.objects.filter(job__in=("run_skill", "ai_dashboard"))
    for user_id, params in schedules.values_list("user_id", "params").iterator():
        if isinstance(params, dict):
            add(user_id, params.get("skill_id"))
    AgentRun = apps.get_model("search_engine", "AgentRun")
    runs = AgentRun.objects.filter(skill_ref__isnull=False)
    for user_id, ref in runs.values_list("user_id", "skill_ref").iterator():
        if isinstance(ref, dict):
            add(user_id, ref.get("id"))
    SkillRunLog = apps.get_model("origin", "SkillRunLog")
    for user_id, skill_id in SkillRunLog.objects.values_list("user_id", "skill_id").distinct():
        add(user_id, skill_id)
    return pairs


def _install(SkillInstall, rows):
    SkillInstall.objects.bulk_create(rows, ignore_conflicts=True, batch_size=BATCH)


def keep_used_skills(apps, schema_editor):
    Skill = apps.get_model("origin", "Skill")
    SkillInstall = apps.get_model("origin", "SkillInstall")
    CustomUser = apps.get_model("origin", "CustomUser")
    TeamMembers = apps.get_model("origin", "TeamMembers")
    TeamMaster = apps.get_model("origin", "TeamMaster")
    pairs = _used_pairs(apps)
    if not pairs:
        return
    skills = {
        str(s["id"]): s
        for s in Skill.objects.filter(
            pk__in={skill for _, skill in pairs}, status="active", published_version_no__gt=0
        ).values("id", "owner_id", "team_id", "visibility", "is_official", "published_version_no")
    }
    users = {
        str(pk)
        for pk in CustomUser.objects.filter(
            pk__in={user for user, _ in pairs}, is_system_user=False, is_deleted=False
        ).values_list("pk", flat=True)
    }
    teams = {s["team_id"] for s in skills.values() if s["visibility"] in ("team", "specific")}
    members = {
        (str(team), str(user))
        for team, user in TeamMembers.objects.filter(
            team_id__in=teams, is_deleted=False
        ).values_list("team_id", "attendee_id")
    } | {
        (str(team), str(user))
        for team, user in TeamMaster.objects.filter(team_id__in=teams).values_list(
            "team_id", "owner_id"
        )
    }
    rows = []
    for user_id, skill_id in sorted(pairs):
        skill = skills.get(skill_id)
        if skill is None or user_id not in users or str(skill["owner_id"]) == user_id:
            continue
        if skill["visibility"] == "public":
            if not skill["is_official"]:
                continue
        elif skill["visibility"] not in ("team", "specific"):
            continue
        elif (str(skill["team_id"]), user_id) not in members:
            continue
        rows.append(
            SkillInstall(
                user_id=user_id,
                skill_id=skill["id"],
                version_no=skill["published_version_no"],
                counted=False,
            )
        )
    _install(SkillInstall, rows)


def install_defaults(apps, schema_editor):
    Skill = apps.get_model("origin", "Skill")
    SkillInstall = apps.get_model("origin", "SkillInstall")
    CustomUser = apps.get_model("origin", "CustomUser")
    defaults = list(
        Skill.objects.filter(
            slug__in=DEFAULT_SKILL_SLUGS,
            is_official=True,
            visibility="public",
            status="active",
            published_version_no__gt=0,
        ).values_list("id", "published_version_no")
    )
    if not defaults:
        return  # the official sync has not run: it installs them when it creates them
    users = CustomUser.objects.filter(is_system_user=False, is_deleted=False)
    rows = []
    for user_id in users.values_list("pk", flat=True).iterator(chunk_size=BATCH):
        rows += [
            SkillInstall(user_id=user_id, skill_id=skill_id, version_no=version_no, counted=False)
            for skill_id, version_no in defaults
        ]
        if len(rows) >= BATCH:
            _install(SkillInstall, rows)
            rows = []
    _install(SkillInstall, rows)


def forwards(apps, schema_editor):
    # Continuity first: for a skill that is also a default, the row is the same either way.
    keep_used_skills(apps, schema_editor)
    install_defaults(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0306_skill_install_counted"),
        ("search_engine", "0038_agentrun_kept_sources"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
