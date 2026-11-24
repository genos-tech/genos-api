"""`POST /api/public/v1/secret-scanning/`: GitHub secret scanning's verification endpoint (MIP-2271).

Not for people and not authenticated by a key: GitHub signs each delivery and the signature is the
credential (`services/secret_scanning.py` explains the protocol and what is never logged).

Order (genos-security): size from the header, then the body size, then the signature over the raw
body, and only then is the body parsed. Answers: 413 oversize, 401 unsigned or badly signed, 400
not a bounded JSON array, 200 with GitHub's array otherwise.
"""

from __future__ import annotations

import logging
import time

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from origin.services import secret_scanning as scanning
from origin.services.throttles import CACHE_ERRORS, WindowRateThrottle, _cache_down, _record_denial
from origin.views.public.anonymous_throttle import bucket_for

logger = logging.getLogger(__name__)


class SecretScanningThrottle(WindowRateThrottle):
    """Per address, every request counted (GitHub is a handful of addresses; a flood of unsigned
    posts from anywhere else is the thing to stop, before it costs a key fetch or a verify)."""

    scope = "secret_scanning"
    rate = "60/min"

    def allow_request(self, request, view=None):
        now = time.time()
        window = int(now // self.duration)
        key = f"throttle:{self.scope}:{bucket_for(request)}:{window}"
        self._cache_was_down = False
        try:
            count = self._hit(key, 1)
        except CACHE_ERRORS as exc:
            return _cache_down(self, request, exc)
        if count <= self.num_requests:
            return True
        self._wait = (window + 1) * self.duration - now
        _record_denial(self.scope, request, "anonymous")
        return False


@method_decorator(csrf_exempt, name="dispatch")
class SecretScanningView(APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []
    throttle_classes = [SecretScanningThrottle]

    def post(self, request):
        try:
            declared = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            declared = 0
        if declared > scanning.MAX_BODY_BYTES:
            return Response(
                {"detail": "too_large"}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            )
        raw = request.body
        if len(raw) > scanning.MAX_BODY_BYTES:
            return Response(
                {"detail": "too_large"}, status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            )

        try:
            scanning.verify_signature(
                raw,
                request.headers.get("Github-Public-Key-Identifier"),
                request.headers.get("Github-Public-Key-Signature"),
            )
        except scanning.SignatureError:
            return Response({"detail": "invalid_signature"}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            matches = scanning.parse_matches(raw)
        except (ValueError, RecursionError):
            return Response({"detail": "bad_body"}, status=status.HTTP_400_BAD_REQUEST)

        answers = []
        revoked = 0
        for match in matches:
            token = match.get("token")
            label = scanning.handle_match(token)
            revoked += label == scanning.LABEL_TRUE
            answers.append({"token_raw": token, "token_type": scanning.TOKEN_TYPE, "label": label})
        logger.info("secret scanning: %s matches, %s keys revoked", len(matches), revoked)
        return Response(answers, status=status.HTTP_200_OK)
