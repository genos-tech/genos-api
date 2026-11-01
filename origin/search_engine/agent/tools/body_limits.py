"""One size cap for every body an agent tool writes (MIP-822).

A note, comment, task or to-do-note body arrives from a model (the in-app
agent) or from outside (an MCP client holding an API key), and nothing bounded
it. The regexes that process a body are linear now, but still cost about a
second per quarter-megabyte, and an unbounded write is its own problem:
storage, search indexing, and the LLM context that reads it back.

`check_body_size` is called at the very top of each write tool's `run`,
before any parsing or markdown conversion, so a refused call costs one
`encode`. MCP dispatches to the same `Tool.run`, so it is covered by the same
call, and the refusal comes back as an `isError` tool result the model can
read and act on.

The unit is UTF-8 BYTES of the raw submitted text (not characters, and not
stripped), so a body of CJK or emoji is measured by what it will occupy, not
by how short it looks. The limit is `settings.AGENT_MAX_BODY_BYTES`, read on
every call so a test or an operator override takes effect without a restart
of this module.
"""

from __future__ import annotations

from typing import Any, Iterable

from django.conf import settings

from origin.search_engine.agent.tools.base import ToolError

DEFAULT_MAX_BODY_BYTES = 262_144  # 256 KB — a proposal, see settings.py


def max_body_bytes() -> int:
    """The configured cap; the default when unset, non-numeric or <= 0."""
    try:
        value = int(getattr(settings, "AGENT_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES))
    except (TypeError, ValueError):
        return DEFAULT_MAX_BODY_BYTES
    return value if value > 0 else DEFAULT_MAX_BODY_BYTES


def _byte_length(text: str) -> int:
    # `surrogatepass`: JSON can carry a lone surrogate ("\\ud800"), which a
    # strict encode refuses with UnicodeEncodeError (a 500, not a refusal).
    return len(text.encode("utf-8", errors="surrogatepass"))


def _too_large(field: str, size: int, limit: int) -> ToolError:
    return ToolError(
        f"`{field}` is too large: {size:,} bytes of UTF-8 were sent, and the limit is "
        f"{limit:,} bytes ({limit // 1024} KB) per body. Nothing was saved. Shorten it, "
        "or split the content across several notes, comments or tasks, and tell the user "
        "if it can't be shortened."
    )


def check_body_size(value: Any, field: str) -> None:
    """Raise a model-readable `ToolError` when `value` is over the cap.

    Anything that is not a string is ignored here: the tool's own argument
    handling owns that error. `field` is the argument name the caller used,
    so the message points at the right place (`tasks[2].content_markdown`).
    """
    if not isinstance(value, str) or not value:
        return
    limit = max_body_bytes()
    size = _byte_length(value)
    if size > limit:
        raise _too_large(field, size, limit)


def check_body_sizes(fields: Iterable[tuple[str, Any]]) -> None:
    """`check_body_size` for each `(field, value)`, plus the SUM of them.

    For a call that carries several bodies (`create_task_plan` takes up to
    twenty tasks): each must fit, and so must the call as a whole, or twenty
    just-under-the-cap bodies would write five megabytes in one approval.
    """
    limit = max_body_bytes()
    total = 0
    for field, value in fields:
        check_body_size(value, field)
        if isinstance(value, str) and value:
            total += _byte_length(value)
    if total > limit:
        raise ToolError(
            f"This call carries {total:,} bytes of body text in total, and the limit is "
            f"{limit:,} bytes ({limit // 1024} KB) per call. Nothing was saved. Send fewer "
            "or shorter bodies per call, and tell the user if it can't be shortened."
        )
