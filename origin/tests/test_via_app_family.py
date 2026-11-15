"""MIP-1568: a connected app's official family (`claude`, `chatgpt`) rides on the stripped `via`.

Ken wants the official Claude / ChatGPT icon on what those connections did. The web client reads
a coarse `app` from the `via` it is given: `{"kind": "oauth", "app": "claude" | "chatgpt"}`; the
key is absent for every other app and for an API key.

What is trusted, and what is not. A client's NAME is free text its registrant chose (registration
is open), so it never counts. The only signal is the client's registered redirect URIs: the family
is earned only when EVERY registered redirect URI is one of the official callbacks of that one
family, compared exactly. An attacker who registers only the official callbacks cannot receive the
authorization code (it goes to claude.ai / chatgpt.com), so no connection, and no write, exists.

The stamp is written with the write (`McpOAuthCredential.via()`), so history keeps the family of
that moment; old rows have none. `via_for_viewer` keeps the NAME owner-only and lets the family
through for everyone, while `SHOW_OAUTH_APP_TO_EVERYONE` is True; one line flips it to owner-only.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from origin.models.task.task_activity_models import TaskActivity
from origin.services import mcp_client_family
from origin.services.mcp_client_family import app_family
from origin.tests import test_task_activity_via_privacy as feed_tests
from origin.tests import test_task_comment_via as comment_tests
from origin.tests.test_item_lines import LineBase
from origin.tests.test_mcp_oauth_flow import CALLBACK, FlowBase
from origin.views.public.mcp import via_privacy
from origin.views.public.mcp.via_privacy import via_for_viewer

CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_COM_CB = "https://claude.com/api/mcp/auth_callback"
CHATGPT_CB = "https://chatgpt.com/connector_platform_oauth_redirect"
CHATGPT_PER_SERVER_CB = "https://chatgpt.com/connector/oauth/Ab3_x-9"


CLAUDE_DOC = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CHATGPT_DOC = "https://chatgpt.com/oauth/client.json"


def client(*uris, name="Claude", client_id="dcr-3f9a"):
    """An OAuth client as the classifier sees it: a registered name (never read), its client id
    (a random string for DCR, a URL for a metadata document) and its redirects."""
    return SimpleNamespace(name=name, client_id=client_id, redirect_uris=" ".join(uris))


class TheClassifier(unittest.TestCase):
    def test_the_official_callbacks_earn_their_family(self):
        self.assertEqual(app_family(client(CLAUDE_CB)), "claude")
        self.assertEqual(app_family(client(CLAUDE_COM_CB)), "claude")
        self.assertEqual(app_family(client(CLAUDE_CB, CLAUDE_COM_CB)), "claude")
        self.assertEqual(app_family(client(CHATGPT_CB, name="ChatGPT")), "chatgpt")
        self.assertEqual(app_family(client(CHATGPT_PER_SERVER_CB, name="ChatGPT")), "chatgpt")
        self.assertEqual(app_family(client(CHATGPT_CB, CHATGPT_PER_SERVER_CB)), "chatgpt")

    def test_a_lookalike_name_alone_earns_nothing(self):
        for name in ("Claude", "ChatGPT", "Claude (official)", "claude.ai", "OpenAI ChatGPT"):
            self.assertIsNone(app_family(client("https://evil.example/cb", name=name)), name)
            self.assertIsNone(app_family(client("http://localhost:8123/callback", name=name)), name)

    def test_the_name_is_never_read(self):
        # Same redirects, different names: the answer cannot move. A name that says the
        # OTHER family, or nothing at all, does not change it either.
        self.assertEqual(app_family(client(CLAUDE_CB, name="ChatGPT")), "claude")
        self.assertEqual(app_family(client(CHATGPT_CB, name="")), "chatgpt")
        self.assertEqual(app_family(client(CHATGPT_CB, name=None)), "chatgpt")

    def test_a_spoofed_redirect_uri_earns_nothing(self):
        spoofs = [
            "https://claude.ai.evil.example/api/mcp/auth_callback",
            "https://evilclaude.ai/api/mcp/auth_callback",
            "https://claude.ai@evil.example/api/mcp/auth_callback",
            "https://evil.example/?next=https://claude.ai/api/mcp/auth_callback",
            "https://evil.example/https://claude.ai/api/mcp/auth_callback",
            "https://evil.example/api/mcp/auth_callback",
            "http://claude.ai/api/mcp/auth_callback",
            "https://CLAUDE.AI/api/mcp/auth_callback",
            "https://claude.ai:444/api/mcp/auth_callback",
            "https://claude.ai/api/mcp/auth_callback/",
            "https://claude.ai/api/mcp/auth_callback?x=1",
            "https://claude.ai/api/mcp/auth_callback/../evil",
            "https://claude.ai/api/mcp/other",
            "https://www.claude.ai/api/mcp/auth_callback",
            "https://chatgpt.com.evil.example/connector_platform_oauth_redirect",
            "https://chat.openai.com/connector_platform_oauth_redirect",
            "https://platform.openai.com/apps-manage/oauth",
            "https://chatgpt.com/connector_platform_oauth_redirect/",
            "https://chatgpt.com/connector/oauth/",
            "https://chatgpt.com/connector/oauth/a/b",
            "https://chatgpt.com/connector/oauth/abc?x=1",
            "https://chatgpt.com/other",
        ]
        for uri in spoofs:
            self.assertIsNone(app_family(client(uri)), uri)

    def test_one_foreign_redirect_among_official_ones_earns_nothing(self):
        self.assertIsNone(app_family(client(CLAUDE_CB, "https://evil.example/cb")))
        self.assertIsNone(app_family(client("https://evil.example/cb", CHATGPT_CB)))
        self.assertIsNone(app_family(client(CLAUDE_CB, "http://localhost:9/cb")))

    def test_two_families_at_once_earn_nothing(self):
        self.assertIsNone(app_family(client(CLAUDE_CB, CHATGPT_CB)))

    def test_no_redirects_or_a_malformed_client_earn_nothing(self):
        for bad in (client(), client(""), SimpleNamespace(name="Claude", redirect_uris=None)):
            self.assertIsNone(app_family(bad))
        self.assertIsNone(app_family(None))
        self.assertIsNone(app_family(SimpleNamespace(name="Claude")))
        self.assertIsNone(app_family(SimpleNamespace(name="Claude", redirect_uris=["x"])))

    def test_the_official_metadata_documents_earn_their_family(self):
        self.assertEqual(app_family(client(CLAUDE_CB, client_id=CLAUDE_DOC)), "claude")
        self.assertEqual(app_family(client(CHATGPT_CB, client_id=CHATGPT_DOC)), "chatgpt")
        per_server = "https://chatgpt.com/oauth/cb_Ab-3/client.json"
        self.assertEqual(app_family(client(CHATGPT_CB, client_id=per_server)), "chatgpt")

    def test_a_strangers_document_earns_nothing_even_with_only_official_callbacks(self):
        # A CIMD row is rebuilt from the document and refreshed when its cache expires: a stranger
        # connects with a callback of its own, then publishes an official-only document. The
        # redirect URIs of such a row prove nothing; its client_id URL is what is checked.
        for cid in (
            "https://evil.example/oauth/client.json",
            "https://claude.ai.evil.example/oauth/mcp-oauth-client-metadata",
            "https://evil.example/https://claude.ai/oauth/mcp-oauth-client-metadata",
            "https://claude.ai@evil.example/oauth/mcp-oauth-client-metadata",
            "https://claude.ai/oauth/mcp-oauth-client-metadata?x=1",
            "https://claude.ai/oauth/other",
            "https://CLAUDE.AI/oauth/mcp-oauth-client-metadata",
            "https://chatgpt.com/oauth/client.json/",
            "https://chatgpt.com/oauth/a/b/client.json",
            "https://chatgpt.com.evil.example/oauth/client.json",
            "HTTPS://evil.example/doc",
        ):
            for uris in ((CLAUDE_CB,), (CHATGPT_CB,)):
                self.assertIsNone(app_family(client(*uris, client_id=cid)), cid)

    def test_an_official_document_is_judged_by_its_url_not_by_a_foreign_callback(self):
        # Only the owner of the official host can publish the document; what it lists is theirs.
        self.assertEqual(
            app_family(client("http://localhost:9/cb", client_id=CLAUDE_DOC)), "claude"
        )

    def test_the_families_are_a_closed_set(self):
        self.assertEqual(mcp_client_family.APP_FAMILIES, ("claude", "chatgpt"))


class TheStamp(FlowBase):
    """The family is written WITH the write, from the registered client, through the real flow."""

    def comment_via(self, token, body):
        self._tool_text(self.call(token, "add_comment", {"task_id": "WRD-7", "body_text": body}))
        return TaskActivity.objects.filter(action_type="comment_added").latest("pk").metadata["via"]

    def test_a_write_through_the_claude_connector_is_stamped_claude(self):
        via = self.comment_via(self.tokens()["access_token"], "from claude")
        self.assertEqual(via["kind"], "oauth")
        self.assertEqual(via["app"], "claude")
        self.assertEqual(via["client_name"], "Claude", "the owner's whole via keeps its name")

    def test_a_write_through_the_chatgpt_connector_is_stamped_chatgpt(self):
        self.client_id = self.register(
            client_name="ChatGPT", redirect_uris=[CHATGPT_CB], scope=None
        )
        params = self.authorize_params(redirect_uri=CHATGPT_CB)
        code = self.consent(params=params)
        res = self.token(code=code, redirect_uri=CHATGPT_CB)
        self.assertEqual(res.status_code, 200, res.content)
        via = self.comment_via(res.json()["access_token"], "from chatgpt")
        self.assertEqual(via["app"], "chatgpt")

    def test_a_lookalike_name_on_a_foreign_redirect_is_not_stamped(self):
        # Registration is open: anyone may call their client "Claude".
        evil = "https://evil.example/callback"
        self.client_id = self.register(client_name="Claude", redirect_uris=[evil])
        code = self.consent(params=self.authorize_params(redirect_uri=evil))
        res = self.token(code=code, redirect_uri=evil)
        self.assertEqual(res.status_code, 200, res.content)
        via = self.comment_via(res.json()["access_token"], "from a lookalike")
        self.assertEqual(via["kind"], "oauth")
        self.assertEqual(via["client_name"], "Claude")
        self.assertNotIn("app", via)

    def test_an_official_callback_next_to_a_foreign_one_is_not_stamped(self):
        evil = "https://evil.example/callback"
        self.client_id = self.register(client_name="Claude", redirect_uris=[CALLBACK, evil])
        code = self.consent(params=self.authorize_params(redirect_uri=evil))
        res = self.token(code=code, redirect_uri=evil)
        self.assertEqual(res.status_code, 200, res.content)
        via = self.comment_via(res.json()["access_token"], "mixed")
        self.assertNotIn("app", via)

    def test_the_stamp_is_the_family_of_that_moment(self):
        # History is not rewritten: a row keeps what it was written with.
        token = self.tokens()["access_token"]
        self.comment_via(token, "first")
        row = TaskActivity.objects.filter(action_type="comment_added").latest("pk")
        app = self.connection().application
        app.redirect_uris = "https://evil.example/callback"
        app.save()
        row.refresh_from_db()
        self.assertEqual(row.metadata["via"]["app"], "claude")
        self.assertNotIn("app", self.comment_via(token, "later"))


class TheRule(unittest.TestCase):
    CLAUDE = {
        "kind": "oauth",
        "connection_id": "conn-9",
        "client_name": "My Claude",
        "app": "claude",
    }
    CHATGPT = {**CLAUDE, "app": "chatgpt"}
    SECRETS = ("conn-9", "My Claude")

    def test_a_colleague_reads_the_family_and_never_the_name_or_connection(self):
        for via, app in ((self.CLAUDE, "claude"), (self.CHATGPT, "chatgpt")):
            shown = via_for_viewer(via, "owner", "colleague")
            self.assertEqual(shown, {"kind": "oauth", "app": app})
            self.assertFalse([s for s in self.SECRETS if s in json.dumps(shown)])

    def test_the_owner_reads_their_own_via_whole(self):
        self.assertEqual(via_for_viewer(self.CLAUDE, "owner", "owner"), self.CLAUDE)

    def test_an_app_with_no_family_has_no_app_key(self):
        plain = {"kind": "oauth", "connection_id": "conn-9", "client_name": "Zapier"}
        self.assertEqual(via_for_viewer(plain, "owner", "colleague"), {"kind": "oauth"})

    def test_an_unknown_or_malformed_family_is_dropped(self):
        for bad in ("gemini", "Claude", "", None, 5, ["claude"], {"a": 1}, "claude ", "<b>"):
            via = {**self.CLAUDE, "app": bad}
            self.assertEqual(via_for_viewer(via, "owner", "colleague"), {"kind": "oauth"}, bad)

    def test_only_a_connected_app_carries_a_family(self):
        key = {
            "kind": "api_key",
            "key_id": "k",
            "key_name": "n",
            "key_prefix": "p",
            "app": "claude",
        }
        self.assertEqual(via_for_viewer(key, "owner", "colleague"), {"kind": "api_key"})
        for kind in ("app", "agent", "github", "unknown", "future_kind"):
            shown = via_for_viewer({"kind": kind, "app": "claude"}, "owner", "colleague")
            self.assertNotIn("app", shown, kind)

    def test_a_row_with_no_actor_shows_the_family_not_the_name(self):
        self.assertEqual(
            via_for_viewer(self.CLAUDE, None, "colleague"), {"kind": "oauth", "app": "claude"}
        )

    def test_the_input_is_not_edited(self):
        before = json.dumps(self.CLAUDE)
        via_for_viewer(self.CLAUDE, "owner", "colleague")
        self.assertEqual(json.dumps(self.CLAUDE), before)

    def test_the_switch_ships_on(self):
        self.assertIs(via_privacy.SHOW_OAUTH_APP_TO_EVERYONE, True)

    def test_flipped_off_the_family_is_the_owners_too(self):
        with mock.patch.object(via_privacy, "SHOW_OAUTH_APP_TO_EVERYONE", False):
            self.assertEqual(via_for_viewer(self.CLAUDE, "owner", "colleague"), {"kind": "oauth"})
            self.assertEqual(via_for_viewer(self.CLAUDE, "owner", "owner"), self.CLAUDE)
            self.assertEqual(via_for_viewer(self.CLAUDE, None, "colleague"), {"kind": "oauth"})


class TheActivityFeed(feed_tests._Base):
    APP = {
        "kind": "oauth",
        "connection_id": "conn-9",
        "client_name": "Alice's Claude",
        "app": "claude",
    }

    def test_a_colleague_reads_the_family_and_no_secret(self):
        self.row(self.user2, self.APP)
        data = self.feed(self.user)
        self.assertEqual(data[0]["metadata"]["via"], {"kind": "oauth", "app": "claude"})
        self.assertNoSecrets(data)

    def test_the_owner_reads_it_whole(self):
        self.row(self.user2, self.APP)
        self.assertEqual(self.feed(self.user2)[0]["metadata"]["via"], self.APP)

    def test_flipped_off_a_colleague_reads_the_kind_only(self):
        self.row(self.user2, self.APP)
        with mock.patch.object(via_privacy, "SHOW_OAUTH_APP_TO_EVERYONE", False):
            self.assertEqual(self.feed(self.user)[0]["metadata"]["via"], {"kind": "oauth"})
            self.assertEqual(self.feed(self.user2)[0]["metadata"]["via"], self.APP)

    def test_an_old_row_with_no_family_has_no_app_key(self):
        self.row(self.user2, feed_tests.APP)
        self.assertEqual(self.feed(self.user)[0]["metadata"]["via"], {"kind": "oauth"})

    def test_a_planted_family_on_an_api_key_row_is_not_shown(self):
        self.row(self.user2, {**feed_tests.KEY, "app": "claude"})
        self.assertEqual(self.feed(self.user)[0]["metadata"]["via"], {"kind": "api_key"})


class TheProgressFeed(feed_tests._Base):
    """Same rule on the progress feed (it shares `via_for_viewer`)."""

    fact_for = feed_tests.TheProgressFeed.fact_for
    entries = feed_tests.TheProgressFeed.entries

    def test_a_colleague_reads_the_family_there_too(self):
        app = {"kind": "oauth", "connection_id": "conn-9", "client_name": "Alice's Claude"}
        self.fact_for(self.row(self.user2, {**app, "app": "chatgpt"}))
        [theirs] = self.entries(self.user)
        self.assertEqual(theirs["metadata"]["via"], {"kind": "oauth", "app": "chatgpt"})
        self.assertNoSecrets(theirs)


class TheCommentList(comment_tests._Base):
    APP = {
        "kind": "oauth",
        "connection_id": "conn-9",
        "client_name": "Alice's Claude",
        "app": "claude",
    }

    def test_a_colleague_reads_the_family_and_no_secret(self):
        self.write(1, self.user2, self.APP)
        rows = self.listing(self.user)
        self.assertEqual(rows[1]["via"], {"kind": "oauth", "app": "claude"})
        text = repr(rows)
        for secret in comment_tests.SECRETS:
            self.assertNotIn(secret, text)

    def test_the_writer_reads_it_whole(self):
        self.write(1, self.user2, self.APP)
        self.assertEqual(self.listing(self.user2)[1]["via"], self.APP)

    def test_flipped_off_a_colleague_reads_the_kind_only(self):
        self.write(1, self.user2, self.APP)
        with mock.patch.object(via_privacy, "SHOW_OAUTH_APP_TO_EVERYONE", False):
            self.assertEqual(self.listing(self.user)[1]["via"], {"kind": "oauth"})

    def test_a_comment_an_app_then_a_person_edited_has_no_family(self):
        self.write(1, self.user2, self.APP)
        self.write(1, self.user2, {"kind": "app"})
        self.assertEqual(self.listing(self.user)[1]["via"], {"kind": "app"})


class TheModelSeesNothingNew(LineBase):
    """Model-visible surfaces are byte-identical: the in-app and MCP history tools rebuild a
    connected app's via themselves, and never carry the family."""

    APP = {"kind": "oauth", "connection_id": "conn-77", "client_name": "Zed", "app": "chatgpt"}

    def test_get_task_history_never_carries_the_family_not_even_to_the_owner(self):
        task = self.make_task("History", assignee=self.user, reporter=self.user)
        for actor in (self.user, self.user2):
            TaskActivity.objects.create(
                team=self.team,
                project=self.project,
                task=task,
                actor=actor,
                action_type="status_changed",
                field_name="status",
                old_value="Open",
                new_value="WIP",
                metadata={"via": self.APP},
            )
        result = self._result(self._call("get_task_history", {"task_id": str(task.task_id)}))
        self.assertFalse(result.get("isError"), result)
        history = result["structuredContent"]["history"]
        history = [h for h in history if h["action"] == "status_changed"]
        self.assertEqual(len(history), 2, "both rows were read")
        text = json.dumps(result)
        self.assertNotIn("chatgpt", text)
        for row in history:
            self.assertNotIn("app", row["via"])
