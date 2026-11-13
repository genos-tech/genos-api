"""The sequence scenario as an eval fixture, in its OWN teams (MIP-1269 -> MIP-1271 test C).

`origin/tests/fixtures/sequence_scenario.py` builds Ken's 12-step story (a client asks in a
chat, the PM opens a task, a developer works and closes it, ...) through the real REST and MCP
funnels, but as a test case base. This module runs the same builder OUTSIDE the test runner,
into fixed-id teams, so an eval case can ask the in-app agent about it.

TEAMS (never the v1 eval team: that corpus is ranking-sensitive, see AGENT_BRIEF.md)

  v1   English story. The client A is a member of the project.                 viewers: v1, b, c
  v2   English story. The client A is only in the GM chat, on no project, and
       B tells A in the task thread (`announce`). A V2 viewer must not see
       the task, its comments or its note.                                       viewers: v2
  ja   The same story with Japanese chat and comments, chat shape "threaded".   viewers: ja, ja-b

A viewer is `(team, person)`; `viewer_info("v2")` gives the ids an eval case runs as.

WHAT IT NEEDS: Postgres only. Every row is written by a REST or MCP handler, none of which
embeds, indexes or calls a model (indexing is the reindex cron's job), so there is no
OpenSearch and no network dependence. A retrieval case WOULD need `opensearch_reindex`; none
of the sequence cases is one.

SAFETY. The test base class's own `setUp` deletes every task and creates test users; it is
never called here. The driver below builds fixed-id rows and checks for them first, so the
seeding is idempotent and cannot touch other data. The MCP tier grant and the test host are
applied with `override_settings` for the duration of the build only.

TIME. The story's day is pinned (`SCENARIO_DAY`), not "three days ago", so the stored instants
do not depend on when the fixture was first seeded. A case must read instants from the data,
never recompute them.
"""

from __future__ import annotations

import importlib
import logging
import uuid
from datetime import date, datetime
from typing import Any

from django.db import transaction
from django.test import override_settings

log = logging.getLogger(__name__)

#: The story's day (JST). Pinned for determinism.
SCENARIO_DAY = date(2026, 11, 2)
TEST_HOST = "evalseq.local"

# Fixed ids, zero-prefixed like the v1 fixture's, in a range of their own (...05e...).
_TEAM_IDS = {
    "v1": uuid.UUID("00000000-0000-4000-8000-0000005eaa01"),
    "v2": uuid.UUID("00000000-0000-4000-8000-0000005eaa02"),
    "ja": uuid.UUID("00000000-0000-4000-8000-0000005eaa03"),
}
_ROLES = {"a": 1, "b": 2, "c": 3}
_TEAM_INDEX = {"v1": 1, "v2": 2, "ja": 3}

NAMES = {"a": "Alice Client", "b": "Ben Manager", "c": "Chris Dev"}

JA_TEXTS: dict[Any, str] = {
    1: "Xが壊れています。エクスポートボタンが反応しません",
    2: "Xを確認しています。新しいエクスポートのコードが原因のようです",
    3: "Xが原因で失敗しているエクスポート(Y)の対応をお願いできますか?",
    "3b": "対応します。Yのタスクを作成します",
    7: "進捗: Yを再現しました。原因は権限の不足です",
    8: "ありがとうございます。CSVエクスポートも確認してください",
    10: "修正してリリースしました。Yは解決済みです",
    "12a": "Yは解決しました。タスクはクローズしました",
}

# label -> how its story is built
_BUILDS: dict[str, dict[str, Any]] = {
    "v1": {"client_on_project": True, "chat_shape": "flat", "announce": False, "texts": None},
    "v2": {"client_on_project": False, "chat_shape": "flat", "announce": True, "texts": None},
    "ja": {
        "client_on_project": True,
        "chat_shape": "threaded",
        "announce": False,
        "texts": JA_TEXTS,
    },
}

#: viewer key -> (team label, person)
VIEWERS: dict[str, tuple[str, str]] = {
    "v1": ("v1", "a"),
    "b": ("v1", "b"),
    "c": ("v1", "c"),
    "v2": ("v2", "a"),
    "ja": ("ja", "a"),
    "ja-b": ("ja", "b"),
}


def team_id(label: str) -> uuid.UUID:
    return _TEAM_IDS[label]


def user_id(label: str, person: str) -> uuid.UUID:
    """`00000000-0000-4000-8000-0000005e<team><role>`: stable across machines."""
    return uuid.UUID(
        f"00000000-0000-4000-8000-0000005e{_TEAM_INDEX[label]:02x}{_ROLES[person]:02x}"
    )


def _email(label: str, person: str) -> str:
    return f"evalseq-{label}-{person}@genos.app"


def exists(label: str) -> bool:
    from origin.models.common.team_models import TeamMaster  # noqa: PLC0415

    return TeamMaster.objects.filter(team_id=_TEAM_IDS[label]).exists()


def ensure_sequence(*, reseed: bool = False) -> dict[str, Any]:
    """Seed every missing sequence team (all, when `reseed`). Idempotent; returns `summary()`."""
    built = []
    if reseed:
        for label in _TEAM_IDS:
            delete_team(label)
    for label in _TEAM_IDS:
        if not exists(label):
            _build(label)
            built.append(label)
    out = summary()
    out["built"] = built
    return out


def delete_team(label: str) -> None:
    """Remove one scenario team and its people. Touches only the fixed ids above."""
    from origin.models.common.team_models import TeamMaster  # noqa: PLC0415
    from origin.models.common.user_models import CustomUser  # noqa: PLC0415
    from origin.services.demo_seeder import delete_demo_team_data  # noqa: PLC0415

    if TeamMaster.objects.filter(team_id=_TEAM_IDS[label]).exists():
        delete_demo_team_data(_TEAM_IDS[label])
        TeamMaster.objects.filter(team_id=_TEAM_IDS[label]).delete()
    CustomUser.objects.filter(id__in=[user_id(label, p) for p in _ROLES]).delete()


def summary() -> dict[str, Any]:
    """Ids of every seeded team and viewer (only those that exist)."""
    out: dict[str, Any] = {"teams": {}, "viewers": {}}
    for label in _TEAM_IDS:
        if exists(label):
            out["teams"][label] = str(_TEAM_IDS[label])
    for key, (label, person) in VIEWERS.items():
        if label in out["teams"]:
            out["viewers"][key] = viewer_info(key)
    return out


def viewer_info(viewer: str) -> dict[str, Any]:
    """What an eval case runs as: `team_id`, `user_id`, and the story's task (display id and id).

    KeyError for an unknown viewer; LookupError when that team is not seeded.
    """
    from origin.models.task.task_models import TaskMaster  # noqa: PLC0415

    label, person = VIEWERS[viewer]
    if not exists(label):
        raise LookupError(
            f"sequence team {label!r} is not seeded (agent_eval_setup --scenario sequence)"
        )
    task = TaskMaster.objects.filter(team_id=_TEAM_IDS[label]).order_by("task_id").first()
    return {
        "viewer": viewer,
        "team": label,
        "person": person,
        "team_id": str(_TEAM_IDS[label]),
        "user_id": str(user_id(label, person)),
        "display_id": task.display_id if task else "",
        "task_id": task.task_id if task else 0,
    }


# --------------------------------------------------------------------------- #
# The driver: the test base class's builder, on fixed-id rows                  #
# --------------------------------------------------------------------------- #


# The scenario builder lives in the test tree (`origin/tests` is left out of the production image,
# see .dockerignore and test_dockerignore). Seeding is a DEV-ONLY tool that runs where the tests
# are (the dev stack bind-mounts the repo), so the modules are loaded by name at call time rather
# than imported by the app, and a missing tree is reported instead of crashing an import.
_TESTS_PKG = "origin" + ".tests"


def _load_test_module(name: str):
    try:
        return importlib.import_module(f"{_TESTS_PKG}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith(_TESTS_PKG):
            raise RuntimeError(
                "the sequence scenario is built by origin/tests, which this image does not "
                "contain: seed it from the dev stack (a checkout with origin/tests)"
            ) from exc
        raise


def _driver_class():
    SequenceScenario = _load_test_module("fixtures.sequence_scenario").SequenceScenario  # noqa: N806

    class Driver(SequenceScenario):
        """`SequenceScenario.build_sequence` without `setUp`, and with fixed-id people."""

        def __init__(self, label: str):
            super().__init__("runTest")
            self.label = label

        def runTest(self):  # pragma: no cover - never run as a test
            raise RuntimeError("the driver is not a test")

        def prepare(self) -> None:
            from django.contrib.auth import get_user_model  # noqa: PLC0415
            from rest_framework.test import APIClient  # noqa: PLC0415

            from origin.models.common.team_models import TeamMaster, TeamMembers  # noqa: PLC0415
            from origin.search_engine import quota  # noqa: PLC0415

            user_model = get_user_model()
            self.client = APIClient(HTTP_HOST=TEST_HOST)
            self._raw_keys = {}
            people = {}
            for person in _ROLES:
                people[person] = user_model.objects.create_user(
                    email=_email(self.label, person),
                    username=NAMES[person],
                    password=uuid.uuid4().hex,
                    id=user_id(self.label, person),
                    is_demo=True,
                    timezone="Asia/Tokyo",
                )
            self.a, self.b, self.c = people["a"], people["b"], people["c"]
            self.user = self.b  # the PM owns the team (McpBase's `self.user`)
            self.team = TeamMaster.objects.create(
                team_id=_TEAM_IDS[self.label],
                team_name=f"Eval Sequence {self.label.upper()}",
                team_email=f"evalseq-{self.label}@genos.app",
                owner=self.b,
                is_demo=True,
            )
            for member in (self.b, self.a, self.c):
                TeamMembers.objects.create(team=self.team, attendee=member)
            quota.invalidate_effective_tier([str(u.id) for u in people.values()])
            self.base = datetime.combine(SCENARIO_DAY, datetime.min.time(), tzinfo=_jst())

    return Driver


def _jst():
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    return ZoneInfo("Asia/Tokyo")


def _build(label: str) -> None:
    """Build one team's story. All or nothing: a failure leaves no partial team behind."""
    _mcp_enabled_everywhere = _load_test_module("test_mcp_endpoint")._mcp_enabled_everywhere  # noqa: N806

    spec = _BUILDS[label]
    driver = _driver_class()(label)
    # The test runner adds the test host and the MCP tier grant; outside it, we do.
    with override_settings(
        SEARCH_ENGINE=_mcp_enabled_everywhere(),
        ALLOWED_HOSTS=["*"],
        SECURE_SSL_REDIRECT=False,
    ):
        with transaction.atomic():
            # Clear any half-built remains of a previous failed attempt (fixed ids only).
            delete_team(label)
            driver.prepare()
            seq = driver.build_sequence(
                client_on_project=spec["client_on_project"],
                chat_shape=spec["chat_shape"],
                announce=spec["announce"],
                texts=spec["texts"],
            )
    # The MCP steps minted API keys whose raw values were discarded: leave no credential rows.
    from origin.models.common.api_key_models import ApiKey  # noqa: PLC0415

    ApiKey.objects.filter(user_id__in=[user_id(label, p) for p in _ROLES]).delete()
    log.info("sequence team %s built: task %s", label, seq.task.display_id if seq.task else "-")
