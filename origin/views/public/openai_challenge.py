"""`GET /.well-known/openai-apps-challenge`: proof that this host is ours (MIP-1108).

To publish Genos in ChatGPT's directory, OpenAI's plugin portal gives the publisher a token and
checks that the MCP server's host serves it, as plain text, at this path. The token is not a
secret (anyone who fetches the page sees it; that is the point of the check), but it is only
OURS to serve once the portal has issued it, so it comes from the environment:

    OPENAI_APPS_CHALLENGE=<the token from the portal>

Unset, empty or malformed, the route is a 404: a deployment that was never asked for the
check says nothing at this path. Read per request (not at import) so the value is whatever the
running process was given, and so no settings file has to change.
"""

from __future__ import annotations

import logging
import os
import re

from django.http import Http404, HttpResponse
from django.views.decorators.http import require_safe

log = logging.getLogger(__name__)

# What a challenge token can look like: printable ASCII with no spaces, at most 512 characters.
# Anything else is a pasting mistake (a quote, a newline in the middle, two lines) and is served
# as nothing rather than as something OpenAI would then fail to match.
_TOKEN = re.compile(r"[\x21-\x7e]{1,512}")


def challenge_token() -> str | None:
    raw = os.environ.get("OPENAI_APPS_CHALLENGE", "").strip()
    if not raw:
        return None
    if not _TOKEN.fullmatch(raw):
        log.warning("openai_challenge: OPENAI_APPS_CHALLENGE is set but is not a plain token")
        return None
    return raw


@require_safe
def openai_apps_challenge_view(request):
    token = challenge_token()
    if token is None:
        raise Http404
    response = HttpResponse(token, content_type="text/plain; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    return response
