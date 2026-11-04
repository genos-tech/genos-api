"""The body guard beyond `AuthenticatedAPIView` (MIP-1013, second half).

`PublicApiView` (REST v1) and the plain `APIView`s that read a body carry the same
`ObjectBodyMixin`: `[1]` is a 400 "Request body must be a JSON object.", `null` reads as
`{}`. Four of those answered 500 on a body that was not an object (calendar create and
update, GitHub register-repo, OAuth connect), and v1 task PATCH did on `null` or a
string naming a field.

Three views are deliberately NOT guarded, and are pinned here so a later "guard
everything" cannot slip past them:

* MCP answers in JSON-RPC's own words (`-32600`), which a client shows;
* the Stripe and GitHub webhooks are authenticated by a signature over the RAW body,
  and must answer the sender, not judge its JSON first.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from origin.models.common.api_key_models import (
    SCOPE_READ,
    SCOPE_WRITE,
    ApiKey,
    generate_key,
    hash_key,
)
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import stripe_billing
from origin.views.common.billing_views import StripeWebhookView
from origin.views.common.calendar_views import CalendarEventDetailView, CalendarEventsView
from origin.views.common.github_views import GithubRegisterRepoView, GithubWebhookView
from origin.views.common.oauth_views import (
    IntegrationsDisconnectAccountView,
    IntegrationsDisconnectView,
    OAuthInitiateView,
)
from origin.views.common.object_body import NOT_AN_OBJECT, NOT_JSON, ObjectBodyMixin
from origin.views.public.mcp.views import McpView
from origin.views.public.v1_views import PublicApiView

from .test_base import BaseAPITestCase
from .test_github_webhook import WEBHOOK_SECRET

NOT_OBJECTS = ("[1]", '"abc"', "5", "true", '"title"', '["title"]')
EMPTY_ISH = ("null", "[]", "0", '""', "false")
REFUSED = {"error": NOT_AN_OBJECT}


class _Client(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # The real 500 response if there is one, not the exception re-raised into the test.
        self.client = APIClient(raise_request_exception=False)

    def send(self, method, url, raw, **extra):
        return self.client.generic(method, url, raw, content_type="application/json", **extra)

    def assertSameAsEmpty(self, method, url, raws=EMPTY_ISH):
        empty = self.send(method, url, "{}")
        self.assertNotEqual(empty.status_code, 500)
        for raw in raws:
            resp = self.send(method, url, raw)
            self.assertEqual(
                (resp.status_code, resp.content), (empty.status_code, empty.content), repr(raw)
            )
        return empty


class GuardedClassesTests(_Client):
    def test_the_guarded_and_the_unguarded_views(self):
        for cls in (
            PublicApiView,
            CalendarEventsView,
            CalendarEventDetailView,
            GithubRegisterRepoView,
            OAuthInitiateView,
            IntegrationsDisconnectView,
            IntegrationsDisconnectAccountView,
        ):
            self.assertTrue(issubclass(cls, ObjectBodyMixin), cls.__name__)
            self.assertTrue(cls.object_body_required, cls.__name__)
        self.assertFalse(McpView.object_body_required)
        for cls in (StripeWebhookView, GithubWebhookView):
            self.assertFalse(issubclass(cls, ObjectBodyMixin), cls.__name__)


class PublicApiBodyTests(_Client):
    TASKS = "/api/public/v1/tasks/"

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open", reporter=self.user
        )

    def _key(self, scope=SCOPE_WRITE):
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user,
            team=self.team,
            name="k",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=scope,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")

    def detail(self):
        return f"{self.TASKS}{self.task.task_id}/"

    def test_a_body_that_is_not_an_object_is_refused_and_writes_nothing(self):
        self._key()
        before = TaskMaster.objects.count()
        for method, url in (("POST", self.TASKS), ("PATCH", self.detail())):
            for raw in NOT_OBJECTS:
                resp = self.send(method, url, raw)
                self.assertEqual((resp.status_code, resp.json()), (400, REFUSED), (method, raw))
        self.assertEqual(TaskMaster.objects.count(), before)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "T")

    def test_an_empty_body_answers_as_an_empty_object(self):
        # PATCH answered 500 on `null` (`"title" in None`).
        self._key()
        post = self.assertSameAsEmpty("POST", self.TASKS)
        self.assertEqual(post.json(), {"error": "title and project_id are required."})
        patch = self.assertSameAsEmpty("PATCH", self.detail())
        self.assertEqual(patch.json(), {"error": "No writable fields supplied."})

    def test_a_malformed_body_is_refused(self):
        self._key()
        resp = self.send("POST", self.TASKS, '{"title":')
        self.assertEqual((resp.status_code, resp.json()), (400, {"error": NOT_JSON}))

    def test_an_object_still_writes(self):
        self._key()
        resp = self.send("PATCH", self.detail(), '{"title": "Renamed"}')
        self.assertEqual(resp.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Renamed")

    def test_the_key_and_its_scope_are_checked_first(self):
        # No key: refused for that, whatever the body.
        self.assertIn(self.send("POST", self.TASKS, "[1]").status_code, (401, 403))
        # A read key on a write: the scope refusal, not the body's.
        self._key(scope=SCOPE_READ)
        resp = self.send("POST", self.TASKS, "[1]")
        self.assertEqual(resp.status_code, 403)

    def test_the_off_switch_still_answers_first(self):
        self._key()
        with override_settings(PUBLIC_API_DISABLED=True):
            resp = self.send("POST", self.TASKS, "[1]")
        self.assertEqual(resp.status_code, 503)


class McpBodyTests(_Client):
    MCP = "/api/public/v1/mcp"

    def setUp(self):
        super().setUp()
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user,
            name="mcp",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=SCOPE_WRITE,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")

    def url(self):
        return f"{self.MCP}?team_id={self.team.team_id}"

    def test_a_body_that_is_not_an_object_is_still_a_json_rpc_error(self):
        # -32600 "Invalid Request" for anything that is not one request object; a JSON
        # string is handed to MCP's own re-parse (`_parse`), where "abc" is -32700.
        cases = {
            "[1]": -32600,
            '[{"jsonrpc": "2.0", "method": "ping", "id": 1}]': -32600,
            "5": -32600,
            "true": -32600,
            "null": -32600,
            '"abc"': -32700,
        }
        for raw, code in cases.items():
            resp = self.send("POST", self.url(), raw)
            self.assertEqual(resp.status_code, 400, raw)
            body = resp.json()
            self.assertEqual(body["jsonrpc"], "2.0", raw)
            self.assertEqual(body["error"]["code"], code, raw)
            self.assertNotIn(NOT_AN_OBJECT, json.dumps(body), raw)


@override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
class GithubWebhookBodyTests(_Client):
    URL = "/api/v2/github/webhook/"

    def ping(self, raw: str, secret: str = WEBHOOK_SECRET):
        body = raw.encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.send(
            "POST", self.URL, raw, HTTP_X_HUB_SIGNATURE_256=sig, HTTP_X_GITHUB_EVENT="ping"
        )

    def test_the_signature_is_checked_over_the_raw_body_whatever_its_shape(self):
        # The sender is answered, not its JSON judged: a signed body is a pong whatever it
        # parses as, and a badly signed one is the signature's 401, not a 400 about JSON.
        for raw in ('{"zen": "x"}', "[1]", "null", '"abc"', '{"zen":'):
            resp = self.ping(raw)
            self.assertEqual((resp.status_code, resp.json()), (200, {"detail": "pong"}), raw)
            resp = self.ping(raw, secret="wrong")
            self.assertEqual(resp.status_code, 401, raw)


class StripeWebhookBodyTests(_Client):
    URL = "/api/v2/billing/stripe/webhook/"

    def test_the_raw_body_reaches_the_signature_check_whatever_its_shape(self):
        for raw in ('{"id": "evt"}', "[1]", "null", '{"id":'):
            with (
                mock.patch.object(
                    stripe_billing, "verify_webhook", return_value={"id": "evt_x"}
                ) as verify,
                mock.patch.object(stripe_billing, "handle_event", return_value="ignored"),
            ):
                resp = self.send("POST", self.URL, raw, HTTP_STRIPE_SIGNATURE="t=1,v1=x")
            self.assertEqual(resp.status_code, 200, raw)
            self.assertEqual(verify.call_args.args[0], raw.encode(), raw)

    def test_a_bad_signature_is_still_the_signatures_400(self):
        with mock.patch.object(
            stripe_billing, "verify_webhook", side_effect=stripe_billing.BillingError("bad sig")
        ):
            resp = self.send("POST", self.URL, "[1]", HTTP_STRIPE_SIGNATURE="t=1,v1=x")
        self.assertEqual((resp.status_code, resp.json()), (400, {"detail": "invalid_signature"}))


class PlainViewBodyTests(_Client):
    """The plain `APIView`s that read a body. Each answered 500 on `[1]` and on `null`."""

    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_calendar_create_and_update(self):
        for method, url in (
            ("POST", "/api/v2/calendar/events/"),
            ("PATCH", "/api/v2/calendar/events/evt1/"),
        ):
            for raw in NOT_OBJECTS:
                resp = self.send(method, url, raw)
                self.assertEqual((resp.status_code, resp.json()), (400, REFUSED), (method, raw))
            self.assertSameAsEmpty(method, url)

    def test_github_register_repo(self):
        url = "/api/v2/github/register-repo/"
        for raw in NOT_OBJECTS:
            resp = self.send("POST", url, raw)
            self.assertEqual((resp.status_code, resp.json()), (400, REFUSED), raw)
        empty = self.assertSameAsEmpty("POST", url)
        self.assertEqual((empty.status_code, empty.json()), (400, {"detail": "invalid_repo"}))

    def test_integrations_disconnect(self):
        for url in (
            "/api/v2/integrations/google/",
            f"/api/v2/integrations/account/{self.user.id}/",
        ):
            resp = self.send("DELETE", url, "[1]")
            self.assertEqual((resp.status_code, resp.json()), (400, REFUSED), url)
            self.assertSameAsEmpty("DELETE", url)


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="goog-client-id", GOOGLE_OAUTH_CLIENT_SECRET="goog-secret"
)
class OAuthConnectBodyTests(_Client):
    URL = "/api/v2/oauth/google/initiate/"

    def test_a_body_that_is_not_an_object_is_refused(self):
        # Answered 500 (`request.data.get("intent")` on a list).
        self.authenticate()
        for raw in NOT_OBJECTS:
            resp = self.send("POST", self.URL, raw)
            self.assertEqual((resp.status_code, resp.json()), (400, REFUSED), raw)

    def test_an_empty_body_is_the_connect_it_always_was(self):
        self.authenticate()
        empty = self.assertSameAsEmpty("POST", self.URL, raws=())
        self.assertEqual(empty.status_code, 200)
        self.assertIn("accounts.google.com", empty.json()["url"])
        for raw in EMPTY_ISH:
            resp = self.send("POST", self.URL, raw)
            self.assertEqual(resp.status_code, 200, repr(raw))
            self.assertIn("accounts.google.com", resp.json()["url"], repr(raw))

    def test_anonymous(self):
        # This view is `AllowAny` (GET is the sign-in redirect), and says "Must be signed
        # in" itself, in the handler. So an anonymous `[1]` hears about the body first:
        # the guard runs after DRF's permissions, and DRF's permissions let it in.
        self.assertEqual(self.send("POST", self.URL, "{}").status_code, 401)
        resp = self.send("POST", self.URL, "[1]")
        self.assertEqual((resp.status_code, resp.json()), (400, REFUSED))
