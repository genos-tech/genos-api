"""The skill tag catalog (MIP-2211): `SkillTag`, and the free-text tags that already exist moved
into it.

Before: a skill's tags were free text in `Skill.tags`. Now each name must be in its owner's catalog
(a name, a chip colour), picked in the editor and managed in Manage tags. The data step gives every
(team, owner, name) that a skill already carries a catalog row, so no existing tag is lost and every
existing skill keeps passing the editor's check. The colour is a palette colour chosen from the name
(`zlib.crc32`, not `hash()`, which changes between runs), so the same name is the same colour in every
database. `Skill.tags` is not touched: it stays the list of names, and so do published versions and
the Hub's columns. Names are already valid slugs (`clean_tags` enforced that), so the rows fit
`name` (24). Reverse: nothing to undo for the data (the table goes with the schema step).
"""

import zlib

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

PALETTE = (
    "#ff2323",
    "#1dc200",
    "#0044c2",
    "#ffd93b",
    "#ffa823",
    "#8e23ff",
    "#ff238a",
    "#808080",
    "#008080",
    "#4b0082",
    "#8b4513",
    "#2e8b57",
    "#4682b4",
    "#6a5acd",
)


def _text_color(color):
    h = color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return "black" if (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.6 else "white"


def backfill_catalog(apps, schema_editor):
    Skill = apps.get_model("origin", "Skill")
    SkillTag = apps.get_model("origin", "SkillTag")
    # (team, owner) -> names in the order a skill first used them
    wanted = {}
    rows = (
        Skill.objects.exclude(tags=[])
        .order_by("created_at")
        .values_list("team_id", "owner_id", "tags")
    )
    for team_id, owner_id, tags in rows.iterator(chunk_size=500):
        names = wanted.setdefault((team_id, owner_id), [])
        for name in tags or []:
            name = str(name or "").strip().lower()[:24]
            if name and name not in names:
                names.append(name)
    for (team_id, owner_id), names in wanted.items():
        for order, name in enumerate(names, start=1):
            color = PALETTE[zlib.crc32(name.encode("utf-8")) % len(PALETTE)]
            SkillTag.objects.get_or_create(
                team_id=team_id,
                owner_id=owner_id,
                name=name,
                defaults={"color": color, "text_color": _text_color(color), "sort_order": order},
            )


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0277_html_notes"),
    ]

    operations = [
        migrations.CreateModel(
            name="SkillTag",
            fields=[
                ("tag_id", models.BigAutoField(primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=24)),
                ("color", models.CharField(default="#808080", max_length=10)),
                ("text_color", models.CharField(default="white", max_length=10)),
                ("sort_order", models.IntegerField(default=0)),
                ("ts_created_at", models.DateTimeField(auto_now_add=True)),
                ("ts_updated_at", models.DateTimeField(auto_now=True)),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="skill_tags",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="skill_tags",
                        to="origin.teammaster",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("team", "owner", "name"), name="uniq_skill_tag_per_owner"
                    )
                ],
            },
        ),
        migrations.RunPython(backfill_catalog, migrations.RunPython.noop),
    ]
