"""Server-owned fields that a request body must not set (MIP-1018).

Several views hand the body to a `fields = "__all__"` ModelSerializer, which
writes any model column the body names. This module is the one rule they
share:

  * a field the client may not set is REFUSED with a 400
    `{"error": "<field> cannot be set."}` when the body carries a value
    other than the one the server would store anyway;
  * a body that carries the server's own value (`is_deleted: false` on a
    create, the stored value on an update) or `null` is accepted, and the
    value is simply never used. A client that echoes a whole object back
    keeps working; one that tries to change a system field is told so.
    This is the same rule as `owner` on a project create (MIP-1016).

Unknown keys are not this module's business: a serializer ignores them.
"""

from rest_framework import status
from rest_framework.response import Response


def _same(sent, stored) -> bool:
    """True if `sent` is `stored` in any way a client plausibly spells it.

    Text-compared, case-insensitively, so a UUID in upper case, `"false"`
    for `False` and `""` for `None` all count as the stored value.
    """

    def text(value):
        return "" if value is None else str(value).strip().lower()

    return sent == stored or text(sent) == text(stored)


def forged_field(body, server_values: dict):
    """The first field of `server_values` that `body` tries to change, else None.

    `server_values` maps each protected field to the value the server holds
    or would store. A key absent from `body`, or sent as `None`, is not an
    attempt.
    """
    for field, stored in server_values.items():
        if field not in body:
            continue
        sent = body.get(field)
        if sent is None or _same(sent, stored):
            continue
        return field
    return None


def refuse_forged_fields(body, server_values: dict):
    """A 400 `Response` naming the first forged field, or None to carry on."""
    field = forged_field(body, server_values)
    if field is None:
        return None
    return Response({"error": f"{field} cannot be set."}, status=status.HTTP_400_BAD_REQUEST)


def stored_values(instance, fields) -> dict:
    """`{field: value held now}` for `fields` of a model `instance`.

    A foreign key reads as the raw id (what a body sends), not the object.
    """
    return {field: instance.serializable_value(field) for field in fields}
