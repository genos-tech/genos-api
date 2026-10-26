"""A person's own security log (MIP-745).

`GET /api/v2/user/security-events/` shows people the rows that are theirs: the
sign-ins, exports and connected accounts that carry no team, so the owner's
team log never lists them. "Was that sign-in from Ohio me?"

The thing worth testing is the scoping filter, because it is the one line that
decides whose rows leave the database. It is tested from both sides: rows that
must come back, and (more importantly) rows that must not. The hard cases are
the ones that LOOK like the caller's: a team owner's `member.added` that targets
them, someone else's failed sign-in, an address that merely resembles theirs.
"""

from django.contrib.auth import get_user_model

from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.services.security_events import record_security_event
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

URL = "/api/v2/user/security-events/"


class MySecurityEventTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.user)

    def _record(self, action, **kw):
        return record_security_event(action, **kw)

    def _get(self, **params):
        res = self.client.get(URL, params)
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        return res.data

    def _ids(self, **params):
        return {e["id"] for e in self._get(**params)["events"]}

    # ── rows that are mine ────────────────────────────────────────────

    def test_my_own_actions_are_listed(self):
        signin = self._record(SecurityAction.SIGNIN_SUCCEEDED, actor=self.user)
        export = self._record(SecurityAction.EXPORT, actor=self.user)
        oauth = self._record(SecurityAction.OAUTH_CONNECTED, actor=self.user)

        ids = self._ids()

        # An export also raises its own `alert.export` row with me as the actor,
        # which belongs in my log too, so this asserts the rows I made are there
        # and that EVERYTHING returned is mine, not an exact set.
        self.assertTrue({signin.event_id, export.event_id, oauth.event_id} <= ids)
        for event in SecurityEvent.objects.filter(event_id__in=ids):
            self.assertEqual(event.actor_id, self.user.pk)

    def test_a_failed_sign_in_against_my_account_is_listed_with_the_attackers_ip(self):
        """Nobody was signed in, so there is no actor; the account it was aimed
        at is the target. The IP is shown: it is the attempter's, not mine."""
        row = self._record(
            SecurityAction.SIGNIN_FAILED,
            actor_label="someone typed this",
            target_type="user",
            target_id=self.user.pk,
        )

        events = self._get()["events"]

        self.assertEqual([e["id"] for e in events], [row.event_id])
        self.assertIn("ip", events[0])

    def test_a_password_reset_request_aimed_at_me_is_listed(self):
        row = self._record(
            SecurityAction.PASSWORD_RESET_REQUESTED,
            actor_label="a stranger",
            target_type="user",
            target_id=self.user.pk,
        )

        self.assertEqual(self._ids(), {row.event_id})

    def test_my_api_key_events_are_listed_although_they_carry_a_team(self):
        """A key is bound to a team, so its rows have a team_id, and are still
        mine because I am the actor."""
        used = self._record(SecurityAction.API_KEY_USED, actor=self.user, team_id=self.team.team_id)

        self.assertEqual(self._ids(), {used.event_id})

    # ── rows that must NOT be mine ────────────────────────────────────

    def test_another_persons_rows_are_never_listed(self):
        self._record(SecurityAction.SIGNIN_SUCCEEDED, actor=self.user2)
        self._record(SecurityAction.EXPORT, actor=self.user2)

        self.assertEqual(self._ids(), set())

    def test_a_failed_sign_in_against_someone_else_is_not_listed(self):
        self._record(
            SecurityAction.SIGNIN_FAILED,
            actor_label="x",
            target_type="user",
            target_id=self.user2.pk,
        )

        self.assertEqual(self._ids(), set())

    def test_a_team_owners_record_of_acting_on_me_is_not_listed(self):
        """`member.added` targets the person added, and carries a team. It is
        the owner's log entry, not mine; a filter of 'target is me' would leak
        it, which is what the team_id condition is for."""
        for action in (
            SecurityAction.MEMBER_ADDED,
            SecurityAction.MEMBER_REMOVED,
            SecurityAction.ROLE_CHANGED,
        ):
            self._record(
                action,
                actor=self.user2,
                team_id=self.team.team_id,
                target_type="user",
                target_id=self.user.pk,
            )

        self.assertEqual(self._ids(), set())

    def test_a_row_that_only_resembles_my_target_id_is_not_listed(self):
        """The target id is compared whole, as the string it is stored as."""
        self._record(
            SecurityAction.SIGNIN_FAILED,
            actor_label="x",
            target_type="user",
            target_id=str(self.user.pk)[:-1],
        )
        self._record(
            SecurityAction.SIGNIN_FAILED,
            actor_label="x",
            target_type="user",
            target_id=str(self.user.pk) + "0",
        )

        self.assertEqual(self._ids(), set())

    def test_a_non_user_target_with_my_id_is_not_listed(self):
        """Only a target of type 'user' names an account."""
        self._record(
            SecurityAction.WEBHOOK_CREATED,
            actor=self.user2,
            target_type="webhook",
            target_id=self.user.pk,
        )

        self.assertEqual(self._ids(), set())

    def test_my_typed_address_in_someone_elses_row_is_not_enough(self):
        """The label is what a stranger typed. It is not an identity."""
        self._record(
            SecurityAction.SIGNIN_FAILED,
            actor_label=self.user.email,
            target_type="",
            target_id="",
        )

        self.assertEqual(self._ids(), set())

    # ── paging, filters, access ───────────────────────────────────────

    def test_newest_first_with_a_cursor_that_walks_every_row_once(self):
        made = [self._record(SecurityAction.SIGNIN_SUCCEEDED, actor=self.user) for _ in range(5)]

        seen, before = [], None
        for _ in range(10):
            body = self._get(limit=2, **({"before": before} if before else {}))
            seen += [e["id"] for e in body["events"]]
            before = body["nextBefore"]
            if before is None:
                break

        self.assertEqual(seen, [e.event_id for e in reversed(made)])

    def test_the_last_page_has_no_cursor(self):
        self._record(SecurityAction.SIGNIN_SUCCEEDED, actor=self.user)

        self.assertIsNone(self._get(limit=5)["nextBefore"])

    def test_the_page_size_is_capped(self):
        for _ in range(3):
            self._record(SecurityAction.SIGNIN_SUCCEEDED, actor=self.user)

        self.assertEqual(len(self._get(limit=10**9)["events"]), 3)

    def test_it_can_be_narrowed_to_one_action(self):
        self._record(SecurityAction.SIGNIN_SUCCEEDED, actor=self.user)
        export = self._record(SecurityAction.EXPORT, actor=self.user)

        self.assertEqual(self._ids(action=SecurityAction.EXPORT.value), {export.event_id})

    def test_a_bad_cursor_or_action_is_a_400_not_a_500(self):
        self.assertEqual(self.client.get(URL, {"before": "abc"}).status_code, 400)
        self.assertEqual(self.client.get(URL, {"action": "nope"}).status_code, 400)

    def test_it_requires_a_signed_in_user(self):
        self.client.credentials()

        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_an_api_key_cannot_read_it(self):
        """A leaked key must not be able to read the trail that would show it.

        Held by the project's default authenticator (JWT only, `settings.py`),
        which the view's own `authentication_classes` repeats to state the
        intent, as the team log does. Removing that line changes nothing, so
        this test pins the outcome rather than the line."""
        from origin.models.common.api_key_models import SCOPE_READ, ApiKey, generate_key, hash_key

        raw = generate_key()
        ApiKey.objects.create(
            user=self.user,
            team=self.team,
            name="bot",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=SCOPE_READ,
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")

        self.assertEqual(self.client.get(URL).status_code, 401)

    def test_the_team_log_is_unchanged_and_still_excludes_personal_rows(self):
        """The paging code is now shared with the team endpoint, so its behaviour
        is pinned here too: it lists the team's rows and none of mine."""
        self.team.owner = self.user
        self.team.save(update_fields=["owner"])
        team_row = self._record(
            SecurityAction.MEMBER_ADDED, actor=self.user, team_id=self.team.team_id
        )
        personal = self._record(SecurityAction.SIGNIN_SUCCEEDED, actor=self.user)

        res = self.client.get("/api/v2/team/security-events/", {"team_id": str(self.team.team_id)})

        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        ids = {e["id"] for e in res.data["events"]}
        self.assertIn(team_row.event_id, ids)
        self.assertNotIn(personal.event_id, ids)
