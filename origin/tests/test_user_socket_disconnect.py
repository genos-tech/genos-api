"""A person's open sockets are closed when their credentials end (MIP-2241).

genos-sockets checks the token only when a client connects, so a session stolen before a
password reset, an account deletion or an operator cutoff kept receiving pushes. After the
change commits genos-api posts `{"userId"}` to `/internal/user/disconnect`.

What matters here: the post is signed and carries the id only; it is sent after commit and
never when the change rolled back; a failing sockets never breaks the caller; it is a no-op
where sockets is not configured; and every ending path (password reset,
`end_all_credentials`, operator cutoff, account deletion) schedules it for that user only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from io import StringIO
from unittest import mock

import requests
from django.core.cache import cache
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from origin.services import realtime_push
from origin.services.account_deletion import delete_account
from origin.services.credential_revocation import end_all_credentials
from origin.services.realtime_push import USER_DISCONNECT_PATH, push_user_disconnect
from origin.tests.test_base import BaseAPITestCase

SOCKETS = {"SOCKETS_INTERNAL_URL": "http://sockets.invalid", "SOCKETS_INTERNAL_SECRET": "s3cret"}
_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-user-socket-disconnect",
    }
}
POST = "origin.services.realtime_push.requests.post"


def _ok():
    resp = mock.Mock()
    resp.status_code = 200
    return resp


def _disconnects(post):
    """(user ids) of every disconnect post made."""
    ids = []
    for call in post.call_args_list:
        if call.args[0].endswith(USER_DISCONNECT_PATH):
            ids.append(json.loads(call.kwargs["data"])["userId"])
    return ids


@override_settings(**SOCKETS)
class PushTests(TestCase):
    def test_it_posts_the_id_only_signed_after_commit(self):
        with mock.patch(POST, return_value=_ok()) as post:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                push_user_disconnect("u-1")
            post.assert_not_called()  # nothing before the commit
            for cb in callbacks:
                cb()
        (call,) = post.call_args_list
        self.assertEqual(call.args[0], "http://sockets.invalid" + USER_DISCONNECT_PATH)
        self.assertEqual(json.loads(call.kwargs["data"]), {"userId": "u-1"})
        headers = call.kwargs["headers"]
        ts = headers[realtime_push.TIMESTAMP_HEADER]
        self.assertEqual(
            headers[realtime_push.SIGNATURE_HEADER],
            realtime_push._sign("s3cret", ts, call.kwargs["data"]),
        )

    def test_a_rolled_back_change_disconnects_nobody(self):
        with mock.patch(POST, return_value=_ok()) as post:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        push_user_disconnect("u-1")
                        raise RuntimeError("rollback")
                except RuntimeError:
                    pass
        post.assert_not_called()

    def test_a_failing_sockets_is_logged_and_never_raises(self):
        with mock.patch(POST, side_effect=requests.ConnectionError("down")):
            with self.assertLogs("origin.services.realtime_push", "WARNING"):
                with self.captureOnCommitCallbacks(execute=True):
                    push_user_disconnect("u-1")

    def test_a_non_200_is_logged(self):
        bad = mock.Mock(status_code=404)
        with mock.patch(POST, return_value=bad):
            with self.assertLogs("origin.services.realtime_push", "WARNING"):
                with self.captureOnCommitCallbacks(execute=True):
                    push_user_disconnect("u-1")

    @override_settings(SOCKETS_INTERNAL_URL="")
    def test_a_no_op_when_sockets_is_not_configured(self):
        with mock.patch(POST) as post:
            with self.captureOnCommitCallbacks(execute=True):
                push_user_disconnect("u-1")
        post.assert_not_called()

    @override_settings(SOCKETS_INTERNAL_SECRET="")
    def test_a_no_op_without_a_secret(self):
        with mock.patch(POST) as post:
            with self.captureOnCommitCallbacks(execute=True):
                push_user_disconnect("u-1")
        post.assert_not_called()


@override_settings(CACHES=_LOCMEM_CACHE, **SOCKETS)
class EndingPathsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def test_end_all_credentials_disconnects_that_user_only(self):
        with mock.patch(POST, return_value=_ok()) as post:
            with self.captureOnCommitCallbacks(execute=True):
                end_all_credentials(self.user, by="test")
        self.assertEqual(_disconnects(post), [str(self.user.pk)])

    def test_a_password_reset_disconnects_the_member(self):
        raw = "reset-token-under-test"
        self.user.password_reset_token_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.user.password_reset_token_expires_at = timezone.now() + timedelta(hours=1)
        self.user.save(
            update_fields=["password_reset_token_hash", "password_reset_token_expires_at"]
        )
        with mock.patch(POST, return_value=_ok()) as post:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(
                    reverse("password_reset_confirm"),
                    {"token": raw, "new_password": "a-brand-new-password-42"},
                    format="json",
                )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(_disconnects(post), [str(self.user.pk)])

    def test_an_invalid_reset_disconnects_nobody(self):
        with mock.patch(POST, return_value=_ok()) as post:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(
                    reverse("password_reset_confirm"),
                    {"token": "wrong", "new_password": "a-brand-new-password-42"},
                    format="json",
                )
        self.assertEqual(_disconnects(post), [])

    def test_an_operator_cutoff_disconnects_the_user(self):
        with mock.patch(
            "origin.management.commands.operator_cutoff.getpass.getuser", return_value="ops"
        ):
            with mock.patch(POST, return_value=_ok()) as post:
                with self.captureOnCommitCallbacks(execute=True):
                    call_command(
                        "operator_cutoff",
                        "user",
                        str(self.user.pk),
                        "--reason",
                        "stolen session",
                        stdout=StringIO(),
                        stderr=StringIO(),
                    )
        self.assertEqual(_disconnects(post), [str(self.user.pk)])

    def test_a_dry_run_cutoff_disconnects_nobody(self):
        with mock.patch(
            "origin.management.commands.operator_cutoff.getpass.getuser", return_value="ops"
        ):
            with mock.patch(POST, return_value=_ok()) as post:
                with self.captureOnCommitCallbacks(execute=True):
                    call_command(
                        "operator_cutoff",
                        "user",
                        str(self.user.pk),
                        "--reason",
                        "x",
                        "--dry-run",
                        stdout=StringIO(),
                        stderr=StringIO(),
                    )
        self.assertEqual(_disconnects(post), [])

    def test_account_deletion_disconnects_the_deleted_person(self):
        with mock.patch(POST, return_value=_ok()) as post:
            with self.captureOnCommitCallbacks(execute=True):
                delete_account(self.user2)
        self.assertEqual(_disconnects(post), [str(self.user2.pk)])
