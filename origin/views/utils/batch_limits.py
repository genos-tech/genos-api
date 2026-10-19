"""Caps on list inputs that cost work per item (MIP-724).

Only for lists the view does something *per element* with — sends an
email, validates and saves a row. A list that just feeds one `id__in`
filter costs a single query however long it is, and capping it would
only add a way for a big team to hit an error, so those are left alone.

The cap is a 400 naming the limit, not a silent truncation: every caller
here writes, and quietly dropping the tail of a write is worse than
refusing it.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

# Each one is an email to an address the caller chose. The invite modal
# adds addresses one chip at a time; 50 is a big onboarding batch.
MAX_INVITE_EMAILS = 50

# One row write per id. A comment mentioning 200 people is already a
# broadcast.
MAX_MENTIONS_PER_COMMENT = 200

# One bulk insert, but per-id rows. Larger than any team today.
MAX_GROUP_MEMBERS_PER_ADD = 1000


def check_batch_size(items, limit: int, field: str) -> Response | None:
    """A 400 when `items` (already known to be a list) is longer than
    `limit`, else None."""
    if len(items) > limit:
        return Response(
            {
                "error": f"{field} takes at most {limit} items per request.",
                "limit": limit,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None
