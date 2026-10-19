"""Rate limits on authenticated endpoints, and how they fail (MIP-724).

Rates are patched down per test; the cache is the suite's shared
LocMemCache, cleared around each test so one test's counts can't 429
another. A dead Redis is simulated by making the throttle cache raise
`ConnectionInterrupted`, which is what django-redis raises with
`IGNORE_EXCEPTIONS` off.
"""

from __future__ import annotations

import io
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase
from django_redis.exceptions import ConnectionInterrupted
from rest_framework.settings import api_settings

from origin.search_engine.agent_views import (
    AgentAskView,
    AgentDecideView,
    NoteSummaryView,
    ThreadSummaryView,
)
from origin.search_engine.views import SearchView
from origin.services import throttles
from origin.services.throttles import (
    AuthThrottle,
    InviteThrottle,
    SearchThrottle,
    UserBurstThrottle,
    UserSustainedThrottle,
    denied_count,
    parse_rate,
)
from origin.tests.test_base import BaseAPITestCase
from origin.views.common import auth_views
from origin.views.common.auth_views import SignInBurstThrottle
from origin.views.common.team_views import InviteTeamMembersView
from origin.views.common.user_views import AccountExportView
from origin.views.task.task_views import TaskDependencyBatchListView
from origin.views.utils.batch_limits import (
    MAX_GROUP_MEMBERS_PER_ADD,
    MAX_INVITE_EMAILS,
    MAX_MENTIONS_PER_COMMENT,
)

_ME = "/api/v2/user/me/"
_SIGNIN = "/api/v2/user/signin/"
_RESET = "/api/v2/user/password-reset/request/"
_INVITE = "/api/v2/team/invite/"


class _Dead:
    """A throttle cache whose every call fails like an unreachable Redis."""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise ConnectionInterrupted(connection=None)

        return fail


class _ThrottleCase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # The breaker is per process; a test that trips it must not
        # leave the next one skipping the cache.
        self.addCleanup(setattr, throttles, "_down_until", 0.0)

    def dead_cache(self):
        return mock.patch.object(throttles, "caches", _DeadCaches())


class _DeadCaches:
    """Stands in for `django.core.cache.caches`; counts how often it was
    asked for a cache, so a test can see the breaker stop the asking."""

    def __init__(self):
        self.asked = 0

    def __getitem__(self, alias):
        self.asked += 1
        return _Dead()


class TestDefaultPerUserThrottle(_ThrottleCase):
    def test_every_authenticated_view_gets_it_by_default(self):
        self.assertEqual(
            list(api_settings.DEFAULT_THROTTLE_CLASSES),
            [UserBurstThrottle, UserSustainedThrottle],
        )

    def test_a_user_past_the_burst_rate_gets_429_with_retry_after(self):
        self.authenticate()
        with mock.patch.object(UserBurstThrottle, "rate", "3/min"):
            codes = [self.client.get(_ME).status_code for _ in range(4)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3], 429)
        with mock.patch.object(UserBurstThrottle, "rate", "3/min"):
            r = self.client.get(_ME)
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r["Retry-After"]), 0)
        self.assertLessEqual(int(r["Retry-After"]), 60)

    def test_the_budget_is_per_user(self):
        with mock.patch.object(UserBurstThrottle, "rate", "2/min"):
            self.authenticate(self.user)
            self.client.get(_ME)
            self.client.get(_ME)
            self.assertEqual(self.client.get(_ME).status_code, 429)
            self.authenticate(self.user2)
            self.assertEqual(self.client.get(_ME).status_code, 200)

    def test_the_sustained_limit_applies_too(self):
        self.authenticate()
        with mock.patch.object(UserSustainedThrottle, "rate", "2/hour"):
            codes = [self.client.get(_ME).status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    def test_anonymous_requests_are_left_to_the_per_ip_throttles(self):
        with mock.patch.object(UserBurstThrottle, "rate", "1/min"):
            codes = [self.client.get("/api/v2/health/").status_code for _ in range(3)]
        self.assertNotIn(429, codes)

    def test_a_dead_cache_lets_requests_through(self):
        self.authenticate()
        with self.dead_cache(), self.assertLogs("origin.services.throttles", "WARNING") as logs:
            r = self.client.get(_ME)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any("action=allow" in line for line in logs.output))

    def test_a_dead_cache_trips_the_breaker_so_later_requests_skip_it(self):
        self.authenticate()
        dead = _DeadCaches()
        with (
            mock.patch.object(throttles, "caches", dead),
            self.assertLogs("origin.services.throttles", "WARNING"),
        ):
            self.client.get(_ME)
            tried_first = dead.asked
            self.client.get(_ME)
        self.assertGreater(tried_first, 0)
        self.assertEqual(dead.asked, tried_first)


class TestAuthThrottlesFailClosed(_ThrottleCase):
    def test_every_auth_throttle_is_fail_closed(self):
        classes = [
            auth_views.SignInBurstThrottle,
            auth_views.SignInSustainedThrottle,
            auth_views.SignUpThrottle,
            auth_views.PasswordResetRequestThrottle,
            auth_views.PasswordResetConfirmThrottle,
            auth_views.ResendVerificationThrottle,
            auth_views.DemoSignInThrottle,
        ]
        for cls in classes:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, AuthThrottle))
                self.assertTrue(cls.fail_closed)

    def test_sign_in_refuses_when_the_cache_is_down(self):
        body = {"email": "nobody@example.com", "password": "wrong-password"}
        with self.dead_cache(), self.assertLogs("origin.services.throttles", "ERROR") as logs:
            r = self.client.post(_SIGNIN, body)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r["Retry-After"], "60")
        self.assertTrue(any("action=refuse" in line for line in logs.output))

    def test_password_reset_refuses_when_the_cache_is_down(self):
        with self.dead_cache(), self.assertLogs("origin.services.throttles", "ERROR"):
            r = self.client.post(_RESET, {"email": "nobody@example.com"})
        self.assertEqual(r.status_code, 429)

    def test_the_breaker_does_not_hold_sign_in_shut(self):
        # Something else tripped the breaker; Redis is back. Sign-in
        # must go back to the cache, not stay refused for 5 s.
        throttles._trip_breaker()
        body = {"email": "nobody@example.com", "password": "wrong-password"}
        self.assertEqual(self.client.post(_SIGNIN, body).status_code, 401)

    def test_sign_in_still_counts_when_the_cache_is_up(self):
        body = {"email": "nobody@example.com", "password": "wrong-password"}
        with mock.patch.object(SignInBurstThrottle, "rate", "1/min"):
            codes = [self.client.post(_SIGNIN, body).status_code for _ in range(2)]
        self.assertEqual(codes, [401, 429])


class TestDenialsAreLoggedAndCounted(_ThrottleCase):
    def test_a_429_logs_the_scope_user_and_path_and_bumps_the_count(self):
        self.authenticate()
        with (
            mock.patch.object(UserBurstThrottle, "rate", "1/min"),
            self.assertLogs("origin.services.throttles", "WARNING") as logs,
        ):
            self.client.get(_ME)
            self.client.get(_ME)
            self.client.get(_ME)
        denied = [line for line in logs.output if "throttle.denied" in line]
        self.assertEqual(len(denied), 2)
        self.assertIn("scope=user_burst", denied[0])
        self.assertIn(f"user={self.user.pk}", denied[0])
        self.assertIn(f"path={_ME}", denied[0])
        self.assertEqual(denied_count("user_burst"), 2)

    def test_an_auth_429_is_counted_by_ip(self):
        body = {"email": "nobody@example.com", "password": "wrong-password"}
        with (
            mock.patch.object(SignInBurstThrottle, "rate", "1/min"),
            self.assertLogs("origin.services.throttles", "WARNING") as logs,
        ):
            self.client.post(_SIGNIN, body)
            self.client.post(_SIGNIN, body)
        self.assertTrue(any("scope=signin_burst ip=" in line for line in logs.output))
        self.assertEqual(denied_count("signin_burst"), 1)

    def test_the_command_reports_the_counts(self):
        self.authenticate()
        with (
            mock.patch.object(UserBurstThrottle, "rate", "1/min"),
            self.assertLogs("origin.services.throttles", "WARNING"),
        ):
            self.client.get(_ME)
            self.client.get(_ME)
        out = io.StringIO()
        call_command("throttle_denials", stdout=out)
        self.assertIn("user_burst: 1", out.getvalue())

    def test_every_throttle_on_a_route_records_its_denials(self):
        # A stock DRF throttle 429s silently and reads the IGNORE_EXCEPTIONS
        # cache; one added later would be invisible to `throttle_denials`.
        from django.urls import get_resolver

        def views(patterns):
            for p in patterns:
                if hasattr(p, "url_patterns"):
                    yield from views(p.url_patterns)
                elif cls := getattr(p.callback, "cls", None):
                    yield cls

        seen = {t for v in views(get_resolver().url_patterns) for t in v.throttle_classes}
        self.assertIn(throttles.UserBurstThrottle, seen)
        for t in seen:
            with self.subTest(throttle=t.__name__):
                self.assertTrue(
                    issubclass(t, (throttles.WindowRateThrottle, throttles.RecordedThrottleMixin))
                )

    def test_the_command_says_so_when_nothing_was_refused(self):
        out = io.StringIO()
        call_command("throttle_denials", "--hours", "2", stdout=out)
        self.assertIn("No 429s", out.getvalue())


class TestScopedThrottles(_ThrottleCase):
    def test_expensive_views_carry_their_scope_and_keep_the_defaults(self):
        expected = {
            SearchView: "search",
            AgentAskView: "agent",
            AgentDecideView: "agent",
            NoteSummaryView: "agent_summary",
            ThreadSummaryView: "agent_summary",
            AccountExportView: "export",
            InviteTeamMembersView: "invite",
            TaskDependencyBatchListView: "batch_read",
        }
        for view, scope in expected.items():
            with self.subTest(view=view.__name__):
                scopes = [t.scope for t in view.throttle_classes]
                self.assertIn(scope, scopes)
                self.assertIn(UserBurstThrottle, view.throttle_classes)
                self.assertIn(UserSustainedThrottle, view.throttle_classes)

    def test_every_window_scope_has_a_rate_in_settings(self):
        # A scope with no rate raises ImproperlyConfigured on the first
        # request to its view — in production, not at import.
        rates = api_settings.DEFAULT_THROTTLE_RATES
        for cls in _all_subclasses(throttles.WindowRateThrottle):
            if cls.rate:
                continue
            with self.subTest(scope=cls.scope):
                self.assertIn(cls.scope, rates)
                parse_rate(rates[cls.scope])

    def test_search_is_throttled(self):
        self.authenticate()
        body = {"query": "x", "team_id": str(self.team.team_id)}
        with (
            mock.patch.object(SearchThrottle, "rate", "1/min"),
            mock.patch("origin.search_engine.views.search", return_value={"results": []}),
        ):
            self.client.post("/api/v2/search/", body, format="json")
            r = self.client.post("/api/v2/search/", body, format="json")
        self.assertEqual(r.status_code, 429)

    def test_export_is_throttled(self):
        self.authenticate()
        with mock.patch.object(throttles.ExportThrottle, "rate", "1/hour"):
            first = self.client.get("/api/v2/user/account/export/")
            second = self.client.get("/api/v2/user/account/export/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)

    def test_agent_scope_counts_only_posts(self):
        # `methods` is the contract: a GET on an agent-scoped view must
        # not spend the ask budget; a POST must.
        get = mock.Mock(method="GET", user=self.user)
        post = mock.Mock(method="POST", user=self.user)
        with mock.patch.object(throttles.AgentThrottle, "rate", "1/min"):
            throttle = throttles.AgentThrottle()
            self.assertTrue(throttle.allow_request(get, None))
            self.assertTrue(throttle.allow_request(get, None))
            self.assertTrue(throttle.allow_request(post, None))
            with self.assertLogs("origin.services.throttles", "WARNING"):
                self.assertFalse(throttle.allow_request(post, None))


class TestInviteBudgetIsInAddresses(_ThrottleCase):
    def _invite(self, emails):
        return self.client.post(
            _INVITE, {"team_id": str(self.team.team_id), "emails": emails}, format="json"
        )

    def test_one_request_cannot_carry_more_than_the_cap(self):
        self.authenticate()
        emails = [f"p{i}@example.com" for i in range(MAX_INVITE_EMAILS + 1)]
        with mock.patch("origin.views.common.team_views.send_templated_email") as send:
            r = self._invite(emails)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["limit"], MAX_INVITE_EMAILS)
        send.assert_not_called()

    def test_the_cap_itself_is_allowed(self):
        self.authenticate()
        emails = [f"p{i}@example.com" for i in range(MAX_INVITE_EMAILS)]
        with mock.patch("origin.views.common.team_views.send_templated_email"):
            r = self._invite(emails)
        self.assertEqual(r.status_code, 200)

    def test_addresses_not_requests_spend_the_budget(self):
        self.authenticate()
        with (
            mock.patch.object(InviteThrottle, "rate", "5/hour"),
            mock.patch("origin.views.common.team_views.send_templated_email") as send,
        ):
            first = self._invite([f"a{i}@example.com" for i in range(4)])
            second = self._invite(["b0@example.com", "b1@example.com"])
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(send.call_count, 4)

    def test_duplicates_in_one_request_cost_once(self):
        throttle = InviteThrottle()
        request = mock.Mock(data={"emails": ["A@x.com", "a@x.com ", "b@x.com", ""]})
        self.assertEqual(throttle.cost(request), 2)


class TestPerItemCaps(_ThrottleCase):
    def test_a_comment_cannot_mention_more_than_the_cap(self):
        from origin.models.project.prj_models import ProjectMaster, ProjectMembers
        from origin.models.task.task_models import TaskCommentMentionFact, TaskMaster

        project = ProjectMaster.objects.create(project_name="P", team=self.team, owner=self.user)
        ProjectMembers.objects.create(project=project, attendee=self.user)
        task = TaskMaster.objects.create(team=self.team, project=project, title="T")
        self.authenticate()
        ids = [str(self.user2.id)] * (MAX_MENTIONS_PER_COMMENT + 1)
        r = self.client.post(
            "/api/v2/task/comment/mention/",
            {
                "task_id": task.task_id,
                "team_id": str(self.team.team_id),
                "comment_id": 1,
                "mentioned_user_ids": ids,
            },
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        # Refused before the per-id loop, not partway through it: the
        # loop would 400 too (duplicate ids), but only after writing rows.
        self.assertEqual(r.json().get("limit"), MAX_MENTIONS_PER_COMMENT)
        self.assertFalse(TaskCommentMentionFact.objects.filter(task=task).exists())

    def test_a_group_add_cannot_carry_more_than_the_cap(self):
        self.authenticate()
        ids = [str(self.user2.id)] * (MAX_GROUP_MEMBERS_PER_ADD + 1)
        r = self.client.post(
            "/api/v2/mention-group/members/",
            {"group_id": 1, "user_ids": ids},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json().get("limit"), MAX_GROUP_MEMBERS_PER_ADD)


class TestRateParsing(SimpleTestCase):
    def test_spellings(self):
        self.assertEqual(parse_rate("600/min"), (600, 60))
        self.assertEqual(parse_rate("20000/hour"), (20000, 3600))
        self.assertEqual(parse_rate("5/h"), (5, 3600))
        self.assertEqual(parse_rate("2/day"), (2, 86400))
        self.assertEqual(parse_rate("1/s"), (1, 1))


class TestRatesComeFromSettings(SimpleTestCase):
    def test_env_names_feed_the_rates(self):
        # The rates are read from the environment at settings import; this
        # pins the scopes a deploy can tune, so renaming one is a diff.
        self.assertTrue(
            {
                "user_burst",
                "user_sustained",
                "search",
                "agent",
                "agent_summary",
                "export",
                "invite",
                "batch_read",
            }
            <= set(api_settings.DEFAULT_THROTTLE_RATES)
        )


def _all_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)
