"""Recognise Django's own "that id is not an id" errors (MIP-1060).

A client-supplied id that reaches an ORM filter raw is parsed by the field: a UUID column
raises `ValidationError("“abc” is not a valid UUID.")`, an integer column raises the
`ValueError` / `TypeError` `Field 'x' expected a number but got 'abc'.` Both mean "the
caller sent something that is not an id": a 400, not a 500.

Only those two messages count. Anything else, including a `ValueError` from our own code, is
not matched and stays a 500, so the guard cannot hide a real bug. Views that already answer
a malformed id in their own words (`as_int` / `as_uuid`, MIP-1014/1029) never get here.
"""

import re

from django.core.exceptions import ValidationError

_INT_FIELD = re.compile(r"^Field '[^']*' expected a number but got ")
_UUID_FIELD = re.compile(r"is not a valid UUID\.$")

MALFORMED_ID_ERROR = "An id in the request is not valid."


def is_malformed_lookup_value(exc: BaseException) -> bool:
    """True for Django's integer-field and UUID-field parse errors, and nothing else."""
    if isinstance(exc, ValidationError):
        return (
            exc.code == "invalid"
            and bool(exc.messages)
            and all(_UUID_FIELD.search(m) for m in exc.messages)
        )
    if isinstance(exc, (ValueError, TypeError)):
        return bool(_INT_FIELD.match(str(exc)))
    return False
