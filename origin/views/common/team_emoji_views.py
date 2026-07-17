import re

from django.http import Http404
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.common.team_models import TeamMaster
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

# Shortcode rule: lowercase, no colons (added at render time). Max 50 so
# `:name:` fits the reaction column (MessageReaction.emoji, CharField(64)).
_NAME_RE = re.compile(r"[a-z0-9_+-]{1,50}")

# Flat cap, deliberately NOT the tier-based `check_upload_size` (whose
# smallest limit is 25 MiB): emoji are rendered dozens of times per
# viewport, so "small" is the feature, not a quota.
MAX_EMOJI_BYTES = 512 * 1024  # 512 KiB

# extension -> magic-byte predicate over the first 12 bytes. Both checks
# must pass: the extension names the stored file (it ends up in the URL
# the frontend bakes into bodies), the sniff stops content smuggling
# (e.g. an HTML file named .png).
_MAGIC_SNIFFERS = {
    "gif": lambda h: h.startswith((b"GIF87a", b"GIF89a")),
    "png": lambda h: h.startswith(b"\x89PNG\r\n\x1a\n"),
    "jpg": lambda h: h.startswith(b"\xff\xd8\xff"),
    "jpeg": lambda h: h.startswith(b"\xff\xd8\xff"),
    "webp": lambda h: h.startswith(b"RIFF") and h[8:12] == b"WEBP",
}


def _verify_team_member(user, team_id):
    """Return TeamMaster iff the user is a team member; else 404.

    Replicates the verified-membership idiom from
    `origin/views/chat/channel_views.py` (existence-hiding: we don't
    distinguish "no such team" from "not a member") rather than the
    older trust-the-client team_id pattern some v2 views still use.
    """
    try:
        return TeamMaster.objects.get(
            team_id=team_id,
            team_members__attendee=user,
            team_members__is_deleted=False,
        )
    except TeamMaster.DoesNotExist:
        raise Http404("Team not found.")


def _absolute_https_url(request, storage_url):
    """Absolute URL with the X-Forwarded-Proto https fixup.

    Same reasoning as ChannelInlineUploadView: behind the TLS-terminating
    proxy `request.scheme` is "http", and the URL gets baked verbatim
    into BlockNote bodies, where an http:// URL breaks the https SPA.
    """
    url = request.build_absolute_uri(storage_url)
    if request.headers.get("X-Forwarded-Proto") == "https" and url.startswith("http://"):
        url = "https://" + url[len("http://") :]
    return url


def _serialize_emoji(request, emoji: TeamEmojiMaster) -> dict:
    return {
        "emojiId": emoji.emoji_id,
        "name": emoji.name,
        "url": _absolute_https_url(request, emoji.image.url),
        "createdBy": str(emoji.created_by_id) if emoji.created_by_id else None,
        "tsCreatedAt": emoji.ts_created_at.isoformat() if emoji.ts_created_at else None,
    }


class TeamEmojiView(AuthenticatedAPIView):
    """Team custom emoji catalog. POST uploads (any team member), GET
    lists active emoji, DELETE soft-deletes (uploader only — the file is
    kept so bodies that baked its URL keep rendering). No PUT: renaming
    would strand the baked `:name:` in reactions, so rename = delete +
    re-upload, like Slack."""

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        team_id = request.data.get("team_id")
        name = (request.data.get("name") or "").strip().lower()
        file = request.FILES.get("file")

        if not team_id or not name or file is None:
            return Response(
                {"error": "team_id, name and file are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        team = _verify_team_member(request.user, team_id)

        if not _NAME_RE.fullmatch(name):
            return Response(
                {"error": "name must be 1-50 chars of a-z, 0-9, _, + or -."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if file.size > MAX_EMOJI_BYTES:
            return Response(
                {"error": "Emoji images must be 512 KB or smaller."},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        ext = (file.name or "").rsplit(".", 1)[-1].lower() if "." in (file.name or "") else ""
        sniffer = _MAGIC_SNIFFERS.get(ext)
        if sniffer is None:
            return Response(
                {"error": "Emoji images must be .png, .jpg, .jpeg, .gif or .webp."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        head = file.read(12)
        file.seek(0)
        if not sniffer(head):
            return Response(
                {"error": "File content does not match its extension."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if TeamEmojiMaster.objects.filter(team=team, name=name, is_deleted=False).exists():
            return Response(
                {"error": "An emoji with this name already exists in the team."},
                status=status.HTTP_409_CONFLICT,
            )

        emoji = TeamEmojiMaster(team=team, name=name, created_by=request.user)
        # Transient carrier read by the model's upload_to path builder;
        # the client filename itself is discarded there.
        emoji.image_ext = ext
        emoji.image.save(f"{name}.{ext}", file, save=True)
        return Response(_serialize_emoji(request, emoji), status=status.HTTP_201_CREATED)

    def get(self, request):
        team_id = request.GET.get("team_id")
        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        team = _verify_team_member(request.user, team_id)

        # Team catalog + the global defaults (team=NULL, seeded starter
        # packs). A team emoji with the same name overrides the global
        # one, so a team can "replace" a default by uploading over it.
        team_rows = list(TeamEmojiMaster.objects.filter(team=team, is_deleted=False))
        team_names = {e.name for e in team_rows}
        global_rows = TeamEmojiMaster.objects.filter(team__isnull=True, is_deleted=False).exclude(
            name__in=team_names
        )
        combined = sorted([*team_rows, *global_rows], key=lambda e: e.name)
        return Response(
            {"teamEmoji": [_serialize_emoji(request, e) for e in combined]},
            status=status.HTTP_200_OK,
        )

    def delete(self, request):
        emoji_id = request.GET.get("emoji_id") or request.data.get("emoji_id")
        if not emoji_id:
            return Response(
                {"error": "emoji_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            emoji = TeamEmojiMaster.objects.get(emoji_id=emoji_id, is_deleted=False)
        except (TeamEmojiMaster.DoesNotExist, ValueError):
            raise Http404("Emoji not found.")
        if emoji.team_id is None:
            # Global defaults have no uploader; they're managed via
            # `seed_team_emoji --global`, never through the API.
            return Response(
                {"error": "Default emoji are managed by the server."},
                status=status.HTTP_403_FORBIDDEN,
            )
        # Membership first (existence-hiding for outsiders), then the
        # uploader check (a fellow member gets an honest 403).
        _verify_team_member(request.user, emoji.team_id)
        if emoji.created_by_id != request.user.id:
            return Response(
                {"error": "Only the uploader can delete an emoji."},
                status=status.HTTP_403_FORBIDDEN,
            )
        emoji.is_deleted = True
        emoji.save(update_fields=["is_deleted", "ts_updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
