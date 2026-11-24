"""MIP-2253: the persisted one-line step summary of `get_current_user` holds no email.

The summary is stored on `AgentStep.summary` and sent to the client. The result keeps the email for
the model. The change is still model-visible: the goldens (`agent_pins.json`,
`agent_fingerprint.json`) were re-recorded because the raw tool result is 26 bytes shorter.
"""

from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext

from .test_base import BaseAPITestCase


class CurrentUserSummaryTests(BaseAPITestCase):
    def test_summary_names_the_user_but_never_the_email(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        out = REGISTRY["get_current_user"].run({}, ctx)
        self.assertTrue(self.user.email)
        self.assertNotIn(self.user.email, out["__summary__"])
        self.assertNotIn("@", out["__summary__"])
        self.assertIn(self.user.username, out["__summary__"])

    def test_the_result_the_model_sees_is_unchanged(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        out = REGISTRY["get_current_user"].run({}, ctx)
        self.assertEqual(out["email"], self.user.email)
        self.assertEqual(out["username"], self.user.username)
