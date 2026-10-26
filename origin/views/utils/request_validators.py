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
