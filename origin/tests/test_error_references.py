"""Error answers carry a reference, never their exception (MIP-622).

An exception's text names tables, columns, Stripe and provider internals
and the values that broke them, and a provider's response body quotes our
request back. These pin that what reaches a client, a model or an MCP
caller is text we wrote plus the request's reference (`apis/request_id.py`),
and that the log line holding the cause carries the same reference:

* the middleware sets it, says it in `X-Request-ID`, and the JSON log
  formatter stamps it on every line of the request;
* `error_response`, `handler500`, billing's 503s, the note views' 500s,
  calendar create's 502, the write tools' ToolErrors and the agent
  stream's crash event;
* `describe_exception` keeps exactly the status the eval runner needs to
  tell a provider outage from a bad answer.
"""

import json
import logging
import threading
from unittest import mock

import httpx
import requests
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import path
from google.genai import errors as genai_errors
from rest_framework.test import APIClient

from apis.logging_formatters import RailwayJSONFormatter
from apis.request_id import (
    GENERIC_MESSAGE,
    RequestIdMiddleware,
    current_request_id,
    describe_exception,
    describe_upstream,
    error_response,
    server_error,
)
from origin.models.common.user_models import ConnectedAccount, CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.controller import _drive_loop
from origin.search_engine.agent.evals.runner import _infra_failure
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent_views import _stream_ndjson
from origin.search_engine.llm.types import AgentMessage
from origin.services import stripe_billing

from .test_base import BaseAPITestCase

# What an exception would leak if it reached the client.
_SECRET = "relation origin_taskmaster column due_date someone@example.com"


def _boom(*_args, **_kwargs):
    raise RuntimeError(_SECRET)


def _crash_view(request):
    raise RuntimeError(_SECRET)


urlpatterns = [path("crash/", _crash_view)]
handler500 = "apis.request_id.server_error"


class _Capture(logging.Handler):
    """Every record, and the reference current when it was logged."""

    def __init__(self):
        super().__init__()
        self.records: list[tuple[logging.LogRecord, str | None]] = []

    def emit(self, record):
        self.records.append((record, current_request_id()))


def _capture(logger_name="origin.errors"):
    handler = _Capture()
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    return handler, lambda: logger.removeHandler(handler)


class RequestIdTests(SimpleTestCase):
    def test_middleware_sets_the_reference_and_says_it_in_the_header(self):
        seen = {}

        def view(request):
            from django.http import HttpResponse  # noqa: PLC0415

            seen["id"] = current_request_id()
            return HttpResponse("ok")

        response = RequestIdMiddleware(view)(RequestFactory().get("/"))
        self.assertTrue(seen["id"])
        self.assertEqual(response["X-Request-ID"], seen["id"])
        self.assertIsNone(current_request_id(), "the reference outlives its request")

    def test_each_request_gets_its_own(self):
        ids = []

        def view(request):
            from django.http import HttpResponse  # noqa: PLC0415

            ids.append(current_request_id())
            return HttpResponse("ok")

        middleware = RequestIdMiddleware(view)
        middleware(RequestFactory().get("/"))
        middleware(RequestFactory().get("/"))
        self.assertNotEqual(ids[0], ids[1])

    def test_a_raising_view_still_clears_it(self):
        with self.assertRaises(RuntimeError):
            RequestIdMiddleware(_boom)(RequestFactory().get("/"))
        self.assertIsNone(current_request_id())

    def test_json_log_lines_carry_it(self):
        record = logging.LogRecord("origin.test", logging.INFO, __file__, 1, "hi", (), None)
        captured = {}

        def view(request):
            from django.http import HttpResponse  # noqa: PLC0415

            captured["line"] = RailwayJSONFormatter().format(record)
            return HttpResponse("ok")

        response = RequestIdMiddleware(view)(RequestFactory().get("/"))
        self.assertEqual(json.loads(captured["line"])["request_id"], response["X-Request-ID"])

    def test_json_log_lines_outside_a_request_carry_none(self):
        record = logging.LogRecord("origin.test", logging.INFO, __file__, 1, "hi", (), None)
        self.assertNotIn("request_id", json.loads(RailwayJSONFormatter().format(record)))

    def test_the_middleware_runs_first(self):
        from django.conf import settings  # noqa: PLC0415

        self.assertEqual(settings.MIDDLEWARE[0], "apis.request_id.RequestIdMiddleware")


class ErrorResponseTests(SimpleTestCase):
    def test_the_answer_names_the_reference_and_the_log_the_cause(self):
        handler, remove = _capture()
        self.addCleanup(remove)
        captured = {}

        def view(request):
            try:
                _boom()
            except RuntimeError as e:
                captured["response"] = error_response("thing failed", e)
            return captured["response"]

        RequestIdMiddleware(view)(RequestFactory().get("/"))
        response = captured["response"]
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.data["error"], GENERIC_MESSAGE)
        self.assertNotIn(_SECRET, json.dumps(response.data))

        ((record, logged_ref),) = handler.records
        self.assertEqual(logged_ref, response.data["reference"])
        self.assertIn(response.data["reference"], record.getMessage())
        self.assertIs(record.exc_info[0], RuntimeError, "the traceback goes to the log")

    def test_outside_a_request_it_still_makes_one(self):
        response = error_response(
            "thing failed", RuntimeError(_SECRET), status=503, message="Try later."
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"], "Try later.")
        self.assertTrue(response.data["reference"])


@override_settings(ROOT_URLCONF=__name__, DEBUG=False)
class Handler500Tests(SimpleTestCase):
    def test_an_uncaught_exception_answers_json_with_the_reference(self):
        client = self.client_class(raise_request_exception=False)
        response = client.get("/crash/")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response["Content-Type"], "application/json")
        body = response.json()
        self.assertEqual(body["error"], GENERIC_MESSAGE)
        self.assertEqual(body["reference"], response["X-Request-ID"])
        self.assertNotIn(_SECRET, response.content.decode())

    def test_apis_urls_installs_it(self):
        from apis import urls  # noqa: PLC0415

        self.assertEqual(urls.handler500, "apis.request_id.server_error")

    def test_server_error_outside_a_request(self):
        response = server_error(RequestFactory().get("/"))
        self.assertEqual(json.loads(response.content)["error"], GENERIC_MESSAGE)


def _upstream(status_code, body):
    resp = requests.Response()
    resp.status_code = status_code
    resp._content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return resp


class DescribeTests(SimpleTestCase):
    def test_google_status_word_survives_and_its_message_does_not(self):
        exc = genai_errors.ServerError(
            504, {"error": {"code": 504, "message": _SECRET, "status": "DEADLINE_EXCEEDED"}}
        )
        self.assertEqual(describe_exception(exc), "ServerError 504 DEADLINE_EXCEEDED")

    def test_sdk_status_codes(self):
        request = httpx.Request("POST", "https://example.invalid/v1")
        import anthropic  # noqa: PLC0415
        import openai  # noqa: PLC0415

        overloaded = anthropic.InternalServerError(
            _SECRET, response=httpx.Response(529, request=request), body=None
        )
        limited = openai.RateLimitError(
            _SECRET, response=httpx.Response(429, request=request), body=None
        )
        self.assertEqual(describe_exception(overloaded), "InternalServerError 529")
        self.assertEqual(describe_exception(limited), "RateLimitError 429")

    def test_a_plain_exception_is_its_type(self):
        self.assertEqual(describe_exception(RuntimeError(_SECRET)), "RuntimeError")

    def test_a_non_int_code_or_lowercase_status_is_dropped(self):
        exc = RuntimeError(_SECRET)
        exc.code = _SECRET
        exc.status = _SECRET
        self.assertEqual(describe_exception(exc), "RuntimeError")

    def test_upstream_google(self):
        resp = _upstream(
            403, {"error": {"message": _SECRET, "status": "PERMISSION_DENIED", "errors": []}}
        )
        self.assertEqual(describe_upstream(resp), "status=403 code=PERMISSION_DENIED")

    def test_upstream_google_reason(self):
        resp = _upstream(403, {"error": {"message": _SECRET, "errors": [{"reason": "forbidden"}]}})
        self.assertEqual(describe_upstream(resp), "status=403 code=forbidden")

    def test_upstream_github(self):
        resp = _upstream(
            422,
            {
                "message": _SECRET,
                "documentation_url": "https://docs.github.com/rest/repos/webhooks#create-a-repository-webhook",
            },
        )
        self.assertEqual(describe_upstream(resp), "status=422 code=create-a-repository-webhook")

    def test_upstream_not_json(self):
        self.assertEqual(describe_upstream(_upstream(502, _SECRET.encode())), "status=502")


class EvalSignatureTests(SimpleTestCase):
    """The controller's LLM failure message is what `_infra_failure` reads:
    if it stopped naming the status, a quota outage would score as a
    regression."""

    def _failure(self, exc):
        class _Failing:
            def generate_step(self, *_args, **_kwargs):
                raise exc
                yield  # pragma: no cover — makes this a generator

        events = []
        se = {
            "AGENT_EFFORT_LEVELS": False,
            "AGENT_STEP_CAP_WRAPUP": False,
            "AGENT_INJECT_CURRENT_DATE": False,
        }
        from django.conf import settings  # noqa: PLC0415

        with (
            override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **se}),
            mock.patch(
                "origin.search_engine.agent.controller.get_model_client", return_value=_Failing()
            ),
        ):
            _drive_loop(
                messages=[AgentMessage(role="user", text="what is due?")],
                ctx=ToolContext(team_id="team", user_id="1"),
                emit=events.append,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
            )
        return [e for e in events if e.get("type") == "error"]

    def test_quota_is_infra_and_its_message_stays_out(self):
        exc = genai_errors.ClientError(
            429, {"error": {"code": 429, "message": _SECRET, "status": "RESOURCE_EXHAUSTED"}}
        )
        errors = self._failure(exc)
        self.assertTrue(_infra_failure(errors), errors)
        self.assertNotIn(_SECRET, json.dumps(errors))

    def test_deadline_is_infra(self):
        exc = genai_errors.ServerError(
            504, {"error": {"code": 504, "message": _SECRET, "status": "DEADLINE_EXCEEDED"}}
        )
        self.assertTrue(_infra_failure(self._failure(exc)))

    def test_a_bug_is_not_infra(self):
        errors = self._failure(KeyError(_SECRET))
        self.assertFalse(_infra_failure(errors))
        self.assertNotIn(_SECRET, json.dumps(errors))


class StreamWorkerTests(SimpleTestCase):
    def setUp(self):
        for target in (
            "origin.search_engine.agent_views._push_run_complete",
            "origin.search_engine.ingestion.ingest_conversation_run",
        ):
            patcher = mock.patch(target, autospec=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _events(self, worker_target, **kwargs):
        lines = b"".join(_stream_ndjson(worker_target, **kwargs)).decode().splitlines()
        return [json.loads(line) for line in lines if line]

    def test_a_crash_says_its_reference_not_its_exception(self):
        handler, remove = _capture()
        self.addCleanup(remove)
        events = self._events(_boom, request_id="feedc0ffee123456")
        (error,) = [e for e in events if e["type"] == "error"]
        self.assertEqual(error["message"], "Agent crashed (ref feedc0ffee123456).")
        self.assertIn("feedc0ffee123456", handler.records[0][0].getMessage())

    def test_the_worker_logs_under_the_requests_reference(self):
        """The generator runs after the middleware has reset the context, so
        the reference has to be handed in."""
        seen = {}

        def worker_target(emit, cancel_event):
            seen["id"] = current_request_id()

        self._events(worker_target, request_id="feedc0ffee123456")
        self.assertEqual(seen["id"], "feedc0ffee123456")


class AskViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_the_view_hands_its_reference_to_the_stream(self):
        """Django iterates the stream after the middleware has reset the
        context, so the view must pass the reference while it still has it."""
        captured = {}

        def fake_stream(worker, **kwargs):
            captured.update(kwargs)
            return iter([b""])

        with (
            mock.patch("origin.search_engine.agent_views._stream_ndjson", side_effect=fake_stream),
            mock.patch(
                "origin.search_engine.agent_views.check_remaining", return_value=(True, 0, None)
            ),
        ):
            res = self.client.post(
                "/api/v2/agent/ask/",
                {"query": "What is due?", "team_id": str(self.team.team_id)},
                format="json",
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(captured["request_id"], res["X-Request-ID"])


class BillingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    @override_settings(
        STRIPE={
            "SECRET_KEY": "sk_test_x",
            "WEBHOOK_SECRET": "whsec_x",
            "PRICE_CORE": "price_core_789",
            "PRICE_PRO": "price_pro_123",
            "PRICE_MAX": "price_max_456",
            "PRICE_PRO_LEGACY": "",
            "PRICE_MAX_LEGACY": "",
            "AUTOMATIC_TAX": False,
            "DEFAULT_CURRENCY": "jpy",
            "PRICES_BY_CURRENCY": {},
        }
    )
    def test_a_stripe_failure_says_our_text_and_a_reference(self):
        import stripe  # noqa: PLC0415

        self.user.stripe_customer_id = "cus_abc"
        self.user.save(update_fields=["stripe_customer_id"])
        alive = stripe.Customer.construct_from({"id": "cus_abc", "object": "customer"}, "sk_test_x")
        with (
            mock.patch("stripe.Customer.retrieve", return_value=alive),
            mock.patch(
                "stripe.checkout.Session.create",
                side_effect=stripe.InvalidRequestError(_SECRET, param="price"),
            ),
        ):
            res = self.client.post("/api/v2/billing/checkout/", {"plan": "pro"}, format="json")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["error"], "Could not start checkout.")
        self.assertEqual(res.json()["reference"], res["X-Request-ID"])
        self.assertNotIn(_SECRET, res.content.decode())

    def test_billing_error_keeps_the_stripe_cause(self):
        with self.assertRaises(stripe_billing.BillingError) as raised:
            try:
                raise RuntimeError(_SECRET)
            except RuntimeError as e:
                raise stripe_billing.BillingError("Could not start checkout.") from e
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)


class NoteViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_a_failed_create_names_no_cause(self):
        with mock.patch(
            "origin.views.note.personal_note_views.snapshot_note_version", side_effect=_boom
        ):
            res = self.client.post(
                "/api/v2/note/personal/",
                {
                    "team_id": str(self.team.team_id),
                    "user_id": self.user.id,
                    "title": "T",
                    "body": {"content": "hello"},
                },
                format="json",
            )
        self.assertEqual(res.status_code, 500)
        body = res.json()
        self.assertEqual(body["error"], "Failed to create note and role.")
        self.assertNotIn("details", body)
        self.assertEqual(body["reference"], res["X-Request-ID"])
        self.assertNotIn(_SECRET, res.content.decode())


class CalendarCreateTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = CustomUser.objects.create_user(
            username="cal", email="cal@example.com", password="pw123456"
        )
        ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="cal-sub",
            provider_email="cal@example.com",
            scopes=["https://www.googleapis.com/auth/calendar.events"],
            access_token_encrypted="placeholder",
            is_login_identity=True,
        )
        self.client.force_authenticate(user=self.user)

    @mock.patch("origin.views.common.calendar_views.get_valid_access_token", return_value="tok")
    @mock.patch("origin.views.common.calendar_views.requests.request")
    def test_a_refusal_answers_the_status_not_googles_body(self, mock_request, _tok):
        mock_request.return_value = _upstream(
            400, {"error": {"message": _SECRET, "status": "INVALID_ARGUMENT"}}
        )
        res = self.client.post(
            "/api/v2/calendar/events/",
            {
                "summary": "Standup",
                "start": {"dateTime": "2026-08-03T09:00:00Z"},
                "end": {"dateTime": "2026-08-03T09:15:00Z"},
            },
            format="json",
        )
        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.json(), {"detail": "calendar_api_error", "upstream_status": 400})


class WriteToolTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def test_a_database_error_reaches_the_log_not_the_model(self):
        handler, remove = _capture()
        self.addCleanup(remove)
        with self.assertRaises(ToolError) as raised:
            CREATE_TASK.run(
                {"title": "T", "project_id": self.project.project_id, "due_date": _SECRET},
                self.ctx,
            )
        message = str(raised.exception)
        self.assertRegex(message, r"^Failed to create task \(ref [0-9a-f]{16}\)\.$")
        self.assertNotIn("due_date", message)
        ref = message.split("ref ")[1].rstrip(").")
        self.assertIn(ref, handler.records[0][0].getMessage())
        self.assertIsNotNone(handler.records[0][0].exc_info)


class StreamWorkerThreadTests(SimpleTestCase):
    """`bind_request_id` resets on the way out, so a pooled thread never
    carries one request's reference into the next."""

    def test_the_binding_is_reset(self):
        from apis.request_id import bind_request_id, reset_request_id  # noqa: PLC0415

        seen = []

        def run():
            token = bind_request_id("abc")
            seen.append(current_request_id())
            reset_request_id(token)
            seen.append(current_request_id())

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        self.assertEqual(seen, ["abc", None])
