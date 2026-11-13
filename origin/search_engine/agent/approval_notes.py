"""What the model is told when the person UNDOES an approved write (MIP-889).

The person puts a write back afterwards (`POST .../undo/`). The model's next turn still holds the
answer that says the write was done, and a replay of the run still shows its result, so the model
may speak as if the write stands.

Behind `SEARCH_ENGINE["AGENT_TELL_MODEL_UNDO"]` (default off) the model is told, with short factual
data and no instruction:

  * a replayed result of an undone write carries `undone_by_user` (`restored` fields and
    `not_restored` fields with their reason code);
  * the earlier answer in the next ask's conversation ends with one bracketed note saying the same.

Everything the notes name is system data: tool names, field names, reason codes; no text of the
workspace. (An earlier version also told the model what an approval EDIT applied; it was dropped:
the write tool's own result already carries the applied values.)

Flag off: every function here returns its input unchanged, or nothing.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings


def enabled() -> bool:
    return bool(settings.SEARCH_ENGINE.get("AGENT_TELL_MODEL_UNDO", False))


def undone_marker(step: Any) -> dict[str, Any] | None:
    """`{"restored": [...], "not_restored": [{"field", "reason"}]}` for an undone step."""
    if not enabled() or getattr(step, "undone_at", None) is None:
        return None
    outcome = getattr(step, "undo_outcome_json", None)
    outcome = outcome if isinstance(outcome, dict) else {}
    restored = [str(f)[:40] for f in (outcome.get("restored") or [])][:20]
    skipped = [
        {"field": str(s.get("field", ""))[:40], "reason": str(s.get("reason", ""))[:40]}
        for s in (outcome.get("skipped") or [])
        if isinstance(s, dict)
    ][:20]
    marker: dict[str, Any] = {"restored": restored, "not_restored": skipped}
    if outcome.get("action") == "deleted":
        marker["what_was_created_is_deleted"] = True
    return marker


def with_step_notes(result: Any, step: Any) -> Any:
    """The result of a completed step as the model is replayed it: with the undo marker when the
    flag is on and the step was undone. A rejected or failed step has none."""
    if not enabled() or not isinstance(result, dict) or getattr(step, "error", ""):
        return result
    marker = undone_marker(step)
    if marker is None:
        return result
    return {**result, "undone_by_user": marker}


def undo_note_text(steps: list[Any]) -> str:
    """One bracketed note for an earlier answer whose writes were undone, '' for none."""
    parts: list[str] = []
    for step in steps:
        marker = undone_marker(step)
        if marker is None:
            continue
        what = f"{step.tool_name} (step {step.step_index + 1})"
        if marker.get("what_was_created_is_deleted"):
            part = f"{what}: what it created was deleted"
        elif marker["restored"]:
            part = f"{what}: put back {', '.join(marker['restored'])}"
        else:
            part = f"{what}: undone"
        if marker["not_restored"]:
            part += "; not put back because it changed since: " + ", ".join(
                m["field"] for m in marker["not_restored"]
            )
        parts.append(part)
    if not parts:
        return ""
    return "[Note: after this answer the person undid " + "; ".join(parts) + ".]"
