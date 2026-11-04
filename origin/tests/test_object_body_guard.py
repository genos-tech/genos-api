"""The base-view body guard (`object_body`, MIP-1013).

A JSON body is an object or it is no body: `[1]`, `"abc"`, `5`, `true` are a 400 in
`refuse_non_object_body`'s words; `null`, `[]`, `0`, `""`, `false` reach the handler as
`{}`; a body that is not JSON is a 400 of its own. Form and multipart posts, GET, and a
request with no JSON content type are not looked at.

The first half drives `guard_object_body` on a bare DRF `Request`, so it pins the two
DRF internals the guard leans on (`_full_data`/`_data` replacing the parsed value, and
the empty QueryDict DRF leaves after a parse error) on whichever DRF version runs it.
The second half goes through real routes.
"""

from __future__ import annotations

import json
from unittest import mock

from django.core.cache import cache
from django.http import QueryDict
from django.test import SimpleTestCase
from rest_framework.exceptions import ParseError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework.views import APIView

from origin.models.chat.unified_models import Activity, ActivityType, Channel, ChannelKind
from origin.views.common import base_auth_api_view
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.common.object_body import (
    NOT_AN_OBJECT,
    NOT_JSON,
    ObjectBodyMixin,
    guard_object_body,
)

from .test_base import BaseAPITestCase

NOT_OBJECTS = ("[1]", '"abc"', "5", "true", "1.5", '[{"a": 1}]', '["team_id"]')
EMPTY_ISH = ("null", "[]", "0", '""', "false", "0.0")
MALFORMED = ('{"a":', "{'a': 1}", "nope", "[1,", "\x00")

PARSERS = [JSONParser(), FormParser(), MultiPartParser()]


def _request(raw, method="POST", content_type="application/json") -> Request:
    wsgi = APIRequestFactory().generic(method, "/x/", raw, content_type=content_type)
    return Request(wsgi, parsers=PARSERS)


def _detail(exc: ParseError):
    return {k: str(v) for k, v in exc.detail.items()}


class GuardTests(SimpleTestCase):
    def test_an_object_passes_untouched(self):
        request = _request('{"a": 1}')
        guard_object_body(request)
        self.assertEqual(request.data, {"a": 1})

    def test_an_empty_body_reads_as_an_empty_object(self):
        for raw in EMPTY_ISH:
            request = _request(raw)
            guard_object_body(request)
            # A real dict, and DRF does not parse again on the next read.
            self.assertEqual(type(request.data), dict, repr(raw))
            self.assertEqual(request.data, {}, repr(raw))
            self.assertEqual(request.data, {}, repr(raw))

    def test_no_body_at_all_passes(self):
        guard_object_body(_request(""))
        guard_object_body(_request("{}"))

    def test_a_body_that_is_not_an_object_is_refused(self):
        for raw in NOT_OBJECTS:
            with self.assertRaises(ParseError, msg=raw) as caught:
                guard_object_body(_request(raw))
            self.assertEqual(_detail(caught.exception), {"error": NOT_AN_OBJECT}, raw)
            self.assertEqual(caught.exception.status_code, 400)

    def test_a_body_that_is_not_json_is_refused(self):
        for raw in MALFORMED:
            with self.assertRaises(ParseError, msg=repr(raw)) as caught:
                guard_object_body(_request(raw))
            self.assertEqual(_detail(caught.exception), {"error": NOT_JSON}, repr(raw))

    def test_a_parse_error_swallowed_before_the_guard_is_still_refused(self):
        # What the team alignment did: read the body, catch the error, carry on. DRF then
        # leaves an empty QueryDict where the data was, which read as an empty body.
        request = _request('{"a":')
        with self.assertRaises(ParseError):
            request.data
        self.assertIsInstance(request.data, QueryDict)  # the DRF behaviour this relies on
        with self.assertRaises(ParseError) as caught:
            guard_object_body(request)
        self.assertEqual(_detail(caught.exception), {"error": NOT_JSON})

    def test_every_body_method_is_checked_and_get_is_not(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.assertRaises(ParseError, msg=method):
                guard_object_body(_request("[1]", method=method))
        for method in ("GET", "HEAD", "OPTIONS"):
            guard_object_body(_request("[1]", method=method))

    def test_a_charset_on_the_json_type_is_still_json(self):
        with self.assertRaises(ParseError):
            guard_object_body(_request("[1]", content_type="application/json; charset=utf-8"))

    def test_a_body_that_is_not_json_typed_is_not_read(self):
        # Form, multipart, text and no type at all: the guard must not parse them (an
        # upload is a real cost, and reading `text/plain` would be a 415 for a view that
        # never reads its body).
        cases = [
            _request("a=1", content_type="application/x-www-form-urlencoded"),
            Request(
                APIRequestFactory().post("/x/", {"a": "1"}, format="multipart"),
                parsers=PARSERS,
            ),
            _request("[1]", content_type="text/plain"),
            _request("[1]", content_type=""),
        ]
        for request in cases:
            with mock.patch.object(
                Request, "_load_data_and_files", side_effect=AssertionError("parsed")
            ):
                guard_object_body(request)

    def test_a_form_post_still_reads_as_its_fields(self):
        request = _request("a=1", content_type="application/x-www-form-urlencoded")
        guard_object_body(request)
        self.assertEqual(request.data.get("a"), "1")


# ── through a view: rendering, ordering, opt-out ───────────────────────


class _Echo(ObjectBodyMixin, APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes: list = []

    def post(self, request):
        return Response({"seen": request.data, "type": type(request.data).__name__})

    delete = put = patch = post


class _EchoOptOut(_Echo):
    object_body_required = False


class _MultipartOnly(_Echo):
    parser_classes = [MultiPartParser]


class _AuthEcho(AuthenticatedAPIView):
    throttle_classes: list = []

    def post(self, request):
        return Response({"seen": request.data})


def _call(view, raw, method="POST", content_type="application/json", user=None):
    request = APIRequestFactory().generic(method, "/x/", raw, content_type=content_type)
    if user is not None:
        force_authenticate(request, user=user)
    response = view.as_view()(request)
    response.render()
    return response.status_code, json.loads(response.content)


class GuardViewTests(SimpleTestCase):
    def test_the_refusal_renders_as_an_error_body(self):
        # `ParseError({"error": ...})` on the wire, on the DRF version running this.
        self.assertEqual(_call(_Echo, "[1]"), (400, {"error": NOT_AN_OBJECT}))
        self.assertEqual(_call(_Echo, '{"a":'), (400, {"error": NOT_JSON}))

    def test_the_handler_sees_an_empty_body_as_an_empty_object(self):
        for raw in EMPTY_ISH:
            self.assertEqual(_call(_Echo, raw), (200, {"seen": {}, "type": "dict"}), repr(raw))

    def test_the_handler_sees_an_object_as_sent(self):
        self.assertEqual(_call(_Echo, '{"a": [1]}'), (200, {"seen": {"a": [1]}, "type": "dict"}))

    def test_a_delete_body_is_checked(self):
        self.assertEqual(_call(_Echo, "[1]", method="DELETE"), (400, {"error": NOT_AN_OBJECT}))

    def test_opting_out_leaves_the_body_to_the_view(self):
        self.assertEqual(_call(_EchoOptOut, "[1]"), (200, {"seen": [1], "type": "list"}))
        self.assertEqual(_call(_EchoOptOut, "null"), (200, {"seen": None, "type": "NoneType"}))

    def test_a_multipart_only_view_sent_json_is_still_a_415(self):
        status, _ = _call(_MultipartOnly, "[1]")
        self.assertEqual(status, 415)


class AuthenticatedViewGuardTests(BaseAPITestCase):
    def test_the_base_view_carries_the_guard(self):
        self.assertTrue(issubclass(AuthenticatedAPIView, ObjectBodyMixin))
        self.assertTrue(AuthenticatedAPIView.object_body_required)
        self.assertEqual(_call(_AuthEcho, "[1]", user=self.user), (400, {"error": NOT_AN_OBJECT}))
        self.assertEqual(_call(_AuthEcho, "null", user=self.user), (200, {"seen": {}}))

    def test_an_anonymous_caller_hears_401_before_any_word_about_the_body(self):
        for raw in ("[1]", '{"a":', "null"):
            status, _ = _call(_AuthEcho, raw)
            self.assertEqual(status, 401, repr(raw))

    def test_the_team_alignment_reads_the_body_the_guard_left(self):
        # The alignment reads the body too (for `team_id` and the object ids), and
        # swallows a parse error. It must run AFTER the guard: before it, it saw `null`
        # as None and a malformed body as an empty QueryDict.
        seen = []

        def spy(request):
            seen.append(request.data)

        with mock.patch.object(base_auth_api_view, "align_team_to_object", side_effect=spy):
            self.assertEqual(_call(_AuthEcho, "null", user=self.user), (200, {"seen": {}}))
            self.assertEqual(_call(_AuthEcho, '{"a":', user=self.user), (400, {"error": NOT_JSON}))
        self.assertEqual(seen, [{}])


class RealRouteGuardTests(BaseAPITestCase):
    READ_ALL = "/api/v3/activities/read-all/"

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient(raise_request_exception=False)
        self.authenticate()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="GM", owner=self.user
        )
        self.unread = [
            Activity.objects.create(
                team=self.team,
                recipient=self.user,
                actor=self.user2,
                activity_type=ActivityType.MENTION,
                channel=self.channel,
            )
            for _ in range(2)
        ]

    def send(self, method, url, raw, content_type="application/json"):
        return self.client.generic(method, url, raw, content_type=content_type)

    def test_a_malformed_body_marks_nothing_read(self):
        # The regression this guard was found by: the alignment swallowed the parse
        # error, the view then read an empty body, and an empty body here means "mark
        # EVERYTHING read".
        for raw in MALFORMED:
            resp = self.send("PUT", self.READ_ALL, raw)
            self.assertEqual((resp.status_code, resp.json()), (400, {"error": NOT_JSON}), repr(raw))
        self.assertFalse(Activity.objects.filter(is_read=True).exists())

    def test_an_empty_body_still_marks_everything_read(self):
        resp = self.send("PUT", self.READ_ALL, "null")
        self.assertEqual((resp.status_code, resp.json()), (200, {"updated": 2}))

    def test_a_non_object_is_refused_even_where_the_view_never_reads_the_body(self):
        # `?channel_id=` scoped read-all reads no body; the guard answers first anyway.
        resp = self.send("PUT", f"{self.READ_ALL}?channel_id={self.channel.id}", "[1]")
        self.assertEqual((resp.status_code, resp.json()), (400, {"error": NOT_AN_OBJECT}))
        self.assertFalse(Activity.objects.filter(is_read=True).exists())

    def test_a_null_body_reaches_the_handler_as_an_empty_one(self):
        # Answered 500 before (the view did `request.data.get`); `{}` is a 200.
        url = "/api/v2/user/preferences/timezone/"
        empty = self.send("PATCH", url, "{}")
        for raw in EMPTY_ISH:
            resp = self.send("PATCH", url, raw)
            self.assertEqual((resp.status_code, resp.json()), (empty.status_code, empty.json()))

    def test_a_form_post_is_untouched(self):
        resp = self.send(
            "PUT",
            self.READ_ALL,
            f"channel_id={self.channel.id}",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual((resp.status_code, resp.json()), (200, {"updated": 2}))

    def test_a_delete_body_is_checked_on_a_real_route(self):
        resp = self.send("DELETE", f"/api/v2/todo/items/{12345}/", "[1]")
        self.assertEqual((resp.status_code, resp.json()), (400, {"error": NOT_AN_OBJECT}))

    def test_an_anonymous_caller_is_401_on_a_real_route(self):
        self.unauthenticate()
        resp = self.send("PUT", self.READ_ALL, "[1]")
        self.assertEqual(resp.status_code, 401)
