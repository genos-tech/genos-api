"""A skill's name is unique where it is read from (MIP-2210).

Before: one rule, "unique per owner within a team" (`skill_slug_per_owner`), whatever the
visibility. Now two more: a TEAM skill's name is unique in its team, and a PUBLIC skill's name is
unique among all public skills (partial unique indexes).

A database that already holds a clash would refuse the new indexes, so the data step runs first:
among skills that clash, the oldest keeps its name and each later one gets `-2`, `-3`... (the first
suffix that is free for that skill everywhere its name has to be unique). Production held none when
this was written (1 skill, private); the step is the net for other databases. Skill versions are
immutable and keep the name they were cut with.
"""

from django.db import migrations, models

SLUG_MAX = 40


def _free(Skill, skill, candidate):
    others = Skill.objects.exclude(pk=skill.pk).filter(slug=candidate)
    if others.filter(team_id=skill.team_id, owner_id=skill.owner_id).exists():
        return False
    if (
        skill.visibility == "team"
        and others.filter(team_id=skill.team_id, visibility="team").exists()
    ):
        return False
    if skill.visibility == "public" and others.filter(visibility="public").exists():
        return False
    return True


def rename_clashes(apps, schema_editor):
    Skill = apps.get_model("origin", "Skill")
    groups = [
        ("team", ["team_id", "slug"]),
        ("public", ["slug"]),
    ]
    for visibility, keys in groups:
        seen = set()
        rows = Skill.objects.filter(visibility=visibility).order_by("created_at", "id")
        for skill in rows.iterator():
            key = tuple(getattr(skill, k) for k in keys)
            if key not in seen:
                seen.add(key)
                continue
            n = 1
            while True:
                n += 1
                suffix = f"-{n}"
                candidate = f"{skill.slug[: SLUG_MAX - len(suffix)]}{suffix}"
                if _free(Skill, skill, candidate):
                    break
            Skill.objects.filter(pk=skill.pk).update(slug=candidate)
            seen.add(tuple(candidate if k == "slug" else getattr(skill, k) for k in keys))


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0274_skills_safety"),
    ]

    operations = [
        migrations.RunPython(rename_clashes, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="skill",
            constraint=models.UniqueConstraint(
                condition=models.Q(("visibility", "team")),
                fields=("team", "slug"),
                name="skill_slug_unique_in_team",
            ),
        ),
        migrations.AddConstraint(
            model_name="skill",
            constraint=models.UniqueConstraint(
                condition=models.Q(("visibility", "public")),
                fields=("slug",),
                name="skill_slug_unique_public",
            ),
        ),
    ]
