"""Authenticated serving for user-uploaded media.

`/media/` used to be fully public: anyone who knew (or guessed — paths
keep original filenames) a storage path could fetch any team's
attachments anonymously. This module gates the sensitive prefixes
behind authentication while keeping avatars public.

Public (unauthenticated):
    user_profiles/  team_profiles/  project_profiles/  gm_profiles/
    channel_profiles/
  Avatars are low-sensitivity, rendered everywhere, and — decisive —
  used as web-push notification icons, which the browser fetches from a
  context that doesn't reliably attach our cookies.

Everything else — the note/task/chat attachment trees and any prefix
added in the future — requires a signed-in user. Fail-closed by
default: an unknown prefix is protected, not public.

How the browser authenticates media loads with zero frontend special-
casing for `<img>`/`<video>`/top-level downloads: the HttpOnly `refresh`
cookie is scoped to the API host, and every prod topology serves the app
and the API on the same site (genosai.dev ↔ api.genosai.dev; localhost
ports in dev), so the browser attaches it to media subresource requests
natively. `downloadFile`-style `fetch()` consumers pass
`credentials: "include"` (frontend PR #165). API/tooling consumers can
send a normal `Authorization: Bearer` header instead.

## Per-file authorization

The first version of this module authenticated without authorizing: any
signed-in user who knew a path could fetch any team's attachments. That
is closed here — every protected path is now resolved to the object it
belongs to and checked against that object's existing ACL.

    task_attachments/<task_id>/…      → can_access_task
    tasks/<task_id>/…                 → can_access_task   (task body)
    chats/<channel_uuid>/messages/…   → active ChannelMember
    chats/<channel_uuid>/inline/…     → active ChannelMember
    notes/personal|task|chat/<id>/…   → note_role.get_effective_role

The ACLs are *borrowed*, never re-derived: an attachment is exactly as
visible as the object that owns it, so `/media/` can never disagree with
the endpoint that hands out its URL.

**Anything else protected is denied**, and logged at WARNING. The one
unmapped tree in production is legacy `chats/<chat_type>/…`, written by
the dropped pre-v3 chat tables — zero live messages reference it. The
warning exists for the next `upload_to` somebody adds: a new family
fails closed and says so in the log, rather than silently inheriting
world-readability.

### One normalized string

`serve_media` normalizes the path ONCE and uses that single value to
classify (public?), to authorize (which object?), and to serve. Two
independent classifications could be made to disagree:

    notes/task/<a-task-note-I-can-read>/../../personal/<not-mine>/x.pdf

reads as a task note segment-wise, but `django.views.static.serve`
re-normalizes and would have served the personal note. Django's
traversal guard (`safe_join`) only stops escapes *out of* MEDIA_ROOT; it
has nothing to say about moving between families inside it.
"""

from __future__ import annotations

import logging
import posixpath
import uuid as uuid_lib
from urllib.parse import quote

from django.conf import settings
from django.http import JsonResponse
from django.views.static import serve as _serve_static
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

logger = logging.getLogger(__name__)

PUBLIC_MEDIA_PREFIXES = (
    "user_profiles/",
    "team_profiles/",
    "project_profiles/",
    "gm_profiles/",
    "channel_profiles/",
    # Custom emoji render dozens of times per viewport (message lists,
    # reaction chips), so the per-request RefreshToken DB read of the
    # authed path would be the hot case, not the exception. Filenames
    # are uuid4-prefixed (unguessable) and the content is sticker-grade.
    "team_emoji/",
)


def _is_public_media(path: str) -> bool:
    # Normalises defensively so "user_profiles/../notes/…" is judged by
    # where it actually points, not by its first segment. `serve_media`
    # already hands in a normalized path; normpath is idempotent, so this
    # stays correct for the unit-test callers that pass a raw one.
    clean = posixpath.normpath(path).lstrip("/")
    return clean.startswith(PUBLIC_MEDIA_PREFIXES)


def _media_request_user(request):
    """The signed-in user behind a media request, or `None`.

    Accepts a Bearer access token or the HttpOnly `refresh` cookie —
    accepting the cookie is what lets plain `<img>` tags work with no
    frontend involvement. Refresh verification includes the blacklist
    check, so a rotated-out or signed-out cookie stops working
    immediately.

    Resolution goes through simplejwt's own `JWTAuthentication.get_user`
    rather than reading the `user_id` claim directly, because two checks
    that this module needs live *inside* that method and nowhere else:
    `CHECK_USER_IS_ACTIVE` and `CHECK_REVOKE_TOKEN` (the `hash_password`
    claim that makes a password reset evict a stolen session — see
    `apis/settings.SIMPLE_JWT`). Decoding the claim by hand would have
    silently opted media out of both, which is the same mistake this
    repo already shipped once on the OAuth path.

    `is_deleted` is checked here because it is this codebase's concept,
    not simplejwt's.

    Costs one user read per protected-media request, on top of the
    ACL query in `_may_read_media`. Avatars and custom emoji — the
    high-volume case, rendered dozens of times per viewport — are public
    and reach neither.
    """
    candidates = []
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            candidates.append(AccessToken(auth[len("Bearer ") :]))
        except TokenError:
            pass  # a stale header must not mask a valid cookie

    cookie = request.COOKIES.get("refresh")
    if cookie:
        try:
            candidates.append(RefreshToken(cookie))
        except TokenError:
            pass

    backend = JWTAuthentication()
    for token in candidates:
        try:
            user = backend.get_user(token)
        except (AuthenticationFailed, TokenError):
            continue
        if getattr(user, "is_deleted", False):
            continue
        return user
    return None


# ── per-file authorization ─────────────────────────────────────────────


def _int_or_none(segment):
    """Path segments are attacker-supplied. `int("abc")` reaching a PK
    filter is a 500, not a 403, so ids are parsed before they are used."""
    try:
        return int(segment)
    except (TypeError, ValueError):
        return None


def _uuid_or_none(segment):
    """Same, for channel ids — an unparseable UUID raises `ValidationError`
    deep in the ORM, which surfaces as a 500."""
    try:
        return str(uuid_lib.UUID(str(segment)))
    except (TypeError, ValueError, AttributeError):
        return None


def _may_read_media(clean: str, user_id) -> bool:
    """May `user_id` read this (already normalized) protected path?

    Model imports are function-local: `apis/urls.py` imports this module
    at URLconf load, and the app registry is not guaranteed ready then.
    `scope_guards.can_access_task` defers its import for the same reason.
    """
    from origin.models.chat.unified_models import ChannelMember  # noqa: PLC0415
    from origin.views.utils.note_role import (  # noqa: PLC0415
        NOTE_TYPE_CHAT,
        NOTE_TYPE_PERSONAL,
        NOTE_TYPE_TASK,
        get_effective_role,
    )
    from origin.views.utils.scope_guards import can_access_task  # noqa: PLC0415

    # The directory name IS the note type — `personal_note_attachment_path`
    # and friends build it from the same three-way split.
    note_dir_to_type = {
        "personal": NOTE_TYPE_PERSONAL,
        "task": NOTE_TYPE_TASK,
        "chat": NOTE_TYPE_CHAT,
    }

    segments = clean.split("/")

    # A task attachment and a task-body attachment are both as visible as
    # their task. `can_access_task` is the same rule the search ACL uses,
    # so an assignee with no ProjectMembers row can open what they find.
    if segments[0] in ("task_attachments", "tasks") and len(segments) >= 3:
        task_id = _int_or_none(segments[1])
        return task_id is not None and can_access_task(task_id, user_id)

    # `messages/` is the MessageAttachment tree; `inline/` is the
    # compose-time uploader (channel_views.ChannelInlineUploadView), which
    # writes through default_storage and so has no `upload_to` to grep —
    # it is only visible in the production media tree.
    if segments[0] == "chats" and len(segments) >= 4 and segments[2] in ("messages", "inline"):
        channel_id = _uuid_or_none(segments[1])
        return (
            channel_id is not None
            and ChannelMember.objects.filter(
                channel_id=channel_id, user=user_id, is_deleted=False
            ).exists()
        )

    # Notes carry a real ACL of their own (explicit grants, project/chat
    # membership, and Team-Note folder inheritance). Reuse it wholesale —
    # "can read the note" is exactly "can read its attachments".
    if segments[0] == "notes" and len(segments) >= 4:
        note_type = note_dir_to_type.get(segments[1])
        note_id = _int_or_none(segments[2])
        if note_type is None or note_id is None:
            return False
        return get_effective_role(user_id, note_type, note_id) is not None

    logger.warning(
        "serve_media: no ACL mapped for prefix %r — denied. Add it to _may_read_media.",
        "/".join(segments[:2]),
    )
    return False


def serve_media(request, path, document_root=None):
    """Auth-gated replacement for `django.views.static.serve` on
    `/media/`, keeping the forced attachment disposition.

    Protected paths answer 401 (anonymous) or 404 (signed in, not
    yours) before touching the filesystem, so non-existence is never
    revealed. 404 rather than 403 for the authorized-but-denied case
    follows the convention in `views/chat/channel_views.py`: 403 would
    confirm that the path names a real file, and it is exactly what
    `_serve_static` already answers for a path that does not exist, so
    "not yours" and "no such file" are indistinguishable.
    """
    # Normalize ONCE. This value classifies, authorizes, AND is what gets
    # served — see the module docstring on why two independent
    # classifications of the same path is a bypass, not a style choice.
    clean = posixpath.normpath(path).lstrip("/")

    if not _is_public_media(clean):
        user = _media_request_user(request)
        if user is None:
            return JsonResponse({"detail": "Authentication required."}, status=401)
        if not _may_read_media(clean, user.id):
            return JsonResponse({"detail": "Not found."}, status=404)

    # Resolved per-request (not bound in the URLconf) so MEDIA_ROOT
    # overrides — tests, env changes — take effect without re-importing
    # the URL module.
    if document_root is None:
        document_root = settings.MEDIA_ROOT

    # Force `Content-Disposition: attachment` on every media response.
    # BlockNote's toolbar FileDownloadButton is hardcoded to
    # `window.open(url)`, which makes the browser pick rendering by
    # MIME — `.py` (text/x-python) opens in a tab while `.md` falls
    # back to "Save As" since no native viewer exists. Forcing the
    # attachment disposition gives uniform "download the file"
    # behavior across types, and also fixes any future bare anchor /
    # `target=_blank` clicks on attachment URLs.
    #
    # `<img>` / `<video>` / `<audio>` subresource loads ignore
    # Content-Disposition, so inline image previews in chat / note
    # bodies still render normally — the header only affects
    # top-level navigation and `fetch`/XHR consumers (which we
    # already wrap with `URL.createObjectURL` in `downloadFile`).
    response = _serve_static(request, clean, document_root=document_root)
    filename = clean.rsplit("/", 1)[-1] or "download"
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download"
    response["Content-Disposition"] = (
        f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"
    )
    return response
