"""GIF search proxy for the chat composers' GIF picker.

Wraps the GIPHY API so the key stays server-side and the frontend gets
a stable, minimal shape regardless of upstream's verbosity. Rendering
needs no proxying: the returned media URLs are hotlinked GIPHY CDN
files, which the CSP's `img-src https:` already allows.
"""

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response

from origin.views.common.base_auth_api_view import AuthenticatedAPIView

GIPHY_SEARCH_URL = "https://api.giphy.com/v1/gifs/search"
GIPHY_TRENDING_URL = "https://api.giphy.com/v1/gifs/trending"

DEFAULT_LIMIT = 24
MAX_LIMIT = 50

# GIPHY content rating ceiling. "pg-13" ≈ the "medium" filter tier —
# blocks the explicit buckets without gutting search recall.
RATING = "pg-13"


def _to_int(value, default=0, minimum=0, maximum=None):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < minimum:
        return minimum
    if maximum is not None and n > maximum:
        return maximum
    return n


def _map_gif(item: dict) -> dict | None:
    """GIPHY item -> {id, title, url, previewUrl, width, height}.

    `original` is what gets inserted into the message (full-size GIF);
    `fixed_width` (200px, much lighter) fills the picker grid. Items
    missing either rendition are dropped rather than half-mapped.
    """
    images = item.get("images") or {}
    original = images.get("original") or {}
    preview = images.get("fixed_width") or {}
    url = original.get("url")
    preview_url = preview.get("url") or url
    if not url:
        return None
    return {
        "id": item.get("id"),
        "title": item.get("title") or "GIF",
        "url": url,
        "previewUrl": preview_url,
        "width": _to_int(original.get("width")),
        "height": _to_int(original.get("height")),
    }


class GifSearchView(AuthenticatedAPIView):
    """GET /api/v2/gif/search/?q=&limit=&offset=

    Empty `q` returns GIPHY's trending feed (the picker's initial
    grid). Response: {"results": [...], "next": "<offset>" | ""} —
    `next` is the offset to pass for the following page, empty when
    the listing is exhausted.
    """

    def get(self, request):
        api_key = settings.GIPHY_API_KEY
        if not api_key:
            return Response(
                {"error": "GIF search is not configured."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        query = (request.GET.get("q") or "").strip()
        limit = _to_int(
            request.GET.get("limit"), default=DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT
        )
        offset = _to_int(request.GET.get("offset"), default=0, minimum=0)

        params = {
            "api_key": api_key,
            "limit": limit,
            "offset": offset,
            "rating": RATING,
        }
        if query:
            url = GIPHY_SEARCH_URL
            params["q"] = query
        else:
            url = GIPHY_TRENDING_URL

        # Lazy import mirrors the Tavily tool: keeps module import cheap
        # and makes the upstream call trivially mockable in tests.
        import requests

        try:
            upstream = requests.get(url, params=params, timeout=5)
            upstream.raise_for_status()
            payload = upstream.json()
        except Exception:
            return Response(
                {"error": "GIF search is temporarily unavailable."},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        results = [m for m in (_map_gif(i) for i in payload.get("data") or []) if m]

        pagination = payload.get("pagination") or {}
        count = _to_int(pagination.get("count"))
        total = _to_int(pagination.get("total_count"))
        next_offset = offset + count
        has_more = count > 0 and (total == 0 or next_offset < total)

        return Response(
            {"results": results, "next": str(next_offset) if has_more else ""},
            status=status.HTTP_200_OK,
        )
