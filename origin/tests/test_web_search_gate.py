"""Web-search tool gating is authoritative from the user's persisted
`spotlight_web_search_enabled` preference — NOT a frontend-sent flag.

Regression guard for the bug where the toggle was ON (field True) but the
agent still answered "I don't have a web search tool", because a stale /
racing client sent `allow_web_search: false` and the backend trusted it.
"""

from types import SimpleNamespace

from django.test import SimpleTestCase

from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent_views import _persisted_disabled_tools


class TestWebSearchGate(SimpleTestCase):
    def test_toggle_on_offers_search_web(self):
        user = SimpleNamespace(spotlight_web_search_enabled=True)
        disabled = _persisted_disabled_tools(user)
        self.assertNotIn("search_web", disabled)
        # …and it actually reaches the model's tool list.
        names = {d.name for d in _build_tool_declarations(disabled)}
        self.assertIn("search_web", names)

    def test_toggle_off_hides_search_web(self):
        user = SimpleNamespace(spotlight_web_search_enabled=False)
        disabled = _persisted_disabled_tools(user)
        self.assertIn("search_web", disabled)
        names = {d.name for d in _build_tool_declarations(disabled)}
        self.assertNotIn("search_web", names)

    def test_missing_field_defaults_off(self):
        # A user object without the attribute (defensive) → treated as off.
        self.assertIn("search_web", _persisted_disabled_tools(SimpleNamespace()))

    def test_frontend_flag_is_ignored(self):
        # The old `allow_web_search` request flag no longer participates:
        # gating is a pure function of the persisted preference.
        user = SimpleNamespace(spotlight_web_search_enabled=True)
        # Even if a stale client "would" send allow_web_search=false, the
        # gate never reads request data — only the field.
        self.assertNotIn("search_web", _persisted_disabled_tools(user))
