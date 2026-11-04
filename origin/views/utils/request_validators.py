import uuid

from rest_framework import status
from rest_framework.response import Response

"""
HTTP_200_OK
HTTP_201_CREATED
HTTP_202_ACCEPTED
HTTP_203_NON_AUTHORITATIVE_INFORMATION
HTTP_204_NO_CONTENT
HTTP_205_RESET_CONTENT
HTTP_206_PARTIAL_CONTENT
HTTP_207_MULTI_STATUS
HTTP_208_ALREADY_REPORTED
HTTP_226_IM_USED

HTTP_400_BAD_REQUEST
HTTP_401_UNAUTHORIZED
HTTP_402_PAYMENT_REQUIRED
HTTP_403_FORBIDDEN
HTTP_404_NOT_FOUND
HTTP_405_METHOD_NOT_ALLOWED
HTTP_406_NOT_ACCEPTABLE
HTTP_407_PROXY_AUTHENTICATION_REQUIRED
HTTP_408_REQUEST_TIMEOUT
HTTP_409_CONFLICT
HTTP_410_GONE
HTTP_411_LENGTH_REQUIRED
HTTP_412_PRECONDITION_FAILED
HTTP_413_REQUEST_ENTITY_TOO_LARGE
HTTP_414_REQUEST_URI_TOO_LONG
HTTP_415_UNSUPPORTED_MEDIA_TYPE
HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE
HTTP_417_EXPECTATION_FAILED
HTTP_421_MISDIRECTED_REQUEST
HTTP_422_UNPROCESSABLE_ENTITY
HTTP_423_LOCKED
HTTP_424_FAILED_DEPENDENCY
HTTP_425_TOO_EARLY
HTTP_426_UPGRADE_REQUIRED
HTTP_428_PRECONDITION_REQUIRED
HTTP_429_TOO_MANY_REQUESTS
HTTP_431_REQUEST_HEADER_FIELDS_TOO_LARGE
HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS
"""


def validate_request_data(params: dict) -> None:
    for key, val in params.items():
        if val is None:
            return Response(
                {"error": f"{key} is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    return None


def validate_request_user(requested_user_id: str, target_user_id: str) -> None:
    if requested_user_id != target_user_id:
        return Response(
            {"message": "Only the data owner can request."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def is_own_user_id(value, user) -> bool:
    """True if `value` names `user`, in any spelling of their UUID.

    For a create that takes its owner from `request.user` but still accepts
    the id the client sends: parsed rather than compared as text, so an
    upper-case or unhyphenated copy of the caller's own id is still theirs.
    Anything that is not a UUID at all (a name, a number, a list) is not.
    """
    try:
        return uuid.UUID(str(value)) == uuid.UUID(str(user.id))
    except (AttributeError, TypeError, ValueError):
        return False


def body_object(request) -> dict:
    """The request body as a dict: `request.data` when it is one, else `{}`.

    A view that reads its body with `data.get(...)` crashes (a 500) on a JSON body that
    is not an object: `[1]`, `"abc"`, `5`, `true`. `request.data or {}` covered only the
    empty ones (null, `[]`, `0`, `""`, `false`, which read as no body at all).

    A body that is not an object is read as an empty one, so it gets what a missing field
    gets: the view's own "is required" 400, in the view's own words. Anything that was
    already an answer keeps it, null included. A form post is a QueryDict, which is a
    dict, and passes through.
    """
    data = request.data
    return data if isinstance(data, dict) else {}


def as_uuid(value):
    """A client-supplied id as a `uuid.UUID`, or None if it isn't one.

    A `UUIDField` handed a string that is not a UUID raises `ValidationError`
    from `get_prep_value`: an uncaught 500, not the answer a malformed request
    deserves. It happens in normal use: the web client renders an optimistic
    echo keyed by its `corr-<random>` correlation id, and a mark-read that
    fires before the server ack forwards that placeholder. Returns None rather than a Response so each view keeps its own
    wording: where a view already answers "not found" for an id, a malformed
    id gets that same answer (it names nothing either), else its own 400.
    """
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def as_int(value):
    """A client-supplied integer (an int, or text that is one), or None.

    `int()` alone would take `True` as 1 and `1.9` as 1, and raise on
    anything else; this answers None for all of those, so the view can say
    so in its own words.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def missing_fields(body: dict, *fields: str) -> list[str]:
    """The names in `fields` that `body` does not carry, in the order given.

    PRESENCE, not value: `null` is a value. A task's `due_date` is sent as `null` for
    "no date", so a check that refused `None` (like `validate_request_data`) would turn that
    valid create into a 400. This is for a view that read the field as `request.data[...]`
    and so answered a KeyError (a 500) when the key was missing: it now asks first.
    """
    return [field for field in fields if field not in body]


def _names(fields: list[str]) -> str:
    return fields[0] if len(fields) == 1 else f"{', '.join(fields[:-1])} and {fields[-1]}"


def require_fields(body: dict, *fields: str, objects: tuple = (), message: str | None = None):
    """A 400 `Response` if `body` lacks a field, or an `objects` field is not an object, else None.

    One check for the required fields of a body a view reads with `body[...]` afterwards:

      * a key in `fields` or `objects` that is absent is "<names> is/are required." (or
        `message`, when the view has words of its own that a test or a client already
        knows);
      * a key in `objects` that is present but not a JSON object (`null`, a string, a list, a
        number) is "<name> must be an object.", for a field the view reads a key out of
        (`item_optionals["project_id"]` raises a TypeError, not a KeyError, on a string).

    Presence only otherwise: a `null` in `fields` passes, as it did when the read was
    `request.data["x"]`. Nothing is written here; call it before anything is.
    """
    if absent := missing_fields(body, *fields, *objects):
        text = message or f"{_names(absent)} {'is' if len(absent) == 1 else 'are'} required."
        return Response({"error": text}, status=status.HTTP_400_BAD_REQUEST)
    for field in objects:
        if not isinstance(body[field], dict):
            return Response(
                {"error": f"{field} must be an object."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    return None


def refuse_non_object_body(request):
    """A 400 `Response` if the body is JSON but not an object, else None.

    For a view where an EMPTY body is a request in its own right: a mark-all-read, a
    patch with nothing to change, a billing-portal link. `body_object` would read `[1]`
    as `{}` there and the view would do what it does for an empty body, as if the
    person had sent nothing. This refuses it instead, so a view that takes it reads
    `body_object(request)` only after this has passed.

    The empty bodies (null, `[]`, `0`, `""`, `false`) are still "no body", as they are
    for `body_object`: only a truthy value that is not an object is refused.
    """
    data = request.data
    if data and not isinstance(data, dict):
        return Response(
            {"error": "Request body must be a JSON object."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None


def refuse_non_string_fields(data: dict, *fields: str):
    """A 400 `Response` naming the first of `fields` that is present and not a string, else None.

    For a text field the view reads with `(data.get(f) or "").strip()`, which is an
    `AttributeError` for a truthy value that is not text. Empty-ish values (null, `0`,
    `false`, `[]`, `{}`) mean "not sent" and are left to the view's own "is required".
    """
    for field in fields:
        if data.get(field) and not isinstance(data[field], str):
            return Response(
                {"error": f"{field} must be a string."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    return None


# Schemes a task or milestone link may use. Anything else (`javascript:`, `data:`, `vbscript:`,
# `file:`…) is stored on the task and rendered by the app as `<a href=…>`, and a
# `javascript:` href runs in the app's origin, with the clicker's session, when a
# teammate clicks a link titled "Design doc" (CodeQL js/xss-through-dom, MIP-821).
# The browser's own check was `new URL(x)`, which accepts every scheme.
SAFE_LINK_SCHEMES = ("http", "https")


def link_scheme(url: str) -> str:
    """The scheme a BROWSER would act on for `url`, lowercased, or "".

    Browsers strip leading C0 control characters and spaces, and remove tab,
    newline and carriage return anywhere in the URL, before reading the scheme.
    So `"  JaVa\tScRiPt:alert(1)"` runs as `javascript:`. Reading the scheme the
    way the browser does is the whole defence: a check on the raw string is what
    these tricks beat.
    """
    cleaned = "".join(ch for ch in url if ch not in "\t\n\r").lstrip(
        "".join(chr(c) for c in range(0x21))
    )
    head, sep, _ = cleaned.partition(":")
    if not sep:
        return ""
    # A scheme is ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ). Anything else before
    # the first colon (a "/", "?", "#") means there is no scheme: a relative URL.
    if not head or not head[0].isascii() or not head[0].isalpha():
        return ""
    if not all(c.isascii() and (c.isalnum() or c in "+-.") for c in head):
        return ""
    return head.lower()


def refuse_unsafe_link_urls(links):
    """A 400 `Response` if any link's URL has a scheme other than http(s), else None.

    `links` is whatever the client sent: a list of dicts with a `url`, `[]`, or
    nothing. Shapes this does not understand are left alone (it only refuses a
    dangerous scheme; it does not police the payload), and a URL with no scheme at
    all is left alone too, since a relative href is not a script risk.
    """
    if not isinstance(links, (list, tuple)):
        return None
    for link in links:
        if not isinstance(link, dict):
            continue
        url = link.get("url")
        if not isinstance(url, str):
            continue
        scheme = link_scheme(url)
        if scheme and scheme not in SAFE_LINK_SCHEMES:
            return Response(
                {"error": "A link must start with http:// or https://."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    return None
