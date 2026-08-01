"""The deploy-time guard for `API_PUBLIC_BASE_URL`.

Written after the real incident it prevents: the value was set to the
FRONTEND origin, so every notification email shipped an unsubscribe link
that returned 200 + the SPA shell instead of reaching Django. Nothing
errored — the channel ran green while one-click unsubscribe silently
failed.
"""

from django.test import SimpleTestCase, override_settings

from origin.checks import email_public_url_check

API = "https://api.genos.test"
FRONTEND = "https://genos.test"


def _ids():
    return [w.id for w in email_public_url_check(None)]


class EmailPublicUrlCheckTests(SimpleTestCase):
    @override_settings(
        EMAIL_NOTIFICATIONS_ENABLED=True,
        API_PUBLIC_BASE_URL=API,
        FRONTEND_BASE_URL=FRONTEND,
    )
    def test_distinct_hosts_pass(self):
        self.assertEqual(_ids(), [])

    @override_settings(
        EMAIL_NOTIFICATIONS_ENABLED=True,
        API_PUBLIC_BASE_URL=FRONTEND,
        FRONTEND_BASE_URL=FRONTEND,
    )
    def test_frontend_host_is_flagged(self):
        self.assertEqual(_ids(), ["origin.W002"])

    @override_settings(
        EMAIL_NOTIFICATIONS_ENABLED=True,
        API_PUBLIC_BASE_URL=FRONTEND + "/",
        FRONTEND_BASE_URL=FRONTEND,
    )
    def test_trailing_slash_does_not_hide_the_collision(self):
        self.assertEqual(_ids(), ["origin.W002"])

    @override_settings(
        EMAIL_NOTIFICATIONS_ENABLED=True,
        API_PUBLIC_BASE_URL="",
        FRONTEND_BASE_URL=FRONTEND,
    )
    def test_unset_is_flagged(self):
        self.assertEqual(_ids(), ["origin.W001"])

    @override_settings(
        EMAIL_NOTIFICATIONS_ENABLED=False,
        API_PUBLIC_BASE_URL="",
        FRONTEND_BASE_URL=FRONTEND,
    )
    def test_silent_while_the_channel_is_off(self):
        # Dark-shipped environments must not nag about a value the
        # feature they've disabled would need.
        self.assertEqual(_ids(), [])

    @override_settings(
        EMAIL_NOTIFICATIONS_ENABLED=True,
        API_PUBLIC_BASE_URL=API,
        FRONTEND_BASE_URL=FRONTEND,
    )
    def test_warnings_never_block(self):
        # Warning, not Error: a bad unsubscribe host must never stop
        # notification email from being sent.
        from django.core.checks import Warning as CheckWarning

        with override_settings(API_PUBLIC_BASE_URL=FRONTEND):
            issues = email_public_url_check(None)
        self.assertTrue(all(isinstance(i, CheckWarning) for i in issues))
