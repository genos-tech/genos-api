"""What an automation rule may say, who it may run as, and whether it matches a task (MIP-2498).

The guards live here once and are asked twice: when a rule is saved and again each time it runs,
because members, privacy and projects change in between. A guard answers with a short reason code,
the same code the API refuses with and the task's history records.

**Privacy.** A rule must never move data to people who could not see it. `potential_readers` is
everyone who can open a project now or can let themselves in (a public project: anyone on its
team), and a move is refused when the destination has any such person the source does not. The
sub-tasks, notes, chat card and comments that travel with a task all live in the source project, so
the same comparison covers them.

**Owner.** A rule runs with its owner's rights, and its owner is whoever saved it last, so nobody
can edit their way into another person's reach.
"""

from __future__ import annotations

import logging
import uuid

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import (
    ProjectCustomField,
    ProjectMaster,
    ProjectMembers,
    ProjectStatus,
    ProjectTags,
)
from origin.models.task.automation_models import AutomationRule, AutomationTrigger
from origin.models.task.milestone_models import MilestoneMaster
from origin.services.member_roles import can_manage, resolve_project_role
from origin.views.utils.scope_guards import is_project_member, is_team_member

log = logging.getLogger(__name__)

#: What a rule acts on. A milestone rule moves the milestone with its whole sub-tree (MIP-2521).
APPLIES_TO_TASK = "task"
APPLIES_TO_MILESTONE = "milestone"
APPLIES_TO = (APPLIES_TO_TASK, APPLIES_TO_MILESTONE)
#: Rules one project may hold.
MAX_RULES = 50
MAX_CONDITIONS = 10
#: Statuses a rule may set. "Deleted" is a tombstone, never a rule's action.
SETTABLE_STATUSES = ("Open", "WIP", "Blocked", "Pending", "Closed", "Rejected")
PRIORITIES = ("Minimal", "Low", "Normal", "High", "Critical")
CONDITION_FIELDS = (
    "tag",
    "assignee",
    "reporter",
    "priority",
    "milestone",
    "custom_field",
    "custom_status",
)
CONDITION_OPS = ("is", "is_not", "is_set", "is_not_set")

#: Reasons that mean the rule itself can no longer run, so the engine turns it off.
OWNER_PROBLEMS = frozenset(
    {
        "owner_gone",
        "owner_not_member",
        "owner_not_on_source",
        "owner_not_manager",
        "owner_not_on_destination",
    }
)

#: Reasons that say something about the destination, which a reader of the source may not be on.
_DESTINATION_REASONS = frozenset(
    {
        "destination_not_found",
        "destination_archived",
        "same_project",
        "private_to_public",
        "readers_outside_source",
        "owner_not_on_destination",
        "assignee_not_on_destination",
        "move_not_allowed",
    }
)


def public_reason(code: str) -> str:
    """`code` as it may be told to somebody who may not manage the rule or see its destination:
    in a task's history, and to a manager who is not on the destination."""
    if code in _DESTINATION_REASONS:
        return "destination"
    return "owner" if code in OWNER_PROBLEMS else code


MESSAGES = {
    "owner_gone": "The rule has no owner any more. Save it again to run it as you.",
    "owner_not_member": "The rule's owner must be a member of the team.",
    "owner_not_on_source": "The rule's owner must be a member of this project.",
    "owner_not_manager": "The rule's owner must be this project's owner or an editor.",
    "owner_not_on_destination": "Destination project not found.",
    "destination_not_found": "Destination project not found.",
    "destination_archived": "The destination project is archived.",
    "same_project": "The destination must be another project.",
    "private_to_public": "A private project's tasks cannot move to a public project.",
    "readers_outside_source": (
        "The destination can be read by people who cannot read this project, so tasks cannot "
        "move there."
    ),
}


class RuleError(Exception):
    def __init__(self, message: str, *, code: str, field: str | None = None, status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.field = field
        self.status = status


def guard_error(code: str, field: str | None = None) -> RuleError:
    # A destination the owner is not on is answered as one that does not exist.
    public = "destination_not_found" if code == "owner_not_on_destination" else code
    return RuleError(MESSAGES.get(public, "This rule is not allowed."), code=public, field=field)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def plan_allows(user_id) -> bool:
    """Automation rules are Core and up (Ken, 2026-11-28). The effective tier decides (their own
    or their teams' plans). Fails open on an infrastructure error, as the other plan gates do.

    Not a `TIER_QUOTAS` key: `TIER_QUOTAS_JSON` replaces the whole table in production, so a key
    the override does not know would read as allowed.
    """
    from origin.search_engine import quota  # noqa: PLC0415 - search_engine is heavy

    try:
        return quota.get_effective_tier(str(user_id)) != "free"
    except Exception:  # noqa: BLE001 - a lookup outage must not stop a paying member's rules
        log.exception("automation plan check failed for user=%s", user_id)
        return True


def owner_problem(owner, source: ProjectMaster, destination: ProjectMaster | None) -> str | None:
    """Why `owner` may not run a rule of `source` that ends in `destination`, or None."""
    if owner is None or not owner.is_active:
        return "owner_gone"
    if not is_team_member(source.team_id, owner.id):
        return "owner_not_member"
    if not is_project_member(source.project_id, owner.id, live_only=True):
        return "owner_not_on_source"
    if not can_manage(resolve_project_role(source, owner.id)):
        return "owner_not_manager"
    if destination is not None and not is_project_member(
        destination.project_id, owner.id, live_only=True
    ):
        return "owner_not_on_destination"
    return None


def potential_readers(project: ProjectMaster) -> set[str]:
    """Everyone who can read `project` now or can join it by themselves."""
    people = {
        str(pk)
        for pk in ProjectMembers.objects.filter(
            project_id=project.project_id, attendee_id__isnull=False
        ).values_list("attendee_id", flat=True)
    }
    if project.owner_id:
        people.add(str(project.owner_id))
    if not project.is_private and project.team_id:
        people |= {
            str(pk)
            for pk in TeamMembers.objects.filter(
                team_id=project.team_id, is_deleted=False, attendee_id__isnull=False
            ).values_list("attendee_id", flat=True)
        }
        team_owner = (
            TeamMaster.objects.filter(team_id=project.team_id)
            .values_list("owner_id", flat=True)
            .first()
        )
        if team_owner:
            people.add(str(team_owner))
    return people


def destination_problem(source: ProjectMaster, destination: ProjectMaster) -> str | None:
    """Why tasks of `source` may not move to `destination`, whoever asks."""
    if destination.project_id == source.project_id:
        return "same_project"
    if destination.is_deleted or str(destination.team_id or "") != str(source.team_id or ""):
        return "destination_not_found"
    if destination.archived_at is not None:
        return "destination_archived"
    if source.is_private and not destination.is_private:
        return "private_to_public"
    if potential_readers(destination) - potential_readers(source):
        return "readers_outside_source"
    return None


def assignee_allowed(project: ProjectMaster, user_id) -> bool:
    """May a rule put this person on a task of `project`? Only somebody who could open the
    project anyway: assigning grants reading, and a rule does it to tasks nobody is looking at,
    so in a private project it takes a member (stricter than assigning by hand)."""
    return str(user_id) in potential_readers(project)


# ---------------------------------------------------------------------------
# Matching a task or a milestone
# ---------------------------------------------------------------------------


class MilestoneSubject:
    """What the triggers and conditions read of a milestone: its OWN values (`MilestoneMaster`
    and its assignees), never the backing task row's mirror. The one thing that lives on the
    backing row only is its custom field values."""

    milestone_id = None  # "milestone is X" is a task condition; a milestone is in none
    assignee_id = None

    def __init__(self, milestone, backing, assignee_ids):
        self.tags = milestone.tags
        self.status = milestone.status
        self.custom_status_id = milestone.custom_status_id
        self.priority = milestone.priority
        self.reporter_id = milestone.reporter_id
        self.assignee_ids = frozenset(str(i) for i in assignee_ids)
        self.custom_field_values = backing.custom_field_values if backing else None


def tag_names_of(task) -> list[str]:
    tags = task.tags if isinstance(task.tags, list) else []
    return [str(t.get("tagName")) for t in tags if isinstance(t, dict) and t.get("tagName")]


def catalogue(project_id) -> dict[str, dict]:
    """The project's tags as `{casefolded name: stored tag object}`; the newest wins a clash of
    case, an exact name is looked up first by `find_tag`."""
    rows = ProjectTags.objects.filter(project_id=project_id).order_by("ts_updated_at")
    return {
        row.tag_name.casefold(): {
            "tagName": row.tag_name,
            "tagColor": row.tag_color,
            "tagTextColor": row.tag_text_color,
        }
        for row in rows
    }


def find_tag(project_id, name: str, folded: dict | None = None) -> dict | None:
    """The tag of `project_id` called `name`: exact first, else the one case-insensitive match."""
    row = ProjectTags.objects.filter(project_id=project_id, tag_name=name).first()
    if row is not None:
        return {
            "tagName": row.tag_name,
            "tagColor": row.tag_color,
            "tagTextColor": row.tag_text_color,
        }
    return (folded if folded is not None else catalogue(project_id)).get(name.casefold())


def trigger_matches(rule: AutomationRule, kind: str, payload: dict, task) -> bool:
    """Did this change (`kind`, `payload`) set off `rule`, and does it still hold on the task?"""
    if rule.trigger_type != kind:
        return False
    value = rule.trigger_value or {}
    if kind == AutomationTrigger.CREATED:
        return True
    if kind == AutomationTrigger.STATUS_CHANGED:
        if "custom_status_id" in value:
            wanted = value["custom_status_id"]
            return payload.get("custom_status_to") == wanted and task.custom_status_id == wanted
        return payload.get("status_to") == value.get("status") == task.status
    if kind == AutomationTrigger.TAG_ADDED:
        wanted = str(value.get("tag", "")).casefold()
        return wanted in {t.casefold() for t in payload.get("tags_added") or []} and wanted in {
            t.casefold() for t in tag_names_of(task)
        }
    wanted = value.get("assignee_id")
    if wanted is None:
        return True
    ids = getattr(task, "assignee_ids", None)
    if ids is not None:  # a milestone, which several people hold
        return wanted in (payload.get("assignees_added") or []) and wanted in ids
    return payload.get("assignee_to") == wanted and str(task.assignee_id) == wanted


def trigger_state_holds(rule: AutomationRule, task) -> bool:
    """`trigger_matches` without a change to look at: is the task in the state the trigger names?
    What the dry run asks of the tasks that exist now."""
    value = rule.trigger_value or {}
    kind = rule.trigger_type
    if kind == AutomationTrigger.STATUS_CHANGED:
        if "custom_status_id" in value:
            return task.custom_status_id == value["custom_status_id"]
        return task.status == value.get("status")
    if kind == AutomationTrigger.TAG_ADDED:
        wanted = str(value.get("tag", "")).casefold()
        return wanted in {t.casefold() for t in tag_names_of(task)}
    if kind == AutomationTrigger.ASSIGNEE_CHANGED:
        wanted = value.get("assignee_id")
        # "Any change" includes unassigning, as the live trigger does.
        ids = getattr(task, "assignee_ids", None)
        if ids is not None:
            return wanted is None or wanted in ids
        return wanted is None or str(task.assignee_id) == wanted
    return True


class ProjectRefs:
    """What of a project a rule may point at, read once. A condition that names something the
    project no longer has is false, whatever its operator: "is not tag X" must not start matching
    everything because X was renamed."""

    def __init__(self, project_id):
        self.tags = set(catalogue(project_id))
        self.milestones = set(
            MilestoneMaster.objects.filter(project_id=project_id, is_deleted=False).values_list(
                "milestone_id", flat=True
            )
        )
        self.fields = set(
            ProjectCustomField.objects.filter(project_id=project_id).values_list(
                "field_id", flat=True
            )
        )
        self.statuses = set(
            ProjectStatus.objects.filter(project_id=project_id).values_list("status_id", flat=True)
        )


def _compare(op: str, present: bool, equals: bool) -> bool:
    if op == "is_set":
        return present
    if op == "is_not_set":
        return not present
    if op == "is":
        return equals
    return not equals


def condition_holds(condition: dict, task, refs: ProjectRefs) -> bool:
    field, op, value = condition.get("field"), condition.get("op"), condition.get("value")
    needs_value = op in ("is", "is_not")
    if field == "tag":
        names = {t.casefold() for t in tag_names_of(task)}
        if needs_value and str(value).casefold() not in refs.tags:
            return False
        return _compare(op, bool(names), str(value).casefold() in names)
    if field == "assignee" and (ids := getattr(task, "assignee_ids", None)) is not None:
        return _compare(op, bool(ids), value in ids)
    if field in ("assignee", "reporter"):
        held = getattr(task, f"{field}_id")
        return _compare(op, held is not None, str(held) == value)
    if field == "priority":
        return _compare(op, bool(task.priority), task.priority == value)
    if field == "milestone":
        if needs_value and value not in refs.milestones:
            return False
        return _compare(op, task.milestone_id is not None, task.milestone_id == value)
    if field == "custom_status":
        if needs_value and value not in refs.statuses:
            return False
        return _compare(op, task.custom_status_id is not None, task.custom_status_id == value)
    if field == "custom_field":
        field_id = condition.get("field_id")
        if field_id not in refs.fields:
            return False
        held = (task.custom_field_values or {}).get(str(field_id))
        present = held not in (None, "", [])
        if isinstance(held, list):
            equals = value in [str(item) for item in held]
        else:
            equals = present and str(held) == value
        return _compare(op, present, equals)
    return False


def conditions_hold(rule: AutomationRule, task, refs: ProjectRefs) -> bool:
    return all(condition_holds(c, task, refs) for c in rule.conditions or [])


# ---------------------------------------------------------------------------
# Validating what a person saves
# ---------------------------------------------------------------------------


def _int(raw, *, code: str, field: str) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool) and 0 < raw < 2**63:
        return raw
    if isinstance(raw, str) and raw.isascii() and raw.isdigit() and 0 < int(raw) < 2**63:
        return int(raw)
    raise RuleError(f"{field} must be an id.", code=code, field=field)


def _uuid(raw, *, code: str, field: str) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, TypeError, AttributeError):
        raise RuleError(f"{field} must be a user id.", code=code, field=field) from None


def _clean_trigger(project: ProjectMaster, kind, value) -> tuple[str, dict]:
    bad = "invalid_trigger"
    if kind not in AutomationTrigger.values:
        raise RuleError("Unknown trigger.", code=bad, field="trigger_type")
    value = value if value is not None else {}
    if not isinstance(value, dict):
        raise RuleError("trigger_value must be an object.", code=bad, field="trigger_value")
    if kind == AutomationTrigger.CREATED:
        return kind, {}
    if kind == AutomationTrigger.STATUS_CHANGED:
        if value.get("custom_status_id") is not None:
            status_id = _int(value["custom_status_id"], code=bad, field="trigger_value")
            if not ProjectStatus.objects.filter(project=project, status_id=status_id).exists():
                raise RuleError(
                    "Not a status of this project.", code="unknown_status", field="trigger_value"
                )
            return kind, {"custom_status_id": status_id}
        if value.get("status") not in SETTABLE_STATUSES:
            raise RuleError("Unknown status.", code="unknown_status", field="trigger_value")
        return kind, {"status": value["status"]}
    if kind == AutomationTrigger.TAG_ADDED:
        tag = value.get("tag")
        found = find_tag(project.project_id, tag) if isinstance(tag, str) else None
        if found is None:
            raise RuleError("Not a tag of this project.", code="unknown_tag", field="trigger_value")
        return kind, {"tag": found["tagName"]}
    if value.get("assignee_id") in (None, ""):
        return kind, {}
    return kind, {"assignee_id": _uuid(value["assignee_id"], code=bad, field="trigger_value")}


def _clean_conditions(project: ProjectMaster, raw, applies_to: str) -> list[dict]:
    bad = "invalid_condition"
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_CONDITIONS:
        raise RuleError(
            f"conditions must be a list of at most {MAX_CONDITIONS}.", code=bad, field="conditions"
        )
    refs = ProjectRefs(project.project_id)
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise RuleError("Each condition is an object.", code=bad, field="conditions")
        field, op = item.get("field"), item.get("op", "is")
        if field not in CONDITION_FIELDS or op not in CONDITION_OPS:
            raise RuleError("Unknown condition.", code=bad, field="conditions")
        if field == "milestone" and applies_to == APPLIES_TO_MILESTONE:
            raise RuleError(
                "A milestone is not in a milestone: that condition is for task rules.",
                code=bad,
                field="conditions",
            )
        clean = {"field": field, "op": op}
        if field == "custom_field":
            field_id = _int(item.get("field_id"), code=bad, field="conditions")
            if field_id not in refs.fields:
                raise RuleError("Not a field of this project.", code=bad, field="conditions")
            clean["field_id"] = field_id
        if op in ("is", "is_not"):
            value = item.get("value")
            if field == "tag":
                if not isinstance(value, str) or value.casefold() not in refs.tags:
                    raise RuleError(
                        "Not a tag of this project.", code="unknown_tag", field="conditions"
                    )
            elif field in ("assignee", "reporter"):
                value = _uuid(value, code=bad, field="conditions")
            elif field == "priority":
                if value not in PRIORITIES:
                    raise RuleError("Unknown priority.", code=bad, field="conditions")
            elif field in ("milestone", "custom_status"):
                value = _int(value, code=bad, field="conditions")
                if value not in (refs.milestones if field == "milestone" else refs.statuses):
                    raise RuleError(f"Not a {field} of this project.", code=bad, field="conditions")
            elif not isinstance(value, str) or not value or len(value) > 200:
                raise RuleError("value must be text.", code=bad, field="conditions")
            clean["value"] = value
        out.append(clean)
    return out


def clean_rule(project: ProjectMaster, data: dict, *, owner) -> dict:
    """The model fields of the rule `data` describes (the whole definition, not a patch), judged
    for `owner`, who will run it. Raises `RuleError`."""
    name = data.get("name")
    name = " ".join(name.split()) if isinstance(name, str) else ""
    if not name or len(name) > 80:
        raise RuleError("name is required, at most 80 characters.", code="bad_name", field="name")
    applies_to = data.get("applies_to")
    if applies_to in (None, ""):
        applies_to = APPLIES_TO_TASK
    if applies_to not in APPLIES_TO:
        raise RuleError(
            "applies_to must be task or milestone.", code="bad_applies_to", field="applies_to"
        )
    trigger_type, trigger_value = _clean_trigger(
        project, data.get("trigger_type"), data.get("trigger_value")
    )
    conditions = _clean_conditions(project, data.get("conditions"), applies_to)

    destination = None
    raw = data.get("destination_project_id")
    if raw not in (None, ""):
        destination_id = _int(raw, code="destination_not_found", field="destination_project_id")
        destination = ProjectMaster.objects.filter(
            project_id=destination_id, is_deleted=False
        ).first()
        # Membership first, so a project the person is not on reads like a missing one.
        if destination is None or not is_project_member(destination.project_id, owner.id):
            raise guard_error("destination_not_found", "destination_project_id")

    if problem := owner_problem(owner, project, destination):
        raise guard_error(problem, "destination_project_id" if destination else None)
    if destination is not None and (problem := destination_problem(project, destination)):
        raise guard_error(problem, "destination_project_id")
    target = destination or project

    set_status = data.get("set_status") or ""
    if set_status and set_status not in SETTABLE_STATUSES:
        raise RuleError("Unknown status.", code="unknown_status", field="set_status")
    custom = None
    if data.get("set_custom_status_id") not in (None, ""):
        status_id = _int(
            data["set_custom_status_id"], code="unknown_status", field="set_custom_status_id"
        )
        custom = ProjectStatus.objects.filter(
            project=target, status_id=status_id, archived_at__isnull=True
        ).first()
        if custom is None or (set_status and custom.category != set_status):
            raise RuleError(
                "Not a status of the project the task ends up in.",
                code="unknown_status",
                field="set_custom_status_id",
            )
        set_status = custom.category

    set_assignee = None
    if data.get("set_assignee_id") not in (None, ""):
        user_id = _uuid(data["set_assignee_id"], code="bad_assignee", field="set_assignee_id")
        set_assignee = get_user_model().objects.filter(id=user_id, is_active=True).first()
        if set_assignee is None or not assignee_allowed(target, user_id):
            raise RuleError(
                "That person cannot be assigned there.",
                code="bad_assignee",
                field="set_assignee_id",
            )

    add_tag = data.get("add_tag") or ""
    if add_tag:
        found = find_tag(target.project_id, add_tag) if isinstance(add_tag, str) else None
        if found is None:
            raise RuleError(
                "Not a tag of the project the task ends up in.",
                code="unknown_tag",
                field="add_tag",
            )
        add_tag = found["tagName"]

    if not (destination or set_status or set_assignee or add_tag):
        raise RuleError("A rule needs an action.", code="no_action")

    return {
        "name": name,
        "applies_to": applies_to,
        "trigger_type": trigger_type,
        "trigger_value": trigger_value,
        "conditions": conditions,
        "moves_task": destination is not None,
        "destination_project": destination,
        "set_status": set_status,
        "set_custom_status": custom,
        "set_assignee": set_assignee,
        "add_tag": add_tag,
    }


def definition_of(rule: AutomationRule) -> dict:
    """A saved rule back in the input shape, so a partial edit is validated as a whole."""
    if rule.moves_task and rule.destination_project_id is None:
        # The destination is gone: an edit has to name a new one (or clear the move) on purpose.
        destination = 0
    else:
        destination = rule.destination_project_id
    return {
        "name": rule.name,
        "applies_to": rule.applies_to,
        "trigger_type": rule.trigger_type,
        "trigger_value": rule.trigger_value,
        "conditions": rule.conditions,
        "destination_project_id": destination,
        "set_status": rule.set_status,
        "set_custom_status_id": rule.set_custom_status_id,
        "set_assignee_id": str(rule.set_assignee_id) if rule.set_assignee_id else None,
        "add_tag": rule.add_tag,
    }


def serialize_rule(rule: AutomationRule, *, viewer_projects: set[int]) -> dict:
    """`viewer_projects`: the project ids the reader is on. A destination they are not on is not
    named (a hidden project's name must not leak), nor are the values that belong to it.

    `destination.gone` (MIP-2544) tells a deleted project apart from a hidden one: the rule needs
    a new destination, whoever reads it. Neither is named."""
    destination = rule.destination_project
    visible = destination is None or destination.project_id in viewer_projects
    shown = None
    if rule.moves_task:
        gone = destination is None or destination.is_deleted
        named = visible and not gone
        shown = {
            "projectId": destination.project_id if named else None,
            "name": destination.project_name if named else None,
            "visible": named,
            "gone": gone,
        }
    return {
        "ruleId": rule.id,
        "name": rule.name,
        "position": rule.position,
        "enabled": rule.enabled,
        "disabledReason": rule.disabled_reason if visible else public_reason(rule.disabled_reason),
        "appliesTo": rule.applies_to,
        "triggerType": rule.trigger_type,
        "triggerValue": rule.trigger_value,
        "conditions": rule.conditions,
        "movesTask": rule.moves_task,
        "destination": shown,
        "setStatus": rule.set_status or None,
        "setCustomStatusId": rule.set_custom_status_id if visible else None,
        "setAssigneeId": str(rule.set_assignee_id) if rule.set_assignee_id and visible else None,
        "addTag": (rule.add_tag or None) if visible else None,
        "ownerId": str(rule.owner_id) if rule.owner_id else None,
        "tsCreatedAt": rule.ts_created_at.isoformat() if rule.ts_created_at else None,
        "tsUpdatedAt": rule.ts_updated_at.isoformat() if rule.ts_updated_at else None,
    }
