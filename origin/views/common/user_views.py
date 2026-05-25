import os
from datetime import date
import time

from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.common.user_models import CustomUser
from origin.models.task.task_models import TaskMaster
from origin.serializers.common.user_serializers import UserSerializer
from origin.services.calendar_sync import (
    LINK_ONLY_FIELDS,
    get_google_connected_account,
    sync_task_event,
)


#############################
# User views
#############################
class UserProfileView(AuthenticatedAPIView):
    def put(self, request):
        request_user_id = request.user.id

        user_id = request.data.get("user_id")

        if not user_id:
            return Response(
                {"error": "user_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            if str(request_user_id) != str(user_id):
                return Response(
                    {"error": "Only owner can update user info."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        user = CustomUser.objects.get(id=user_id)

        update_data = request.data.copy()
        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        serializer = UserSerializer(user, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserProfileImageView(AuthenticatedAPIView):
    parser_classes = [MultiPartParser]

    def put(self, request):
        request_user_id = request.user.id
        user_profile_image = request.FILES.get("user_profile_image")

        if user_profile_image is None:
            return Response(
                {"error": "user_profile_image is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_data = CustomUser.objects.get(id=request_user_id)

        # Best-effort cleanup of the previous profile image file. Django's
        # default `FileSystemStorage.get_available_name` never overwrites
        # — every re-upload of "profile.jpg" lands as
        # `profile_<random>.jpg`, leaving the old file behind forever. On
        # Railway's media volume that's an unbounded disk-bloat leak per
        # user. Deleting first means the new save lands at the canonical
        # path (no random suffix), which also keeps the URL stable across
        # uploads. Wrapped in a try so a missing-on-disk file doesn't
        # block the legitimate upload from succeeding.
        previous_file = user_data.profile_image_url
        if previous_file and previous_file.name:
            try:
                previous_file.delete(save=False)
            except Exception as err:  # pragma: no cover - best-effort cleanup
                print(f"Failed to delete previous profile image: {err}")

        # Only update the FileField
        new_profile_image_data = {
            "profile_image_url": user_profile_image,
        }

        serializer = UserSerializer(user_data, data=new_profile_image_data, partial=True)
        if serializer.is_valid():
            saved_user = serializer.save()

            # `profile_image_url.name` is already the storage path that
            # Django actually wrote (relative to MEDIA_ROOT and reflecting
            # any `get_available_name` rename). Mirror it onto
            # `profile_image_file_name` directly instead of rebuilding the
            # path from `request_user_id` + last segment — the rebuild was
            # equivalent today but coupled to `user_profile_image_path`'s
            # exact `user_profiles/<uuid>/<file>` shape, so any future
            # tweak to that helper would silently drift the served URL.
            #
            # Append `?v=<ms-timestamp>` so the served URL is unique per
            # upload. The delete-first cleanup above keeps the on-disk
            # filename stable (`profile.jpg`), which means *without* this
            # suffix the URL never changes and the browser keeps serving
            # the cached bytes after a page refresh — even though
            # `myself.avatarImgPath` was re-hydrated from a fresh API
            # response. The query string is ignored by Django's media
            # serving (path matching uses the path part only), and the
            # `profile_image_url` FileField still stores the clean path
            # so the next upload's `delete(save=False)` finds the file.
            cache_buster = int(time.time() * 1000)
            saved_user.profile_image_file_name = (
                f"{saved_user.profile_image_url.name}?v={cache_buster}"
            )
            saved_user.save(update_fields=["profile_image_file_name"])

            return Response(UserSerializer(saved_user).data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AutoCloseOnPrMergePreferenceView(AuthenticatedAPIView):
    """GET / PATCH the calling user's "auto-close task on PR merge"
    preference. Operates on `request.user`; no user_id is accepted or
    required, so a leaked token can't toggle someone else's setting.

    Returns and accepts a single boolean field `auto_close_on_pr_merge`.
    """

    def get(self, request):
        return Response(
            {"auto_close_on_pr_merge": bool(request.user.auto_close_on_pr_merge)},
            status=status.HTTP_200_OK,
        )

    def patch(self, request):
        value = request.data.get("auto_close_on_pr_merge")
        if not isinstance(value, bool):
            return Response(
                {"error": "auto_close_on_pr_merge must be a boolean."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        request.user.auto_close_on_pr_merge = value
        request.user.save(update_fields=["auto_close_on_pr_merge"])
        return Response(
            {"auto_close_on_pr_merge": value},
            status=status.HTTP_200_OK,
        )


class AutoSyncTasksToCalendarPreferenceView(AuthenticatedAPIView):
    """GET / PATCH the calling user's "auto-sync task due dates to
    Google Calendar" preference. Mirrors `AutoCloseOnPrMergePreferenceView`
    in shape — single boolean field, scoped to `request.user`.

    Toggling OFF is non-destructive: existing linked events stay on
    Google. Toggling back ON resumes syncing those events rather than
    re-creating them. Deletions on Google never propagate back —
    one-way sync is the whole point of this preference.
    """

    def get(self, request):
        return Response(
            {"auto_sync_tasks_to_calendar": bool(request.user.auto_sync_tasks_to_calendar)},
            status=status.HTTP_200_OK,
        )

    def patch(self, request):
        value = request.data.get("auto_sync_tasks_to_calendar")
        if not isinstance(value, bool):
            return Response(
                {"error": "auto_sync_tasks_to_calendar must be a boolean."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        request.user.auto_sync_tasks_to_calendar = value
        request.user.save(update_fields=["auto_sync_tasks_to_calendar"])
        return Response(
            {"auto_sync_tasks_to_calendar": value},
            status=status.HTTP_200_OK,
        )


class LlmModelPreferenceView(AuthenticatedAPIView):
    """GET / PATCH the calling user's LLM provider + model preference.

    Same shape as `AutoCloseOnPrMergePreferenceView` — scoped to
    `request.user`, no `user_id` accepted, so a leaked token can't
    rewrite someone else's choice.

    Both fields are optional strings; empty string means "use the
    server default" (env-driven `LLM_PROVIDER` / `GEMINI_MODEL` /
    `CLAUDE_MODEL`). Validation here is lenient on purpose — the
    request-time resolver in `llm/choice.py` falls back to the server
    default with a warning if the saved value isn't in the current
    `MODEL_CATALOG`, so removing a model from the catalog doesn't
    brick existing users.
    """

    def get(self, request):
        return Response(
            {
                "provider": request.user.preferred_llm_provider or "",
                "model": request.user.preferred_llm_model or "",
            },
            status=status.HTTP_200_OK,
        )

    def patch(self, request):
        provider = request.data.get("provider", "")
        model = request.data.get("model", "")
        if not isinstance(provider, str) or not isinstance(model, str):
            return Response(
                {"error": "provider and model must be strings."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        provider = provider.strip().lower()
        model = model.strip()
        # Reject obviously wrong provider values up front. Specific
        # model strings are NOT validated here — see docstring.
        if provider and provider not in ("gemini", "claude"):
            return Response(
                {"error": "provider must be 'gemini', 'claude', or empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        request.user.preferred_llm_provider = provider
        request.user.preferred_llm_model = model
        request.user.save(update_fields=["preferred_llm_provider", "preferred_llm_model"])
        return Response(
            {"provider": provider, "model": model},
            status=status.HTTP_200_OK,
        )


class CalendarSyncBackfillView(AuthenticatedAPIView):
    """POST one-shot backfill of the calling user's open future-dated
    tasks to Google Calendar.

    Pre-conditions:
      - `auto_sync_tasks_to_calendar` is True.
      - The user has a connected Google account.
    Returns `{synced: <count>}` for the UI to surface a confirmation
    toast. Errors on individual tasks are logged and skipped — the
    response count reflects only successful syncs. No pagination —
    individual users rarely have more than a few hundred open tasks.
    """

    def post(self, request):
        if not request.user.auto_sync_tasks_to_calendar:
            return Response(
                {"detail": "preference_disabled"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        account = get_google_connected_account(request.user)
        if account is None:
            return Response(
                {"detail": "google_not_connected"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Open, future-dated tasks assigned to this user. "Open" =
        # not soft-deleted. Past-due tasks are skipped to avoid
        # cluttering the user's calendar with backlog — the signal
        # will catch them next time they're edited.
        today = date.today()
        tasks = TaskMaster.objects.filter(
            assignee=request.user,
            is_deleted=False,
            due_date__gte=today,
        ).only(
            "pk", "title", "status", "due_date", "linked_calendar_event_id", "linked_calendar_id"
        )
        created = 0
        patched = 0
        cleared = 0
        for task in tasks:
            try:
                outcome = sync_task_event(account, task)
                # Save when link columns changed (create stored a new
                # id, or the link was cleared because Google's event
                # was deleted/cancelled). "patched" leaves the model
                # untouched; "failed" was already logged.
                if outcome in ("created", "cleared"):
                    task.save(update_fields=list(LINK_ONLY_FIELDS))
                # Three-way split so the UI can tell the user what
                # actually happened. "cleared" counts a task whose
                # upstream event was missing — surfaced separately
                # because the user would otherwise wonder why
                # "patched" showed up when no events exist.
                if outcome == "created":
                    created += 1
                elif outcome == "patched":
                    patched += 1
                elif outcome == "cleared":
                    cleared += 1
            except Exception:
                # Defensive — sync_task_event already swallows
                # transport errors, but any unexpected exception
                # shouldn't kill the whole backfill.
                continue
        return Response(
            # `synced` is the convenience total of real syncs
            # (excludes cleared/failed). Frontend uses all four
            # counts to compose an accurate toast.
            {
                "created": created,
                "patched": patched,
                "cleared": cleared,
                "synced": created + patched,
            },
            status=status.HTTP_200_OK,
        )
