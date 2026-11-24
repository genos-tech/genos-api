"""List one team's object ids in every table, for the red-team walker (MIP-2267).

    RT_TEAM=<team uuid> python manage.py shell < scripts/redteam/inventory.py > team_a.json

Output: {"objects": {"app.Model": {"pk": "task_id", "ids": [...], "fields": {...}}}, "markers": [...]}.
A row belongs to the team when it has a team foreign key / team_id to it, or a foreign key to a row
that does (two hops: project -> task -> comment). `markers` are long distinctive strings of the
team's own text (titles, names, bodies) that the walker searches for in another team's responses.
"""

import hashlib
import json
import os
import uuid

from django.apps import apps
from django.db import models

TEAM = uuid.UUID(os.environ["RT_TEAM"])
from origin.models.common.team_models import TeamMaster  # noqa: E402

PER_MODEL = 8
TEXT_FIELDS = (
    "title",
    "task_title",
    "project_name",
    "name",
    "milestone_name",
    "sprint_name",
    "body_text",
    "note_title",
)

owned: dict = {}  # model -> list of pks


def _team_filter(model):
    for f in model._meta.get_fields():
        if getattr(f, "many_to_one", False) and f.related_model is TeamMaster and f.concrete:
            return {f.name: TEAM}
    for f in model._meta.concrete_fields:
        if f.name == "team_id" and isinstance(f, (models.UUIDField, models.CharField)):
            return {"team_id": TEAM if isinstance(f, models.UUIDField) else str(TEAM)}
    return None


all_models = [m for m in apps.get_models() if m._meta.app_label == "origin" and not m._meta.proxy]
for m in all_models:
    flt = _team_filter(m)
    if flt is None or m is TeamMaster:
        continue
    try:
        owned[m] = list(m.objects.filter(**flt).values_list("pk", flat=True)[:500])
    except Exception:  # noqa: BLE001
        pass

for _hop in range(2):
    for m in all_models:
        if m in owned or m is TeamMaster:
            continue
        for f in m._meta.concrete_fields:
            if (
                getattr(f, "many_to_one", False)
                and f.related_model in owned
                and owned[f.related_model]
            ):
                try:
                    ids = list(
                        m.objects.filter(**{f"{f.name}__in": owned[f.related_model]}).values_list(
                            "pk", flat=True
                        )[:500]
                    )
                except Exception:  # noqa: BLE001
                    continue
                if ids:
                    owned[m] = ids
                    break

out = {"team_id": str(TEAM), "objects": {}, "markers": []}
markers = set()
for m, ids in owned.items():
    if not ids:
        continue
    pk = m._meta.pk
    out["objects"][m._meta.label] = {
        "pk": pk.name,
        "pk_type": "uuid"
        if isinstance(pk, models.UUIDField)
        else (
            "int"
            if isinstance(pk, (models.AutoField, models.BigAutoField, models.IntegerField))
            else "str"
        ),
        "ids": [str(i) for i in ids[:PER_MODEL]],
        "count": len(ids),
        # A digest of every column of every row, so a later inventory shows whether another team
        # changed anything here (the write phase of the walk).
        "digest": hashlib.sha256(
            repr(sorted(map(repr, m.objects.filter(pk__in=ids).values_list()))).encode()
        ).hexdigest()[:16],
    }
    names = [f.name for f in m._meta.concrete_fields if f.name in TEXT_FIELDS]
    if names:
        for row in m.objects.filter(pk__in=ids[:50]).values(*names):
            for v in row.values():
                if isinstance(v, str) and len(v.strip()) >= 18:
                    markers.add(v.strip()[:60])
out["markers"] = sorted(markers)[:400]
_team = TeamMaster.objects.get(team_id=TEAM)
out["team_markers"] = [v for v in (_team.team_name, _team.team_email) if v]
print(json.dumps(out))
