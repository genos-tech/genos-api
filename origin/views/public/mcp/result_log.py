"""What an MCP tool answer weighs, for the log (MIP-1367, phase 1).

Nothing here acts on a result. It only records, so a cap on result size can be chosen from
measurements instead of guesses:

* the tool's name and the size of its answer go on the request's `perf.request` line
  (`origin/middleware/request_perf.py`), beside `bytes=` (the response's wire bytes):

      perf.request ... bytes=4558171 tool=get_task result_chars=2276734

  `result_chars` is the length of the serialized TEXT content, the part a client puts into the
  model's context. It is `len()` of the string `tools.call` has already built, so nothing is
  serialized a second time.
* a SHADOW counter: when `result_chars` is above `MCP_RESULT_SHADOW_CAP_CHARS` (default 24,000,
  env override), one INFO line says so, and nothing else happens. The caller gets the same
  answer it always got. This is the number a future cap would use, watched before it is enforced:

      mcp.result_over_shadow_cap tool=get_task result_chars=2276734 cap=24000

Tool names and sizes only. Never an argument, a result, an id, a user, a team or an address; and a
name is logged only when it has the shape of a registered tool's, whatever the client sent.

Nothing here may fail a call: every step is guarded, and the answer is returned either way.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_SHADOW_CAP_CHARS = 24_000


def _env_int(name: str, default: int) -> int:
    """A positive integer from the environment; anything else is the default."""
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


MCP_RESULT_SHADOW_CAP_CHARS = _env_int("MCP_RESULT_SHADOW_CAP_CHARS", DEFAULT_SHADOW_CAP_CHARS)

_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def text_chars(result: dict[str, Any]) -> int:
    """Characters of the text content of a `CallToolResult`: the sum over its text items."""
    total = 0
    for item in result.get("content") or ():
        text = item.get("text") if isinstance(item, dict) else None
        if isinstance(text, str):
            total += len(text)
    return total


def note_result(request, tool_name: str, result: dict[str, Any]) -> None:
    """Stash the tool and its answer's size on the request for the perf line, and log the
    shadow line when the answer is over the proposed cap. Never raises."""
    try:
        if not isinstance(tool_name, str) or not _TOOL_NAME.fullmatch(tool_name):
            return
        chars = text_chars(result)
        target = getattr(request, "_request", request)
        target.mcp_tool = tool_name
        target.mcp_result_chars = chars
        if chars > MCP_RESULT_SHADOW_CAP_CHARS:
            log.info(
                "mcp.result_over_shadow_cap tool=%s result_chars=%d cap=%d",
                tool_name,
                chars,
                MCP_RESULT_SHADOW_CAP_CHARS,
            )
    except Exception:  # noqa: BLE001 - a log line must never fail a call
        pass
