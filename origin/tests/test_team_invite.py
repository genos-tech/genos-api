"""Tests for the invite-by-email flow:

- POST /api/v2/team/invite/          (owner-only send)
- GET  /api/v2/team/invite/preview/  (public token preview)
- POST /api/v2/team/invite/accept/   (authed, email-locked accept)
- POST /api/v2/user/signup/          (invite_token auto-verify + auto-join)
"""

import hashlib
import secrets
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.utils import timezone
from rest_framework import status

from origin.models.common.invite_models import TeamInvite
from origin.models.common.team_models import TeamMembers
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


def _make_invite(team, email, *, inviter=None, status_="pending", minutes=10080):
    """Create a TeamInvite and return (invite, raw_token)."""
    raw = secrets.token_urlsafe(32)
    invite = TeamInvite.objects.create(
        team=team,
        invited_email=email.lower(),
        invited_by=inviter,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=timezone.now() + timedelta(minutes=minutes),
        status=status_,
    )
    return invite, raw


class TestInviteCreate(BaseAPITestCase):
    """POST /api/v2/team/invite/ — owner only.

    `send_templated_email` is patched out: rendering an email template
    inside a test-client request trips a Django/Python 3.14
    template-Context copy bug in `store_rendered_templates`. Mocking the
    send also lets us assert exactly which addresses an email goes to.
    """

    def setUp(self):
        super().setUp()
        patcher = patch("origin.views.common.team_views.send_templated_email")
        self.mock_send = patcher.start()
        self.addCleanup(patcher.stop)

    def test_owner_invites_new_email(self):
        self.authenticate(self.user)
        response = self.client.post(
            "/api/v2/team/invite/",
            {"team_id": str(self.team.team_id), "emails": ["new@example.com"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"]
        self.assertEqual(results[0]["status"], "sent")
        self.assertTrue(
            TeamInvite.objects.filter(
                team=self.team, invited_email="new@example.com", status="pending"
            ).exists()
        )
        self.mock_send.assert_called_once()
        self.assertEqual(self.mock_send.call_args.kwargs["to"], "new@example.com")

    def test_non_owner_forbidden(self):
        # user2 is a member but not the owner.
        self.authenticate(self.user2)
        response = self.client.post(
            "/api/v2/team/invite/",
            {"team_id": str(self.team.team_id), "emails": ["x@example.com"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.mock_send.assert_not_called()

    def test_already_member_skipped(self):
        self.authenticate(self.user)
        response = self.client.post(
            "/api/v2/team/invite/",
            {"team_id": str(self.team.team_id), "emails": [self.user2.email]},
            format="json",
        )
        self.assertEqual(response.data["results"][0]["status"], "already_member")
        self.mock_send.assert_not_called()

    def test_invalid_email_reported(self):
        self.authenticate(self.user)
        response = self.client.post(
            "/api/v2/team/invite/",
            {"team_id": str(self.team.team_id), "emails": ["not-an-email"]},
            format="json",
        )
        self.assertEqual(response.data["results"][0]["status"], "invalid_email")
        self.mock_send.assert_not_called()

    def test_reinvite_refreshes_and_resends(self):
        invite, old_raw = _make_invite(self.team, "again@example.com", inviter=self.user)
        old_hash = invite.token_hash
        self.authenticate(self.user)
        response = self.client.post(
            "/api/v2/team/invite/",
            {"team_id": str(self.team.team_id), "emails": ["again@example.com"]},
            format="json",
        )
        self.assertEqual(response.data["results"][0]["status"], "already_invited_resent")
        invite.refresh_from_db()
        self.assertNotEqual(invite.token_hash, old_hash)  # token rotated
        self.assertEqual(
            TeamInvite.objects.filter(team=self.team, invited_email="again@example.com").count(),
            1,  # no duplicate row
        )
        self.mock_send.assert_called_once()

    def test_dedupes_repeated_emails(self):
        self.authenticate(self.user)
        response = self.client.post(
            "/api/v2/team/invite/",
            {
                "team_id": str(self.team.team_id),
                "emails": ["dup@example.com", "DUP@example.com"],
            },
            format="json",
        )
        self.assertEqual(len(response.data["results"]), 1)
        self.mock_send.assert_called_once()


class TestInvitePreview(BaseAPITestCase):
    """GET /api/v2/team/invite/preview/ — public."""

    def test_no_account(self):
        _, raw = _make_invite(self.team, "ghost@example.com")
        response = self.client.get("/api/v2/team/invite/preview/", {"token": raw})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["valid"])
        self.assertEqual(response.data["status"], "no_account")
        self.assertEqual(response.data["invited_email"], "ghost@example.com")
        self.assertEqual(response.data["team_name"], self.team.team_name)

    def test_account_exists(self):
        _, raw = _make_invite(self.team, self.user2.email)
        response = self.client.get("/api/v2/team/invite/preview/", {"token": raw})
        self.assertEqual(response.data["status"], "account_exists")

    def test_invalid_token_reveals_nothing(self):
        response = self.client.get("/api/v2/team/invite/preview/", {"token": "bogus"})
        self.assertFalse(response.data["valid"])
        self.assertEqual(response.data["status"], "invalid")
        self.assertNotIn("invited_email", response.data)

    def test_expired_token(self):
        _, raw = _make_invite(self.team, "late@example.com", minutes=-5)
        response = self.client.get("/api/v2/team/invite/preview/", {"token": raw})
        self.assertFalse(response.data["valid"])
        self.assertEqual(response.data["status"], "expired")


class TestInviteAccept(BaseAPITestCase):
    """POST /api/v2/team/invite/accept/ — authed, email-locked."""

    def setUp(self):
        super().setUp()
        # A user who is NOT yet a member, matching an invite below.
        self.invitee = User.objects.create_user(
            username="invitee",
            email="invitee@example.com",
            password="pass12345",
        )

    def test_accept_joins_team(self):
        _, raw = _make_invite(self.team, self.invitee.email, inviter=self.user)
        self.authenticate(self.invitee)
        response = self.client.post("/api/v2/team/invite/accept/", {"token": raw}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["team_id"], str(self.team.team_id))
        self.assertTrue(
            TeamMembers.objects.filter(
                team=self.team, attendee=self.invitee, is_deleted=False
            ).exists()
        )

    def test_email_mismatch_rejected(self):
        _, raw = _make_invite(self.team, "someone-else@example.com", inviter=self.user)
        self.authenticate(self.invitee)  # invitee's email differs
        response = self.client.post("/api/v2/team/invite/accept/", {"token": raw}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "email_mismatch")

    def test_expired_rejected(self):
        _, raw = _make_invite(self.team, self.invitee.email, minutes=-1)
        self.authenticate(self.invitee)
        response = self.client.post("/api/v2/team/invite/accept/", {"token": raw}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "expired")

    def test_reaccept_invalid(self):
        invite, raw = _make_invite(self.team, self.invitee.email, status_="accepted")
        self.authenticate(self.invitee)
        response = self.client.post("/api/v2/team/invite/accept/", {"token": raw}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["detail"], "invalid")


class TestInviteSignup(BaseAPITestCase):
    """POST /api/v2/user/signup/ with an invite_token."""

    def test_signup_with_invite_auto_verifies(self):
        """A valid invite token skips email verification and issues a JWT.
        The team-join itself is deferred to the accept endpoint (driven by
        the frontend consume funnel), so the new-account and existing-account
        paths share one post-auth join path."""
        invite, raw = _make_invite(self.team, "fresh@example.com", inviter=self.user)
        response = self.client.post(
            "/api/v2/user/signup/",
            {
                "username": "Fresh",
                "email": "fresh@example.com",
                "password": "strongpass123",
                "invite_token": raw,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # The invite-signup path returns a Django JsonResponse (mirrors
        # DemoSignInView), so parse the body rather than using .data.
        body = response.json()
        self.assertIn("access", body)
        new_user = User.objects.get(email="fresh@example.com")
        self.assertTrue(new_user.is_email_verified)  # verification skipped
        # Join is NOT performed at signup — it stays pending for the funnel.
        invite.refresh_from_db()
        self.assertEqual(invite.status, "pending")
        self.assertFalse(
            TeamMembers.objects.filter(
                team=self.team, attendee=new_user, is_deleted=False
            ).exists()
        )
        # No verification email on the invite path.
        self.assertEqual(len(mail.outbox), 0)

    def test_signup_token_email_mismatch_rejected(self):
        _, raw = _make_invite(self.team, "intended@example.com", inviter=self.user)
        response = self.client.post(
            "/api/v2/user/signup/",
            {
                "username": "Imposter",
                "email": "different@example.com",
                "password": "strongpass123",
                "invite_token": raw,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # No user created for the mismatched email (validated before save).
        self.assertFalse(User.objects.filter(email="different@example.com").exists())
