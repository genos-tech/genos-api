"""One rule for a JSON request body: it is an object, or it is no body at all.

Every view here reads its body as a dict (`request.data.get(...)`). A JSON body
that is not an object reached those reads as a list, a string or a number, and
the view answered with a 500 (MIP-1013: 136 of the 247 mutating DRF routes did).
Fixing that view by view (`body_object`, `refuse_non_object_body`) left
every new view to remember it, so the rule lives in the base view instead:

  * an object passes untouched;
  * an empty value (`null`, `[]`, `0`, `""`, `false`) is no body: the view sees
    `{}`, which is what `request.data or {}` always made of it;
  * anything else (`[1]`, `"abc"`, `5`, `true`) is a 400, `NOT_AN_OBJECT`, the
    same words `refuse_non_object_body` already used;
  * a body that is not JSON at all is a 400, `NOT_JSON`. Before this, the team
    alignment in `AuthenticatedAPIView` read the body first and swallowed the
    parse error, and DRF leaves an empty QueryDict behind after one, so the view
    read a malformed body as an EMPTY one: `PUT /activities/read-all/` with a
    broken body marked everything read.

Only a JSON request with a body-carrying method is looked at. A form or
multipart post is left to its view (a QueryDict is a dict already, and parsing
an upload here would cost every request), and so is a request with no content
type, which DRF reads as an empty body.
"""

from __future__ import annotations

from django.http import QueryDict
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser

NOT_AN_OBJECT = "Request body must be a JSON object."
NOT_JSON = "Request body is not valid JSON."

BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_json_body(request) -> bool:
    """Would DRF read this request's body with its JSON parser?

    Asked of DRF's own parser selection rather than of the header text, so
    `application/json; charset=utf-8` counts and `text/plain` does not (for
    `text/plain` the view decides; reading it here would turn a view that
    never reads its body into a 415).
    """
    if request.method not in BODY_METHODS or not request.content_type:
        return False
    parser = request.negotiator.select_parser(request, request.parsers)
    return isinstance(parser, JSONParser)


def guard_object_body(request) -> None:
    """Refuse a JSON body that is not an object; read an empty one as `{}`.

    Raises `ParseError({"error": ...})`, which DRF renders as a 400 with that
    body. No catch-all: only the JSON parser's own `ParseError` is turned into
    `NOT_JSON`, so a view's 415 (a multipart-only view sent JSON) still is one.
    """
    if not _is_json_body(request):
        return
    try:
        data = request.data
    except ParseError:
        raise ParseError({"error": NOT_JSON}) from None
    if isinstance(data, QueryDict):
        # The JSON parser never returns one. DRF leaves an empty QueryDict in
        # place of the data after a parse error, so this is a malformed body
        # that something read before us and swallowed the error of.
        raise ParseError({"error": NOT_JSON})
    if isinstance(data, dict):
        return
    if data:
        raise ParseError({"error": NOT_AN_OBJECT})
    # `null`, `[]`, `0`, `""`, `false`: no body. DRF has no public way to
    # replace the parsed data; `_full_data` is what `request.data` returns and
    # `_data` is what stops DRF parsing again. Pinned on the DRF versions we
    # run by `test_object_body_guard`.
    request._data = {}
    request._full_data = {}


class ObjectBodyMixin:
    """Run `guard_object_body` after DRF's own checks, before the handler.

    After `super().initial()`, so authentication, permissions and throttles
    answer first: an anonymous caller still hears 401, not a 400 about a body
    nobody was going to read. A view whose body is not ours to judge (MCP
    answers in JSON-RPC's own words) sets `object_body_required = False`.
    """

    object_body_required = True

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if self.object_body_required:
            guard_object_body(request)
