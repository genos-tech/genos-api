"""Every mutating route answers a JSON body that is not an object the same way (MIP-1013).

The rule lives in one place, `ObjectBodyMixin` (`origin/views/common/object_body.py`),
on `AuthenticatedAPIView`, `PublicApiView` and the plain views that read a body. This
walks the URL conf so a route added later is held to it without anyone remembering to:

* `[1]`, `"abc"`, `5`, `true` are exactly 400 `{"error": NOT_AN_OBJECT}` (an upload view
  that takes only multipart never reads JSON: DRF's 415, or its answer to `{}`);
* a malformed body is 400 `{"error": NOT_JSON}`;
* `null`, `[]`, `0`, `false` answer with the status `{}` answers with on the same URL;
* `{}` and `null` never answer 500 (MIP-1014), except the routes named in
  `KNOWN_FAILURES_MIP_1014`, which can only shrink: a member that stops failing fails the test
  until it is taken out.

The views that are not ours to guard are named here BY CLASS, and the walk asserts the
set: so are the classes that carry the guard, which must be exactly the classes walked,
less the exemptions. A new view on a plain `APIView` that reads a body fails that check
until it either takes the mixin or is added to an exemption with a reason.

Each probe runs in a savepoint that is rolled back, with the cache cleared, so every
probe sees the same fixtures; the DELETE pass runs last anyway.
"""

from __future__ import annotations

import re
import uuid
from unittest import mock

from django.core.cache import cache
from django.db import transaction
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone
from rest_framework.parsers import JSONParser
from rest_framework.test import APIClient
from rest_framework.views import APIView

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.api_key_models import SCOPE_WRITE, ApiKey, generate_key, hash_key
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.views.common.object_body import NOT_AN_OBJECT, NOT_JSON, ObjectBodyMixin
from origin.views.public.v1_views import PublicApiView

from .test_base import BaseAPITestCase

BAD = ("[1]", '"abc"', "5", "true")
EMPTY = ("null", "[]", "0", "false")
MALFORMED = '{"a":'
MUTATING = ("post", "put", "patch", "delete")

# Not guarded, on purpose. By dotted class name.
EXEMPT = {
    # Sign-in and the other unauthenticated auth endpoints: serializer views that already
    # answer a body that is not an object with their own 400, or ignore the body (demo,
    # sign-out). Left as they are (MIP-1013 decision 3).
    "origin.views.common.auth_views.UserViewSet": "signup serializer 400",
    "origin.views.common.auth_views.CustomTokenObtainPairView": "sign-in serializer 400",
    "origin.views.common.auth_views.CookieTokenRefreshView": "refresh serializer 400",
    "origin.views.common.auth_views.DemoSignInView": "reads only an optional lang",
    "origin.views.common.auth_views.LogoutView": "reads the cookie, not the body",
    "origin.views.common.auth_views.PasswordResetRequestView": "serializer 400",
    "origin.views.common.auth_views.PasswordResetConfirmView": "serializer 400",
    "origin.views.common.auth_views.ResendVerificationView": "serializer 400",
    # Authenticated by a signature over the RAW body; the sender is answered, not judged.
    "origin.views.common.billing_views.StripeWebhookView": "Stripe signature",
    "origin.views.common.github_views.GithubWebhookView": "GitHub signature",
    # JSON-RPC answers in its own words (-32600).
    "origin.views.public.mcp.views.McpView": "JSON-RPC error",
}

# Not DRF views at all, so there is no `initial()` to guard: django-anymail's inbound and
# tracking webhooks, authenticated by the provider's own scheme.
NON_DRF_MODULE_PREFIXES = (
    "anymail.",
    # MIP-1077: the OAuth server's own protocol endpoints. Each is a plain view that reads
    # its body itself and answers a malformed one with the RFC's error (the registration
    # view parses JSON with a size cap; token and revoke are the library's, form-encoded).
    # Tested in test_mcp_oauth_foundation.
    "origin.views.public.oauth_views.",
    "oauth2_provider.views.",
    # MIP-1108: GET/HEAD only; it never reads a request body.
    "origin.views.public.openai_challenge.",
)

# Views whose parsers do not include JSON (uploads): a JSON body is never read as
# JSON. Their POST/PUT answer it with DRF's 415; a DELETE that reads only the query
# answers as it does for `{}`.
MULTIPART_ONLY = {
    "origin.views.chat.channel_views.ChannelInlineUploadView",
    "origin.views.chat.channel_views.ChannelProfileImageView",
    "origin.views.chat.message_views.MessageAttachmentsView",
    "origin.views.common.team_emoji_views.TeamEmojiView",
    "origin.views.common.team_views.TeamProfileImageView",
    "origin.views.common.user_views.UserProfileImageView",
    "origin.views.project.prj_views.ProjectProfileImageView",
    "origin.views.task.task_views.TaskAttachmentsView",
}

# A malformed body read by something that runs before the guard (DRF's throttles):
# still a 400, in DRF's own words ("JSON parse error - ...").
MALFORMED_READ_EARLY = {
    # `InviteThrottle.cost` counts the addresses in the body.
    ("POST", "api/v2/team/invite/"),
}

# Routes where `{}` itself answers 500, so "the same as `{}`" says nothing and the "never 500"
# check below cannot hold. EMPTY: after MIP-1014 (the method lists, the handlers' own parsing,
# the inbox approvals, `team/create/`) and the `request.data[...]` sweep (MIP-1020) no route is
# left. An entry needs a cause beside it and a follow-up task; its empty bodies are not compared.
# The set can only shrink: a member that now answers anything but 500, or that is no longer
# routed, FAILS the test until it is taken out, so a fix cannot leave its route here. A typo
# fails too: every member must name a route that exists.
KNOWN_FAILURES_MIP_1014: set[tuple[str, str]] = set()

# 251 today; MIP-1014 stops routing the nine pairs whose handler cannot take the route's
# arguments, which leaves 242.
MIN_PAIRS = 240

REFUSED = {"error": NOT_AN_OBJECT}


class _Rollback(Exception):
    pass


def _walk(patterns, prefix=""):
    for p in patterns:
        route = str(p.pattern)
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + route)
        elif isinstance(p, URLPattern):
            yield prefix + route, p.callback


def _name(cls) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _methods(callback, cls) -> set[str]:
    """The mutating methods this URL actually routes, as DRF would dispatch them."""
    actions = getattr(callback, "actions", None)
    if actions:
        return {m for m in actions if m in MUTATING}
    initkwargs = getattr(callback, "view_initkwargs", None) or {}
    allowed = initkwargs.get("http_method_names", cls.http_method_names)
    return {m for m in MUTATING if m in allowed and hasattr(cls, m)}


def mutating_routes():
    """`(METHOD, route, class)` for every mutating route, DRF or not."""
    out = []
    for route, callback in _walk(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
        if cls is None or "(?P" in route or route.startswith("^"):
            continue
        for method in sorted(_methods(callback, cls)):
            out.append((method.upper(), route, cls))
    return out


def _is_guarded(cls) -> bool:
    return issubclass(cls, ObjectBodyMixin) and cls.object_body_required


class RouteBodyShapeTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # Demo sign-in (exempt, probed only for "not a 500") starts a background
        # OpenSearch reindex; nothing here may leave a thread behind.
        patcher = mock.patch("origin.views.common.auth_views.kick_off_demo_reindex")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = APIClient(raise_request_exception=False)
        self.authenticate()
        self.key_client = APIClient(raise_request_exception=False)
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user,
            team=self.team,
            name="walk",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=SCOPE_WRITE,
        )
        self.key_client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")

        gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="GM", owner=self.user
        )
        ChannelMember.objects.create(channel=gm, user=self.user, role="owner")
        msg = Message.objects.create(channel=gm, sender=self.user, seq=1, body=[], body_text="x")
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="Open", reporter=self.user
        )
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=timezone.localdate()
        )
        item = ToDoItem.objects.create(group=group, title="x")
        self.ids = {
            "channel_id": gm.id,
            "message_id": msg.id,
            "project_id": project.project_id,
            "task_id": task.task_id,
            "team_id": self.team.team_id,
            "item_id": item.item_id,
            "user_id": self.user2.id,
            "provider_name": "google",
        }

    def fill(self, route: str) -> str:
        def sub(m):
            conv, name = m.group(1) or "str", m.group(2)
            if name in self.ids:
                return str(self.ids[name])
            return {"uuid": str(uuid.uuid4()), "int": "1"}.get(conv, "x")

        return "/" + re.sub(r"<(?:(\w+):)?(\w+)>", sub, route)

    def probe(self, cls, method, url, raw):
        client = self.key_client if issubclass(cls, PublicApiView) else self.client
        cache.clear()
        result = None
        try:
            with transaction.atomic():
                resp = client.generic(method, url, raw, content_type="application/json")
                if "json" in (resp.get("Content-Type") or ""):
                    body = resp.json()
                else:
                    body = resp.content
                result = (resp.status_code, body)
                raise _Rollback
        except _Rollback:
            pass
        return result

    def test_every_mutating_route(self):
        routes = mutating_routes()
        drf = [(m, r, c) for m, r, c in routes if issubclass(c, APIView)]
        non_drf = {_name(c) for m, r, c in routes if not issubclass(c, APIView)}
        for name in non_drf:
            self.assertTrue(name.startswith(NON_DRF_MODULE_PREFIXES), f"{name} is not a DRF view")

        walked = {_name(c) for _, _, c in drf}
        pairs = {(m, r) for m, r, _ in drf}
        self.assertGreaterEqual(len(pairs), MIN_PAIRS)
        self.assertLessEqual(set(EXEMPT), walked, "an exemption names a class the walk never saw")
        self.assertLessEqual(MULTIPART_ONLY, walked)
        routes_seen = {route for route, _ in _walk(get_resolver().url_patterns)}
        self.assertLessEqual(
            {r for _, r in KNOWN_FAILURES_MIP_1014}, routes_seen, "a known failure is not a route"
        )
        fixed = [f"{m} /{r} (no longer routed)" for m, r in KNOWN_FAILURES_MIP_1014 - pairs]
        self.assertLessEqual(MALFORMED_READ_EARLY, pairs)

        # The guard is on exactly the classes walked, less the exemptions.
        guarded = {_name(c) for _, _, c in drf if _is_guarded(c)}
        self.assertEqual(guarded, walked - set(EXEMPT))
        self.assertEqual(
            MULTIPART_ONLY,
            {
                _name(c)
                for _, _, c in drf
                if not any(isinstance(p, JSONParser) for p in c().get_parsers())
            },
        )

        failures, exercised, still_failing = [], 0, []
        # DELETE last: nothing should survive a rollback, but a probe that deletes a
        # fixture must not be able to hide the routes after it if one did.
        ordered = sorted(drf, key=lambda t: (t[0] == "DELETE", t[1], t[0]))
        seen = set()
        for method, route, cls in ordered:
            if (method, route) in seen:
                continue
            seen.add((method, route))
            exercised += 1
            url = self.fill(route)
            name = _name(cls)
            where = f"{method} /{route} ({name})"
            # `{}` and `null` are a request with nothing in it: a 400 for a field that is
            # missing, or whatever the view answers for no body, but never a 500 (an
            # unhandled exception). Asked of every route, the exempt and the upload ones
            # too. A 5xx the view CHOOSES (503 "not configured") is an answer, not a crash.
            empty = self.probe(cls, method, url, "{}")
            known = (method, route) in KNOWN_FAILURES_MIP_1014
            if known:
                if empty[0] == 500:
                    still_failing.append(f"{method} /{route}: {empty}")
                else:
                    fixed.append(f"{method} /{route}")
            else:
                if empty[0] == 500:
                    failures.append(f"{where} `{{}}` answers {empty}")
                null = self.probe(cls, method, url, "null")
                if null[0] == 500:
                    failures.append(f"{where} `null` answers {null}")
            if name in EXEMPT:
                for raw in BAD:
                    got = self.probe(cls, method, url, raw)
                    if got[0] >= 500:
                        failures.append(f"{where} {raw}: {got}")
                continue
            if name in MULTIPART_ONLY:
                # No JSON parser, so the guard never reads a JSON body: a handler that
                # reads it gets DRF's 415, one that does not (a DELETE that takes
                # its ids from the query) answers as it does for `{}`.
                for raw in BAD + EMPTY:
                    got = self.probe(cls, method, url, raw)
                    if got[0] != empty[0] or got[0] >= 500:
                        failures.append(f"{where} {raw}: {got} but {{}} is {empty}")
                continue
            for raw in BAD:
                got = self.probe(cls, method, url, raw)
                if got != (400, REFUSED):
                    failures.append(f"{where} {raw}: {got}")
            got = self.probe(cls, method, url, MALFORMED)
            if (method, route) in MALFORMED_READ_EARLY:
                ok = got[0] == 400
            else:
                ok = got == (400, {"error": NOT_JSON})
            if not ok:
                failures.append(f"{where} malformed: {got}")
            if known:
                continue
            # Status, not body: a create answers with a fresh id each time (sequences
            # are not rolled back), and the body for `{}` is pinned per view elsewhere.
            for raw in EMPTY:
                got = self.probe(cls, method, url, raw)
                if got[0] != empty[0]:
                    failures.append(f"{where} {raw}: {got} but {{}} is {empty}")

        if still_failing:
            print("\nMIP-1014: `{}` still answers 500 on:\n  " + "\n  ".join(still_failing))
        # A member that stopped failing, or stopped being routed, must leave the set.
        failures += [
            f"{route}: take it out of KNOWN_FAILURES_MIP_1014 (it can only shrink)"
            for route in fixed
        ]
        self.assertGreaterEqual(exercised, MIN_PAIRS)
        self.assertEqual(failures, [], "\n" + "\n".join(failures))
