"""NDJSON event-vocabulary contract for the agent stream emitters.

Binds the event `type` literals emitted by `agent/controller.py` and
`agent_views.py` to the canonical vocabulary in
`origin/search_engine/agent/events.py` (which is mirrored, with a
KEEP-IN-SYNC header, by genos-frontend's
`src/services/agentEventNames.ts` — see genos-docs
`spotlight/SPOTLIGHT_AGENT_CHANGE_SAFETY.md` §4.3).

Why a *source scan* instead of refactoring the emit sites to import
constants: the controller is a ~2000-line hot file with ~30 emit sites;
scanning gives the same guarantee (a new/renamed event can't ship
unlisted) with zero churn and zero runtime risk. If the emit style is
ever refactored (constants, an emit helper), update EVENT_LITERAL_RE —
`test_scan_still_sees_the_emitters` fails loudly rather than letting the
contract rot into a vacuous pass.

No DB, no LLM, no network — safe to hard-gate.
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

import origin.search_engine as search_engine_pkg
import origin.search_engine.agent as agent_pkg
from origin.search_engine.agent.events import AGENT_EVENT_TYPES, AGENT_TERMINAL_EVENT_TYPES

# Every module that writes NDJSON events onto the /ask/ + /decide/ stream.
EMITTER_FILES = (
    Path(agent_pkg.__file__).parent / "controller.py",
    Path(search_engine_pkg.__file__).parent / "agent_views.py",
)

# Matches the house emit idiom: a dict literal with `"type": "<event>"`.
# Lowercase-only on purpose — tool parameter schemas use Gemini's
# UPPERCASE type names ("OBJECT", "STRING"), so they can't collide.
EVENT_LITERAL_RE = re.compile(r'"type": "([a-z_]+)"')


def _emitted_types(path: Path) -> set[str]:
    return set(EVENT_LITERAL_RE.findall(path.read_text(encoding="utf-8")))


class AgentEventContractTests(SimpleTestCase):
    def test_emitted_events_match_the_canonical_vocabulary(self):
        """Set equality, both directions: an emitted-but-unlisted event is
        a new event the frontend doesn't know about (add it to events.py
        AND the frontend mirror + its handler); a listed-but-unemitted
        event is stale vocabulary (remove it from both mirrors)."""
        emitted: set[str] = set()
        for path in EMITTER_FILES:
            emitted |= _emitted_types(path)
        unlisted = emitted - AGENT_EVENT_TYPES
        stale = AGENT_EVENT_TYPES - emitted
        self.assertFalse(
            unlisted,
            f"events emitted but missing from AGENT_EVENT_TYPES: {sorted(unlisted)} "
            "— update events.py AND genos-frontend src/services/agentEventNames.ts "
            "(+ a dispatchLine handler) together",
        )
        self.assertFalse(
            stale,
            f"AGENT_EVENT_TYPES lists events no emitter produces: {sorted(stale)} "
            "— remove from events.py AND the genos-frontend mirror",
        )

    def test_scan_still_sees_the_emitters(self):
        """Guard the guard: if the emit idiom changes (constants, a helper)
        the regex stops matching and the equality test above would pass
        vacuously. Each emitter must still yield a healthy match count."""
        for path in EMITTER_FILES:
            with self.subTest(file=path.name):
                found = _emitted_types(path)
                self.assertGreaterEqual(
                    len(found),
                    5,
                    f"{path.name}: only {sorted(found)} matched — emit style "
                    "changed? Update EVENT_LITERAL_RE in this test.",
                )

    def test_terminal_events_are_a_subset(self):
        self.assertLessEqual(AGENT_TERMINAL_EVENT_TYPES, AGENT_EVENT_TYPES)
