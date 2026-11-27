"""REST for a project's automation rules (MIP-2498). The rules are `services.automation_rules`,
running them `services.automation_engine`.

Only the project's owner and editors read or change them: a rule names other projects and acts with
somebody's rights. A project the caller is not on answers as a missing one.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ParseError
from rest_framework.response import Response

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.automation_models import (
    AutomationRule,
    AutomationRun,
    AutomationRunState,
)
from origin.services import automation_engine
from origin.services import automation_rules as rules
from origin.services.archive import archived_project_response
from origin.services.member_roles import can_manage, resolve_project_role
from origin.services.throttles import AutomationDryRunThrottle, with_defaults
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import can_access_task, is_project_member, member_project_ids

#: Input keys of a rule's definition; a PATCH replaces the ones it names.
_DEFINITION = (
    "name",
    "trigger_type",
    "trigger_value",
    "conditions",
    "destination_project_id",
    "set_status",
    "set_custom_status_id",
    "set_assignee_id",
    "add_tag",
)
_RUNS_PAGE = 50


def _error(exc: rules.RuleError) -> Response:
    body = {"error": exc.message, "code": exc.code}
    if exc.field:
        body["field"] = exc.field
    return Response(body, status=exc.status)


def _plan_response() -> Response:
    return Response(
        {"error": "Automation rules are part of the Core plan and up.", "code": "plan_required"},
        status=status.HTTP_403_FORBIDDEN,
    )


class _RuleBase(AuthenticatedAPIView):
    def _project(self, request, project_id, *, write: bool):
        """`(project, None)` or `(None, response)`."""
        if not is_project_member(project_id, request.user.id, live_only=True):
            return None, Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
        project = ProjectMaster.objects.get(project_id=project_id)
        if not can_manage(resolve_project_role(project, request.user.id)):
            return None, Response(
                {"error": "Only the project owner or an editor can manage automation rules."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if write and (res := archived_project_response(project.project_id)):
            return None, res
        return project, None

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if request.method in ("POST", "PUT", "PATCH") and not isinstance(request.data, dict):
            raise ParseError("The body must be an object.")

    def _rule(self, project, rule_id):
        rule = (
            AutomationRule.objects.select_related("destination_project")
            .filter(project=project, id=rule_id)
            .first()
        )
        if rule is None:
            return None, Response({"error": "Rule not found."}, status=status.HTTP_404_NOT_FOUND)
        return rule, None

    def _serialize(self, request, rule) -> dict:
        return rules.serialize_rule(
            rule, viewer_projects=set(member_project_ids(request.user.id, live_only=True))
        )

    def _list(self, request, project, *, code=status.HTTP_200_OK):
        viewer = set(member_project_ids(request.user.id, live_only=True))
        rows = AutomationRule.objects.filter(project=project).select_related("destination_project")
        return Response(
            {
                "rules": [rules.serialize_rule(r, viewer_projects=viewer) for r in rows],
                "planAllows": rules.plan_allows(request.user.id),
            },
            status=code,
        )


class AutomationRuleListView(_RuleBase):
    """`projects/<id>/automation-rules/`: list, add (at the end of the order)."""

    def get(self, request, project_id: int):
        project, err = self._project(request, project_id, write=False)
        return err or self._list(request, project)

    def post(self, request, project_id: int):
        project, err = self._project(request, project_id, write=True)
        if err:
            return err
        if not rules.plan_allows(request.user.id):
            return _plan_response()
        try:
            with transaction.atomic():
                # The project row serialises the cap and the order.
                ProjectMaster.objects.select_for_update().get(pk=project.pk)
                existing = AutomationRule.objects.filter(project=project)
                if existing.count() >= rules.MAX_RULES:
                    raise rules.RuleError(
                        f"A project can have at most {rules.MAX_RULES} rules.",
                        code="too_many_rules",
                    )
                fields = rules.clean_rule(project, request.data, owner=request.user)
                last = existing.order_by("-position").values_list("position", flat=True).first()
                rule = AutomationRule.objects.create(
                    team_id=project.team_id,
                    project=project,
                    position=0 if last is None else last + 1,
                    enabled=request.data.get("enabled", True) is not False,
                    owner=request.user,
                    created_by=request.user,
                    **fields,
                )
        except rules.RuleError as exc:
            return _error(exc)
        return Response(self._serialize(request, rule), status=status.HTTP_201_CREATED)


class AutomationRuleDetailView(_RuleBase):
    """`projects/<id>/automation-rules/<rule_id>/`. Any change but turning a rule off makes the
    person who saves it its owner, judged with their rights."""

    def get(self, request, project_id: int, rule_id: int):
        project, err = self._project(request, project_id, write=False)
        if err:
            return err
        rule, err = self._rule(project, rule_id)
        return err or Response(self._serialize(request, rule))

    def patch(self, request, project_id: int, rule_id: int):
        project, err = self._project(request, project_id, write=True)
        if err:
            return err
        rule, err = self._rule(project, rule_id)
        if err:
            return err
        data = request.data
        enabled = data.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            return _error(rules.RuleError("enabled must be true or false.", code="bad_enabled"))
        changes = {k: data.get(k) for k in _DEFINITION if k in data}
        if enabled is False and not changes:
            # Turning a rule off needs no plan and takes nothing over.
            rule.enabled = False
            rule.disabled_reason = ""
            rule.save(update_fields=["enabled", "disabled_reason", "ts_updated_at"])
            return Response(self._serialize(request, rule))
        if not rules.plan_allows(request.user.id):
            return _plan_response()
        definition = {**rules.definition_of(rule), **changes}
        # A new action replaces the status pair as one: naming only the status drops the old label.
        if "set_status" in changes and "set_custom_status_id" not in changes:
            definition["set_custom_status_id"] = None
        # A new destination does not inherit values that belong to the old one.
        if "destination_project_id" in changes and str(changes["destination_project_id"]) != str(
            rule.destination_project_id
        ):
            for key in ("set_custom_status_id", "add_tag"):
                if key not in changes:
                    definition[key] = None
        try:
            fields = rules.clean_rule(project, definition, owner=request.user)
        except rules.RuleError as exc:
            return _error(exc)
        for key, value in fields.items():
            setattr(rule, key, value)
        rule.owner = request.user
        rule.disabled_reason = ""
        if enabled is not None:
            rule.enabled = enabled
        rule.save()
        return Response(self._serialize(request, rule))

    def delete(self, request, project_id: int, rule_id: int):
        project, err = self._project(request, project_id, write=True)
        if err:
            return err
        rule, err = self._rule(project, rule_id)
        if err:
            return err
        rule.delete()
        return self._list(request, project)


class AutomationRuleOrderView(_RuleBase):
    """`projects/<id>/automation-rules/order/`: `{"order": [rule ids]}`, every rule once."""

    def put(self, request, project_id: int):
        project, err = self._project(request, project_id, write=True)
        if err:
            return err
        order = request.data.get("order")
        with transaction.atomic():
            ProjectMaster.objects.select_for_update().get(pk=project.pk)
            rows = {r.id: r for r in AutomationRule.objects.filter(project=project)}
            if (
                not isinstance(order, list)
                or not all(isinstance(i, int) for i in order)
                or sorted(order) != sorted(rows)
            ):
                return _error(rules.RuleError("order must list every rule once.", code="bad_order"))
            now = timezone.now()
            for position, rule_id in enumerate(order):
                rows[rule_id].position = position
                rows[rule_id].ts_updated_at = now
            AutomationRule.objects.bulk_update(rows.values(), ["position", "ts_updated_at"])
        return self._list(request, project)


class AutomationRuleRunsView(_RuleBase):
    """`projects/<id>/automation-rules/<rule_id>/runs/`: what the rule did lately. A task the
    reader cannot open is not named."""

    def get(self, request, project_id: int, rule_id: int):
        project, err = self._project(request, project_id, write=False)
        if err:
            return err
        rule, err = self._rule(project, rule_id)
        if err:
            return err
        rows = (
            AutomationRun.objects.filter(rule=rule)
            .exclude(state=AutomationRunState.PENDING)
            .select_related("task", "task__project")
            .order_by("-ts_created_at", "-pk")[:_RUNS_PAGE]
        )
        destination = rule.destination_project
        sees_destination = destination is None or is_project_member(
            destination.project_id, request.user.id
        )
        out = []
        for run in rows:
            readable = can_access_task(run.task_id, request.user.id)
            out.append(
                {
                    "runId": run.pk,
                    "taskId": run.task_id if readable else None,
                    "displayId": run.task.display_id if readable else None,
                    "state": run.state,
                    "reason": run.reason if sees_destination else rules.public_reason(run.reason),
                    "tsDoneAt": run.ts_done_at.isoformat() if run.ts_done_at else None,
                }
            )
        return Response({"runs": out})


class AutomationRuleDryRunView(_RuleBase):
    """`projects/<id>/automation-rules/dry-run/`: `{"rule_id": n}` for a saved rule, else the
    definition of one not saved yet (judged for the caller). Writes nothing."""

    throttle_classes = with_defaults(AutomationDryRunThrottle)

    def post(self, request, project_id: int):
        project, err = self._project(request, project_id, write=False)
        if err:
            return err
        if not rules.plan_allows(request.user.id):
            return _plan_response()
        rule_id = request.data.get("rule_id")
        if rule_id is not None:
            if not isinstance(rule_id, int):
                return Response({"error": "Rule not found."}, status=status.HTTP_404_NOT_FOUND)
            rule, err = self._rule(project, rule_id)
            if err:
                return err
            # A saved rule may move into a project the reader is not on: its titles and
            # reasons would say something about that project.
            destination = rule.destination_project
            if destination is not None and not is_project_member(
                destination.project_id, request.user.id
            ):
                return _error(rules.guard_error("destination_not_found"))
        else:
            try:
                fields = rules.clean_rule(
                    project, {"name": "dry run", **request.data}, owner=request.user
                )
            except rules.RuleError as exc:
                return _error(exc)
            rule = AutomationRule(project=project, owner=request.user, **fields)
        result = automation_engine.dry_run(rule, project)
        if rule.owner_id != request.user.id:
            # Somebody else's plan and standing are theirs.
            for item in result["items"]:
                if item["reason"] == "plan" or item["reason"] in rules.OWNER_PROBLEMS:
                    item["reason"] = "owner"
        return Response(result)
