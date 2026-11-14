"""The flag the MIP-578 parity batch shares, and the one thing a tool does with it (MIP-1561).

`SEARCH_ENGINE["AGENT_PARITY_TOOLS"]` (default OFF, `apis/settings.py`) publishes parameters the
app's own forms already have to the in-app agent and to MCP: see `tool_flag_params` for what the
model is shown. This module is the other half, and imports no tool, so a tool can import it:
the tool itself ignores a parameter while the flag is off.

A parameter the declaration does not carry can still arrive: a model that invents one, an MCP
client holding yesterday's tool list, an approval paused before the flag was switched off. With
the flag off the call must do what it did before the parameter existed, and an unknown argument
was always ignored, so `ignore_unless_parity` takes it out of the call.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.conf import settings

#: The one flag the parity batch shares.
PARITY_FLAG = "AGENT_PARITY_TOOLS"


def flag_on(flag: str) -> bool:
    return bool(settings.SEARCH_ENGINE.get(flag))


def parity_on() -> bool:
    return flag_on(PARITY_FLAG)


def ignore_unless_parity(
    args: dict[str, Any],
    names: Iterable[str],
    *,
    rows: tuple[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """`args` as the tool read them before the parity parameters: without `names` (and, for an
    array of objects, without the row keys in `rows = (array name, keys)`), while the flag is off.
    The SAME object comes back when the flag is on or there is nothing to drop."""
    if parity_on():
        return args
    drop = set(names)
    out = {k: v for k, v in args.items() if k not in drop} if drop & set(args) else args
    if rows is not None:
        array_name, keys = rows
        row_keys = set(keys)
        value = out.get(array_name)
        if isinstance(value, list) and any(
            isinstance(row, dict) and row_keys & set(row) for row in value
        ):
            out = {
                **out,
                array_name: [
                    {k: v for k, v in row.items() if k not in row_keys}
                    if isinstance(row, dict)
                    else row
                    for row in value
                ],
            }
    return out
