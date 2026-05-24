"""Deterministic eval fixture.

Reseeds a known team's worth of demo data (via `demo_seeder`) under
fixed UUIDs so retrieval / agent cases can assert against stable
content (titles, statuses, project membership) without depending on a
hand-managed dev DB.

Usage:

    python manage.py agent_eval_setup            # ensure-or-reseed
    python manage.py agent_eval_setup --reseed   # tear down + reseed
    python manage.py agent_eval --retrieval      # then run the suite

Notes
-----
* Fixture UUIDs are all zero-prefixed; they cannot collide with prod
  users / teams (which are random uuid4).
* The seeded team carries `is_demo=True`, so the existing
  `delete_demo_team_data` helper cleans it up if you ever need to.
* Eval cases reference entities by **title substring**, not by
  numeric id — see `runner.py::must_contain_title_in_top_n`. This
  keeps the YAML stable across reseedings (where auto-incremented ids
  shift).
"""

from __future__ import annotations

import logging
import uuid

from django.core.management import call_command
from django.db import transaction

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.services.demo_seeder import (
    create_demo_environment,
    delete_demo_team_data,
)

log = logging.getLogger(__name__)

# Fixed UUIDs used by the eval fixture. Zero-prefixed so they're
# visually distinct from prod uuid4 values and cannot collide.
FIXTURE_USER_ID = uuid.UUID("00000000-0000-4000-8000-00000000ee01")
FIXTURE_USER_EMAIL = "eval-fixture@genos.app"
FIXTURE_USER_USERNAME = "Eval Fixture User"

# Seeder slug that becomes part of project / bot / team display names.
# Keeping it short and constant means re-seedings produce the same
# strings (so cases that match on titles keep matching).
FIXTURE_SHORT = "evalfixt"


def ensure_fixture() -> dict:
    """Create the fixture if it doesn't already exist; return its summary.

    Idempotent: a second call returns the existing team's summary
    without reseeding. Use `reseed_fixture()` to force fresh content.

    Returns:
        {"team_id": str, "user_id": str, "reseeded": bool}
    """
    existing_user = CustomUser.objects.filter(id=FIXTURE_USER_ID).first()
    if existing_user is not None:
        # Find the team they own — the seeder always creates exactly one
        # demo team per call.
        team = TeamMaster.objects.filter(owner=existing_user, is_demo=True).first()
        if team is not None:
            return {
                "team_id": str(team.team_id),
                "user_id": str(FIXTURE_USER_ID),
                "reseeded": False,
            }

    return reseed_fixture()


def reseed_fixture() -> dict:
    """Tear down any existing fixture team and reseed from scratch.

    Use when:
      * the seeder content changed (case assertions need fresh ids)
      * you want a known-clean state for a baseline measurement
      * eval failures point to drifted index data

    The teardown deletes the team's data AND the indexed OpenSearch
    chunks. The reseed runs the full `create_demo_environment` then
    invokes `opensearch_reindex` synchronously so the index is hot by
    the time this returns.
    """
    with transaction.atomic():
        # 1. Clean slate: remove any existing fixture team's data, the
        # fixture user, and the seeded bot peers. The seeder will
        # recreate all three.
        existing_user = CustomUser.objects.filter(id=FIXTURE_USER_ID).first()
        if existing_user is not None:
            for team in TeamMaster.objects.filter(owner=existing_user, is_demo=True):
                delete_demo_team_data(team.team_id)
            existing_user.delete()
        # `delete_demo_team_data` doesn't touch the bot users (they're
        # standalone CustomUser rows). Their emails are deterministic
        # — `demo-bot-{short}-<role>@genos.app` — so we can clean them
        # up by pattern when the fixture slug is fixed.
        CustomUser.objects.filter(email__startswith=f"demo-bot-{FIXTURE_SHORT}-").delete()

        # 2. Recreate the fixture user with a known UUID so cases that
        # need the requesting user can hard-code it. `id` is the PK
        # and isn't assignable after-the-fact, so pass it through the
        # manager's **extra_fields path.
        demo_user = CustomUser.objects.create_user(
            email=FIXTURE_USER_EMAIL,
            username=FIXTURE_USER_USERNAME,
            password=uuid.uuid4().hex,
            id=FIXTURE_USER_ID,
            is_demo=True,
        )

        # 3. Run the seeder with a fixed slug so generated team / bot /
        # project names are byte-identical across reseedings.
        summary = create_demo_environment(demo_user, short=FIXTURE_SHORT)

    # 4. Reindex synchronously so the freshly-written rows are
    # searchable when the eval starts. `since_minutes=60` is generous
    # — the seeder just wrote everything, so timestamps are < 1 minute
    # old, but we pad against clock skew between worker + DB.
    call_command("opensearch_reindex", since_minutes=60)

    return {
        "team_id": summary["team_id"],
        "user_id": str(FIXTURE_USER_ID),
        "reseeded": True,
    }
