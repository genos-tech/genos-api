"""What a history row may be SAID to have changed from and to (MIP-2551).

Several rows store ids: an assignee or a reporter is a user UUID, a milestone, sprint, project or
parent task a number, a tag or member custom field the option ids or member UUIDs. A line a person
or a model reads says a NAME or no value at all, never an id:

* a person is named by the rule `get_team_members` applies (`LabelAccess.usernames`);
* a relation is named by the row's snapshot (`metadata.oldLabel` / `newLabel`) after
  `history_labels.HistoryLabels` has cut what this reader may not see. No project passes by itself
  (the rows come from many tasks), only the reader's own memberships and, for a project's name,
  what is not hidden from them;
* a custom field is the caller's to judge (its values are its project's data): here its option
  ids and member UUIDs only become names.

Two queries for the people and `HistoryLabels`' few for the relations, however long the page.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Any

from origin.models.task.task_activity_models import TaskActivityActionType as A
from origin.search_engine.agent.tools.base import ToolContext
from origin.services.history_labels import HistoryLabels

PEOPLE_ACTIONS = (A.ASSIGNEE, A.REPORTER)
RELATION_ACTIONS = (A.MILESTONE_LINK, A.SPRINT_LINK, A.PROJECT, A.PARENT)


def _set(value: Any) -> bool:
    return value not in (None, "", [])


def _listed(value: Any) -> list:
    return value if isinstance(value, list) else [value]


class HistoryWords:
    """The names for one page of rows (`TaskActivity` rows, or anything with their columns).

    `viewer_id` is whose memberships decide a relation's name: None in a chat other people read,
    where only what everyone may see is named."""

    def __init__(self, ctx: ToolContext, rows: Iterable, *, viewer_id: Any):
        rows = list(rows)
        wanted = {
            str(v)
            for r in rows
            if r.action_type in PEOPLE_ACTIONS
            for v in (r.old_value, r.new_value)
            if _set(v)
        }
        wanted |= {
            str(v)
            for r in rows
            if r.action_type == A.CUSTOM_FIELD and (r.metadata or {}).get("fieldType") == "member"
            for value in (r.old_value, r.new_value)
            if _set(value)
            for v in _listed(value)
        }
        if wanted:
            # Imported here: `label_acl` imports `tools.base`, which runs the tools package.
            from origin.search_engine.agent.label_acl import LabelAccess  # noqa: PLC0415

            self._people = LabelAccess(ctx).usernames(wanted)
        else:
            self._people = {}
        # Copies: the cut is in memory, and the caller goes on using its own rows.
        relations = [copy.copy(r) for r in rows if r.action_type in RELATION_ACTIONS]
        if relations:
            labels = HistoryLabels(relations, viewer_id=viewer_id or None, task_project_id=None)
            for row in relations:
                labels.cut(row)
        self._relations = {r.pk: r.metadata or {} for r in relations}

    def values(self, row) -> tuple[str | None, str | None] | None:
        """`(old, new)` of `row` in words, a side None when it was empty. None when a value that
        was set has no name this reader may be given: say what changed, with no values."""
        out = []
        for side, value in (("old", row.old_value), ("new", row.new_value)):
            if not _set(value):
                out.append(None)
                continue
            word = self._word(row, side, value)
            if word is None:
                return None
            out.append(word)
        return out[0], out[1]

    def _word(self, row, side: str, value: Any) -> str | None:
        if row.action_type in PEOPLE_ACTIONS:
            return self._people.get(str(value))
        if row.action_type in RELATION_ACTIONS:
            label = self._relations.get(row.pk, {}).get(f"{side}Label")
            return str(label) if label else None
        if row.action_type == A.CUSTOM_FIELD:
            return self._custom(row.metadata or {}, side, value)
        return None

    def _custom(self, metadata: dict, side: str, value: Any) -> str | None:
        kind = metadata.get("fieldType")
        values = _listed(value)
        if kind == "member":
            names = [self._people.get(str(v)) for v in values]
        elif kind == "tag":
            labels = _listed(metadata.get(f"{side}Label"))
            if len(labels) != len(values):
                return None
            # An option deleted before the row was written kept its id as its label.
            names = [
                str(label) if label and label != str(v) else None
                for label, v in zip(labels, values)
            ]
        else:
            names = [str(v) for v in values if isinstance(v, (str, int, float))]
            if len(names) != len(values):
                return None
        return ", ".join(names) if all(names) else None
