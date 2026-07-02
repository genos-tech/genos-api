from collections import defaultdict

from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.mention_group_models import (
    MentionGroupMaster,
    MentionGroupMembers,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


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
        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

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
        if not group_id:
            return Response(
                {"error": "group_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            group = MentionGroupMaster.objects.get(group_id=group_id, is_deleted=False)
        except MentionGroupMaster.DoesNotExist:
            return Response(
                {"error": "Mention group not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

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
        try:
            group = MentionGroupMaster.objects.get(group_id=group_id)
        except MentionGroupMaster.DoesNotExist:
            return Response(
                {"error": "Mention group not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
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

        try:
            group = MentionGroupMaster.objects.get(group_id=group_id, is_deleted=False)
        except MentionGroupMaster.DoesNotExist:
            return Response(
                {"error": "Mention group not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

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
        if not group_id or not user_id:
            return Response(
                {"error": "group_id and user_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
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
        if not group_id:
            return Response(
                {"error": "group_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
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

        # Only return memberships for non-deleted groups.
        live_groups = set(
            MentionGroupMaster.objects.filter(
                group_id__in=group_ids, is_deleted=False
            ).values_list("group_id", flat=True)
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
