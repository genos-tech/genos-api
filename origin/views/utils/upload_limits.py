"""Per-tier per-file upload size enforcement.

One helper shared by every attachment endpoint (chat message, channel
inline editor, task attachments/body images, note attachments). The
user's tier sets `upload_max_mb` in SEARCH_ENGINE["TIER_QUOTAS"];
while that's `None` (unlimited / shipped-dark), each endpoint falls
back to its own historical cap — the chat surfaces keep their flat
25 MiB, everything else gets the absolute ceiling below (those
endpoints previously had NO size check at all, so the ceiling is a
strict safety improvement, not a behavior change).

Response: HTTP 413 with the same `limit_reached` payload the quota
429s use, so the frontend can treat both as "plan limit hit".
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from origin.search_engine.quota import get_upload_max_bytes

# Absolute per-file ceiling, applied when neither the tier nor the
# endpoint supplies a limit. Matches the enterprise target value —
# nothing in the app needs a bigger single file, and the storage
# backend is a mounted disk today.
ABSOLUTE_MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MiB


def check_upload_size(user, file, *, fallback_bytes: int | None = None) -> Response | None:
    """Return a 413 Response when `file` exceeds the user's per-file
    limit, else None.

    Limit resolution: tier `upload_max_mb` → `fallback_bytes` (the
    endpoint's historical cap) → `ABSOLUTE_MAX_UPLOAD_BYTES`.
    `get_upload_max_bytes` fails open (returns None on infra errors),
    so an outage degrades to the fallback, never to a block.
    """
    limit = get_upload_max_bytes(user.id)
    if limit is None:
        limit = fallback_bytes if fallback_bytes is not None else ABSOLUTE_MAX_UPLOAD_BYTES
    if file.size <= limit:
        return None
    limit_mb = limit // (1024 * 1024)
    return Response(
        {
            "error": (
                f"File exceeds the {limit_mb} MB per-file limit for your plan. "
                "Upgrade your plan to upload larger files."
            ),
            "limit_reached": True,
            "category": "upload_size",
            "limit_mb": limit_mb,
        },
        status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
    )
