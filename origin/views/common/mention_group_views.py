from collections import defaultdict

from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.mention_group_models import (
    MentionGroupMaster,
    MentionGroupMembers,
)
from origin.models.common.team_models import TeamMembers
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import is_guest, is_team_member


def _require_team(request, team_id):
    """`None` when the caller belongs to `team_id`, else a 404 `Response`.

    404 rather than 403 for the usual reason (`scope_guards`): it must
    not confirm that an id names a real team.

    Guests are refused deliberately. A mention group is a team-wide
    directory construct — its whole purpose is to name a set of
    colleagues — and handing an external collaborator the org chart is
    exactly the enumeration the guest model exists to prevent.
    """
    if not team_id:
        return Response({"error": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not is_team_member(team_id, request.user.id) or is_guest(team_id, request.user.id):
        return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
    return None


def _require_group(request, group_id):
    """`(group, None)` when allowed, `(None, Response)` otherwise.

    Group ids are sequential integers, so resolving one and acting on it
    without checking the caller's team let anyone walk the id space —
    reading a team's org chart, renaming its groups, or deleting them.
    """
    if not group_id:
        return None, Response(
            {"error": "group_id is required."}, status=status.HTTP_400_BAD_REQUEST
        )
    group = MentionGroupMaster.objects.filter(group_id=group_id, is_deleted=False).first()
    if (
        group is None
        or not is_team_member(group.team_id, request.user.id)
        or is_guest(group.team_id, request.user.id)
    ):
        return None, Response(
            {"error": "Mention group not found."}, status=status.HTTP_404_NOT_FOUND
        )
    return group, None


def _serialize_group(group: MentionGroupMaster, member_user_ids: list) -> dict:
    """Wire-shape returned to the frontend. camelCase to match the
    convention used by team_views / project_views."""
    return {
        "groupId": group.group_id,
        "groupName": group.group_name,
        "description": group.description or "",
        "memberCount": len(member_user_ids),
        "memberUserIds": member_user_ids,
        "createdBy": str(group.created_by_id) if group.created_by_id else None,
        "tsCreatedAt": group.ts_created_at.isoformat() if group.ts_created_at else None,
        "tsUpdatedAt": group.ts_updated_at.isoformat() if group.ts_updated_at else None,
    }


class MentionGroupView(AuthenticatedAPIView):
    """CRUD for the group itself. POST creates, GET lists all groups in
    a team (with their resolved member ids inline so the frontend mention
    picker can render member-count chips without a second round trip),
    PUT updates name / description, DELETE soft-deletes."""

    def post(self, request):
        team_id = request.data.get("team_id")
        group_name = (request.data.get("group_name") or "").strip().lower()
        description = request.data.get("description") or ""
        created_by = request.data.get("created_by")

        if not team_id or not group_name:
            return Response(
                {"error": "team_id and group_name are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if res := _require_team(request, team_id):
            return res

        if MentionGroupMaster.objects.filter(
            team_id=team_id, group_name=group_name, is_deleted=False
        ).exists():
            return Response(
                {"error": "A mention group with this name already exists in the team."},
                status=status.HTTP_409_CONFLICT,
            )

        group = MentionGroupMaster.objects.create(
            team_id=team_id,
            group_name=group_name,
            description=description,
            created_by_id=created_by,
        )
        return Response(_serialize_group(group, []), status=status.HTTP_201_CREATED)

    def get(self, request):
        team_id = request.GET.get("team_id")
        if res := _require_team(request, team_id):
            return res

        groups = list(
            MentionGroupMaster.objects.filter(team_id=team_id, is_deleted=False).order_by(
                "group_name"
            )
        )
        if not groups:
            return Response({"mentionGroups": []}, status=status.HTTP_200_OK)

        # One round trip for all member rows across the listed groups,
        # then group in Python. Avoids N+1 on the per-group member fetch.
        member_rows = MentionGroupMembers.objects.filter(
            group_id__in=[g.group_id for g in groups]
        ).values_list("group_id", "user_id")
        by_group = defaultdict(list)
        for group_id, user_id in member_rows:
            by_group[group_id].append(str(user_id))

        payload = [_serialize_group(g, by_group.get(g.group_id, [])) for g in groups]
        return Response({"mentionGroups": payload}, status=status.HTTP_200_OK)

    def put(self, request):
        group_id = request.data.get("group_id")
        group, err = _require_group(request, group_id)
        if err:
            return err

        # Partial update — only touch fields the caller supplied.
        if "group_name" in request.data:
            new_name = (request.data.get("group_name") or "").strip().lower()
            if not new_name:
                return Response(
                    {"error": "group_name cannot be empty."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if (
                MentionGroupMaster.objects.filter(
                    team_id=group.team_id, group_name=new_name, is_deleted=False
                )
                .exclude(group_id=group_id)
                .exists()
            ):
                return Response(
                    {"error": "A mention group with this name already exists in the team."},
                    status=status.HTTP_409_CONFLICT,
                )
            group.group_name = new_name
        if "description" in request.data:
            group.description = request.data.get("description") or ""
        group.save()

        member_ids = [
            str(uid)
            for uid in MentionGroupMembers.objects.filter(group_id=group_id).values_list(
                "user_id", flat=True
            )
        ]
        return Response(_serialize_group(group, member_ids), status=status.HTTP_200_OK)

    def delete(self, request):
        group_id = request.GET.get("group_id") or request.data.get("group_id")
        if not group_id:
            return Response(
                {"error": "group_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        group, err = _require_group(request, group_id)
        if err:
            return err
        # Soft-delete keeps existing message bodies stable; the resolve
        # endpoint returns an empty member set so live fan-out skips them.
        group.is_deleted = True
        group.save(update_fields=["is_deleted", "ts_updated_at"])
        return Response({"groupId": group.group_id}, status=status.HTTP_200_OK)


class MentionGroupMembersView(AuthenticatedAPIView):
    """Member management. POST accepts a list of user_ids to add (idempotent
    — already-joined users are silently skipped). DELETE removes a single
    user from a group. GET lists the members of a group."""

    def post(self, request):
        group_id = request.data.get("group_id")
        user_ids = request.data.get("user_ids") or []
        added_by = request.data.get("added_by")
        if not group_id or not user_ids:
            return Response(
                {"error": "group_id and user_ids are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        group, err = _require_group(request, group_id)
        if err:
            return err

        existing = set(
            MentionGroupMembers.objects.filter(group_id=group_id).values_list("user_id", flat=True)
        )
        to_create = [
            MentionGroupMembers(
                team_id=group.team_id,
                group_id=group_id,
                user_id=uid,
                added_by_id=added_by,
            )
            for uid in user_ids
            if str(uid) not in {str(e) for e in existing}
        ]
        if to_create:
            with transaction.atomic():
                MentionGroupMembers.objects.bulk_create(to_create, ignore_conflicts=True)

        all_member_ids = [
            str(uid)
            for uid in MentionGroupMembers.objects.filter(group_id=group_id).values_list(
                "user_id", flat=True
            )
        ]
        return Response(
            {
                "groupId": int(group_id),
                "memberUserIds": all_member_ids,
                "memberCount": len(all_member_ids),
            },
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request):
        group_id = request.GET.get("group_id") or request.data.get("group_id")
        user_id = request.GET.get("user_id") or request.data.get("user_id")
        if not user_id:
            return Response(
                {"error": "group_id and user_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        _, err = _require_group(request, group_id)
        if err:
            return err
        MentionGroupMembers.objects.filter(group_id=group_id, user_id=user_id).delete()
        remaining = [
            str(uid)
            for uid in MentionGroupMembers.objects.filter(group_id=group_id).values_list(
                "user_id", flat=True
            )
        ]
        return Response(
            {"groupId": int(group_id), "memberUserIds": remaining, "memberCount": len(remaining)},
            status=status.HTTP_200_OK,
        )

    def get(self, request):
        group_id = request.GET.get("group_id")
        _, err = _require_group(request, group_id)
        if err:
            return err
        member_ids = [
            str(uid)
            for uid in MentionGroupMembers.objects.filter(group_id=group_id).values_list(
                "user_id", flat=True
            )
        ]
        return Response(
            {
                "groupId": int(group_id),
                "memberUserIds": member_ids,
                "memberCount": len(member_ids),
            },
            status=status.HTTP_200_OK,
        )


class MentionGroupResolveView(AuthenticatedAPIView):
    """Bulk-resolve group ids to their member user_ids. Used by the Flask
    mention pipeline to fan-out a single @group mention into per-user
    Mention rows. Soft-deleted groups resolve to an empty list so the
    fan-out silently skips them; the inline token still renders in the
    body via its persisted props."""

    def post(self, request):
        group_ids = request.data.get("group_ids") or []
        if not isinstance(group_ids, list):
            return Response(
                {"error": "group_ids must be a list."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not group_ids:
            return Response({"resolved": {}}, status=status.HTTP_200_OK)

        # Only return memberships for non-deleted groups the CALLER can
        # see. This endpoint takes a list of ids and returns member sets
        # wholesale, so without the team filter it is the fastest way to
        # enumerate several teams' org charts in one request — and the
        # sockets service calls it with the user's own JWT, so scoping
        # here covers that path too.
        live_groups = set(
            MentionGroupMaster.objects.filter(group_id__in=group_ids, is_deleted=False)
            .filter(
                team_id__in=TeamMembers.objects.filter(
                    attendee=request.user, is_deleted=False
                ).values_list("team_id", flat=True)
            )
            .values_list("group_id", flat=True)
        )
        rows = MentionGroupMembers.objects.filter(group_id__in=live_groups).values_list(
            "group_id", "user_id"
        )
        resolved = defaultdict(list)
        for gid, uid in rows:
            resolved[str(gid)].append(str(uid))
        # Ensure every requested id appears in the response (empty list
        # for deleted or unknown ids) so the caller can blindly iterate.
        for gid in group_ids:
            resolved.setdefault(str(gid), [])
        return Response({"resolved": dict(resolved)}, status=status.HTTP_200_OK)
