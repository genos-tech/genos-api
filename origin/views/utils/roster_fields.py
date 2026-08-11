"""Per-reader projections of roster fields — the one place a "who may see
this field" rule for a member row lives, so the several roster-building
views can't drift on it.

Sibling in spirit to `_without_phone` in team_views: both narrow what a
built member row discloses to a particular reader.
"""


def shown_location(value, location_shared, is_self) -> str:
    """A location field's value for a given reader, honouring the opt-out.

    `current_location` / `timezone` ride every roster row, but a user can
    turn OFF sharing their location (`CustomUser.location_shared`). When
    they have, this blanks the field for everyone EXCEPT the owner — you
    always see your own card. Only the DISPLAY is gated: the stored zone is
    untouched, so server-side date-boundary math still works and flipping
    the toggle back on restores the row with nothing to re-collect.
    """
    if is_self or location_shared:
        return value or ""
    return ""
