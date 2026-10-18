"""`GET /api/v2/getPageTitle/?url=` — the title of a page a user links
from a task (MIP-717).

This used to be a plain Django view: no authentication, CSRF-exempt,
`requests.get(url)` following redirects to any address, and `str(e)` in
the response. Anyone on the internet could make the API fetch
`http://<internal service>/` and read back the title or the error,
which made it a port scanner for the private network.

Now: signed-in callers only, per-user throttled, and the fetch goes
through `services.outbound_url.fetch_public` (public addresses only,
pinned to the checked IP, redirects re-checked, standard ports, a body
cap and an overall deadline). Failures all answer the same way, and a
missing title is not an error — the frontend falls back to the URL.
"""

from __future__ import annotations

import html
import logging
import re

from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from origin.services.outbound_url import OutboundUrlError, fetch_public
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

logger = logging.getLogger(__name__)

# A <title> sits in <head>; nothing past the first 256 KB is read.
_MAX_BYTES = 256 * 1024
_TITLE_RE = re.compile(rb"<title[^>]*>(.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
_CHARSET_RE = re.compile(r"charset=([\w-]+)", re.IGNORECASE)
_MAX_TITLE = 300


class PageTitleThrottle(UserRateThrottle):
    """A person adding links stays far under this; a script walking an
    address range does not."""

    rate = "30/min"
    scope = "page_title"


def _title_from(body: bytes, content_type: str) -> str | None:
    match = _TITLE_RE.search(body)
    if not match:
        return None
    charset = _CHARSET_RE.search(content_type or "")
    try:
        text = match.group(1).decode(charset.group(1) if charset else "utf-8", errors="replace")
    except LookupError:
        text = match.group(1).decode("utf-8", errors="replace")
    title = " ".join(html.unescape(text).split())
    return title[:_MAX_TITLE] or None


class PageTitleView(AuthenticatedAPIView):
    throttle_classes = [PageTitleThrottle]

    def get(self, request):
        url = request.query_params.get("url")
        if not url:
            return Response({"error": "Missing 'url' parameter"}, status=400)
        try:
            result = fetch_public(
                url,
                max_bytes=_MAX_BYTES,
                headers={
                    "User-Agent": "Genos-LinkPreview/1",
                    "Accept": "text/html",
                },
            )
        except OutboundUrlError as e:
            # Safe to say: it's about the URL the caller typed, not about
            # what's on the other end.
            return Response({"url": url, "title": None, "error": str(e)}, status=400)
        except Exception:  # noqa: BLE001 — never echo network errors to the client
            logger.info("getPageTitle: fetch failed", exc_info=True)
            return Response({"url": url, "title": None})
        if not 200 <= result.status < 300 or "html" not in result.content_type.lower():
            return Response({"url": url, "title": None})
        return Response({"url": url, "title": _title_from(result.body, result.content_type)})
