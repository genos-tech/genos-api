"""What the model is told about an edit made on its approval card, and about an undo (MIP-889).

Two things happen to an approved write that the model's own transcript does not show:

  * EDIT BEFORE APPROVE. The person changes fields on the card, and the write runs with
    them. The model's replay still holds ITS original call (that is kept on purpose: it is
    replayed with its thought signature), so its final answer can describe the proposal
    rather than what was applied.
  * UNDO. The person puts the write back afterwards. The model's next turn still holds the
    answer that says the write was done.

Behind `SEARCH_ENGINE["AGENT_TELL_MODEL_EDIT_UNDO"]` (default off) the model is told, with
short factual data and no instruction:

  * edit   the tool result carries `user_edits_applied`: `{path: value}` as the person
           typed them (the card's own flat patch), values capped;
  * undo   the replay of the write's result carries `undone_by_user` (`restored` fields and
           `not_restored` fields with their reason code), and the earlier answer in the next
           ask's conversation ends with one bracketed note saying the same.

Everything the notes name is system data: tool names, field names, reason codes. The only
user text is the edit values, which are the person's own words on their own card (capped,
control characters stripped, like a reject reason), not other people's content.

Flag off: every function here returns its input unchanged.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from origin.search_engine.agent import approval_reason

MAX_EDIT_FIELDS = 20
MAX_EDIT_VALUE_CHARS = 300


def enabled() -> bool:
    return bool(settings.SEARCH_ENGINE.get("AGENT_TELL_MODEL_EDIT_UNDO", False))


def _value(value: Any) -> Any:
    if isinstance(value, str):
        return approval_reason.clean(value)[:MAX_EDIT_VALUE_CHARS]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_value(v) for v in list(value)[:MAX_EDIT_FIELDS]]
    return approval_reason.clean(str(value))[:MAX_EDIT_VALUE_CHARS]


def edits_applied(patch: Any) -> dict[str, Any]:
    """The card's edit patch as it goes to the model: capped and cleaned."""
    if not isinstance(patch, dict):
        return {}
    return {str(path)[:80]: _value(v) for path, v in list(patch.items())[:MAX_EDIT_FIELDS]}


def with_edit_note(result: Any, patch: Any) -> Any:
    """`result` plus what the person edited, or `result` itself when off / nothing edited."""
    if not enabled() or not isinstance(result, dict) or not patch:
        return result
    edits = edits_applied(patch)
    return {**result, "user_edits_applied": edits} if edits else result


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
    """The result of a completed step as the model is replayed it: the edit note and the undo
    marker, when the flag is on and they apply. A rejected or failed step has neither."""
    if not enabled() or not isinstance(result, dict) or getattr(step, "error", ""):
        return result
    if getattr(step, "decision", "") == "approve":
        result = with_edit_note(result, getattr(step, "edited_arguments_json", None))
    marker = undone_marker(step)
    if marker is not None and isinstance(result, dict):
        result = {**result, "undone_by_user": marker}
    return result


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
