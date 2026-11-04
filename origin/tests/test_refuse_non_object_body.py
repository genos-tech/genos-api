"""`refuse_non_object_body`: a body that is JSON but not an object is a 400 (MIP-996).

`body_object` (MIP-992) reads such a body as `{}`, which is right for a view with a
required field: the view answers its own "is required". It is wrong for a view where an
empty body is a request in its own right (mark everything read, a billing-portal link):
`[1]` read as `{}` would run that request as if nothing had been sent. This is the other
half: refuse a truthy value that is not an object, and leave the empty ones alone.
"""

from django.test import SimpleTestCase
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from origin.views.utils.request_validators import body_object, refuse_non_object_body

NOT_OBJECTS = ("[1]", '"abc"', "5", "true", "1.5", '[{"a": 1}]')
EMPTY_ISH = ("null", "[]", "0", '""', "false", "0.0", "{}", "")


def _request(raw: str) -> Request:
    wsgi = APIRequestFactory().post("/x/", raw, content_type="application/json")
    return Request(wsgi, parsers=[JSONParser(), FormParser(), MultiPartParser()])


class RefuseNonObjectBodyTests(SimpleTestCase):
    def test_a_body_that_is_not_an_object_is_a_400(self):
        for raw in NOT_OBJECTS:
            refusal = refuse_non_object_body(_request(raw))
            self.assertIsNotNone(refusal, raw)
            self.assertEqual(refusal.status_code, 400, raw)
            self.assertEqual(refusal.data, {"error": "Request body must be a JSON object."}, raw)

    def test_an_empty_body_is_not_refused(self):
        # These were already "no body" for `request.data or {}`, and stay so.
        for raw in EMPTY_ISH:
            self.assertIsNone(refuse_non_object_body(_request(raw)), repr(raw))

    def test_an_object_is_not_refused(self):
        self.assertIsNone(refuse_non_object_body(_request('{"flow": "cancel"}')))

    def test_a_form_post_is_not_refused(self):
        # A form body is a QueryDict, which is a dict.
        wsgi = APIRequestFactory().post("/x/", {"flow": "cancel"}, format="multipart")
        request = Request(wsgi, parsers=[JSONParser(), FormParser(), MultiPartParser()])
        self.assertIsNone(refuse_non_object_body(request))
        self.assertEqual(body_object(request).get("flow"), "cancel")

    def test_what_it_lets_through_reads_the_same_through_body_object(self):
        # A view calls this first, then `body_object`: nothing it passes may read as a
        # non-dict there.
        for raw in EMPTY_ISH + ('{"a": 1}',):
            request = _request(raw)
            self.assertIsNone(refuse_non_object_body(request), repr(raw))
            self.assertIsInstance(body_object(request), dict, repr(raw))
