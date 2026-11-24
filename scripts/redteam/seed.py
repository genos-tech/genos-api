"""Seed the local red-team target with three tenants (MIP-2267).

Run ONLY inside the local red-team container (DATABASE_URL on 127.0.0.1, database origin_rt*):

    python manage.py shell < scripts/redteam/seed.py > manifest.json

  team A   the victim. Full sample workspace in English, owner + a viewer member.
  team B   the attacker. Full sample workspace in Japanese (so any English sample text that shows up
           in a team-B response is team A's), owner + a viewer member.
  team C   the control: an owner and an empty team.

Passwords are random and only written to the manifest on stdout (the caller keeps it in a 0600
temp file, never in the repo). The workspaces are the demo seeder's, re-labelled as ordinary
(non-demo) teams so no demo-only path changes what is tested.
"""

import json
import os
import secrets
import sys
from urllib.parse import urlparse

from django.conf import settings
from django.db import connection

# ---- refuse anything that is not the local throwaway database --------------------------------
_db = settings.DATABASES["default"]
if _db.get("HOST") not in ("127.0.0.1", "localhost", "pg", "pg-test") or not str(
    _db.get("NAME", "")
).startswith("origin_rt"):
    sys.stderr.write(
        "seed.py: refusing: not the local red-team database (origin_rt* on localhost)\n"
    )
    raise SystemExit(2)
for _host in [h for h in settings.ALLOWED_HOSTS if h]:
    if _host not in (
        "127.0.0.1",
        "localhost",
        "healthcheck.railway.app",
    ):  # settings always adds it
        sys.stderr.write(f"seed.py: refusing: ALLOWED_HOSTS has a non-local host {_host!r}\n")
        raise SystemExit(2)
_backend = urlparse(os.environ.get("BACKEND_BASE_URL", "http://127.0.0.1")).hostname
if _backend not in ("127.0.0.1", "localhost"):
    sys.stderr.write("seed.py: refusing: BACKEND_BASE_URL is not local\n")
    raise SystemExit(2)

from django.contrib.auth import get_user_model  # noqa: E402

from origin.models.common.team_models import TeamMaster, TeamMembers  # noqa: E402
from origin.services import demo_seeder  # noqa: E402
from origin.services.member_roles import VIEWER  # noqa: E402

User = get_user_model()
RUN = secrets.token_hex(3)


def _user(name):
    password = secrets.token_urlsafe(18) + "!aA1"
    user = User.objects.create_user(
        username=f"{name}-{RUN}", email=f"{name}-{RUN}@redteam.invalid", password=password
    )
    if hasattr(user, "is_email_verified"):
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified"])
    return user, password


def _tenant(label, lang):
    owner, owner_pw = _user(f"rt{label}owner")
    info = demo_seeder.create_demo_environment(
        owner, short=f"rt{label.lower()}{RUN}", lang=lang, version="v2"
    )
    team = TeamMaster.objects.get(team_id=info["team_id"])
    team.is_demo = False
    team.save(update_fields=["is_demo"])
    viewer, viewer_pw = _user(f"rt{label}viewer")
    TeamMembers.objects.create(team=team, attendee=viewer, member_role=VIEWER)
    return {
        "team_id": str(team.team_id),
        "team_name": team.team_name,
        "owner": {"id": str(owner.id), "email": owner.email, "password": owner_pw},
        "viewer": {"id": str(viewer.id), "email": viewer.email, "password": viewer_pw},
    }


manifest = {"run": RUN, "db": connection.settings_dict["NAME"]}
manifest["A"] = _tenant("A", "en")
manifest["B"] = _tenant("B", "ja")
c_owner, c_pw = _user("rtCowner")
team_c = TeamMaster.objects.create(
    team_name=f"RT Control {RUN}", team_email=f"c-{RUN}@redteam.invalid", owner=c_owner
)
TeamMembers.objects.create(team=team_c, attendee=c_owner)
manifest["C"] = {
    "team_id": str(team_c.team_id),
    "team_name": team_c.team_name,
    "owner": {"id": str(c_owner.id), "email": c_owner.email, "password": c_pw},
}
print(json.dumps(manifest))
