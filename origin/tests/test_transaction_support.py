"""MIP-1643: the cheap flush leaves the database exactly as Django's own flush would."""

import warnings
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.tests import transaction_support
from origin.tests.transaction_support import CheapFlushTransactionTestCase


def _rows_by_table():
    tables = connection.introspection.django_table_names(only_existing=True, include_views=False)
    with connection.cursor() as cursor:
        out = {}
        for table in tables:
            cursor.execute(f"SELECT count(*) FROM {connection.ops.quote_name(table)}")
            out[table] = cursor.fetchone()[0]
    return out


class CheapFlushTests(CheapFlushTransactionTestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="flush_u", email="flush_u@example.com", password="x"
        )
        team = TeamMaster.objects.create(team_name="T", team_email="t@example.com", owner=user)
        TeamMembers.objects.create(team=team, attendee=user)

    def _only_post_migrate_rows_left(self):
        left = {t: n for t, n in _rows_by_table().items() if n}
        self.assertEqual(set(left), {"django_content_type", "auth_permission"}, left)

    def test_every_table_with_rows_is_emptied_and_the_reference_rows_stay(self):
        types, perms = ContentType.objects.count(), Permission.objects.count()
        self.assertGreater(types, 0)

        self._fixture_teardown()

        self._only_post_migrate_rows_left()
        self.assertEqual((ContentType.objects.count(), Permission.objects.count()), (types, perms))

    def test_a_test_that_changed_the_content_types_gets_django_full_flush(self):
        ContentType.objects.create(app_label="made_by_a_test", model="thing")

        self._fixture_teardown()

        self._only_post_migrate_rows_left()
        self.assertFalse(ContentType.objects.filter(app_label="made_by_a_test").exists())
        # post_migrate ran: the real ones are back.
        self.assertTrue(ContentType.objects.filter(app_label="origin").exists())
        self.assertTrue(Permission.objects.filter(content_type__app_label="origin").exists())

    def test_an_edited_permission_gets_django_full_flush_too(self):
        # Same count and ids, one column changed: the fingerprint covers whole rows.
        perm = Permission.objects.order_by("id").first()
        original = perm.name
        Permission.objects.filter(pk=perm.pk).update(name="edited by a test")

        self._fixture_teardown()

        self._only_post_migrate_rows_left()
        self.assertEqual(Permission.objects.order_by("id").first().name, original)

    def test_a_failing_cheap_path_falls_back_to_the_full_flush_and_says_so(self):
        # `_full_flush` is replaced by a spy (the real one is the test above, and costs ~100 MB of
        # disk writes): the point here is that it is asked for, and that the failure is not silent.
        with (
            mock.patch.object(
                transaction_support,
                "delete_rows_of_nonempty_tables",
                side_effect=RuntimeError("boom"),
            ),
            mock.patch.object(CheapFlushTests, "_full_flush") as full_flush,
        ):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self._fixture_teardown()

        full_flush.assert_called_once_with("default")
        self.assertTrue(any("cheap flush failed" in str(w.message) for w in caught))
