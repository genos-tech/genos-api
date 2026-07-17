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

Granularity note (deliberate v1 scope): this authenticates — it does
not yet authorize per file. Any signed-in user who knows a path can
still fetch it; mapping every path family to its content ACL
(task → project members, chat → channel members, …) is the follow-up.
This step removes the anonymous drive-by surface, which is the common-
attack case.
"""

from __future__ import annotations

import posixpath
from urllib.parse import quote

from django.conf import settings
from django.http import JsonResponse
from django.views.static import serve as _serve_static
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

PUBLIC_MEDIA_PREFIXES = (
    "user_profiles/",
    "team_profiles/",
    "project_profiles/",
    "gm_profiles/",
    "channel_profiles/",
)


def _is_public_media(path: str) -> bool:
    # Normalise before classifying so "user_profiles/../notes/…" is
    # judged by where it actually points, not by its first segment.
    # (`django.views.static.serve` re-normalises independently, so this
    # can't disagree with what gets served.)
    clean = posixpath.normpath(path).lstrip("/")
    return clean.startswith(PUBLIC_MEDIA_PREFIXES)


def _is_authenticated_media_request(request) -> bool:
    """True when the request carries a valid access token (Bearer
    header) or a valid refresh token (the HttpOnly session cookie).

    Accepting the refresh cookie here is what lets plain `<img>` tags
    work without any frontend involvement. Verification includes the
    blacklist check, so a rotated-out or signed-out cookie stops
    working immediately; that costs one DB read per protected-media
    request (avatars — the high-volume case — never reach this).
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            AccessToken(auth[len("Bearer ") :])
            return True
        except TokenError:
            pass  # fall through — a stale header must not mask a valid cookie

    cookie = request.COOKIES.get("refresh")
    if cookie:
        try:
            RefreshToken(cookie)
            return True
        except TokenError:
            pass
    return False


def serve_media(request, path, document_root=None):
    """Auth-gated replacement for `django.views.static.serve` on
    `/media/`, keeping the forced attachment disposition.

    Protected paths answer 401 before touching the filesystem, so
    non-existence is never revealed to anonymous callers.
    """
    if not _is_public_media(path) and not _is_authenticated_media_request(request):
        return JsonResponse({"detail": "Authentication required."}, status=401)

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
    response = _serve_static(request, path, document_root=document_root)
    filename = path.rsplit("/", 1)[-1] or "download"
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download"
    response["Content-Disposition"] = (
        f'attachment; filename="{ascii_fallback}"; ' f"filename*=UTF-8''{quote(filename)}"
    )
    return response
