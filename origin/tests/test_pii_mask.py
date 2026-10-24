"""`mask_email`: the audit log's labels keep the shape of an address, not the address (MIP-765)."""

import importlib

from django.test import SimpleTestCase, TransactionTestCase

from origin.models.common.security_event_models import (
    AppendOnlyError,
    SecurityAction,
    SecurityEvent,
    SecuritySource,
)
from origin.services.pii_mask import has_email, mask_email


class MaskEmailTests(SimpleTestCase):
    def test_keeps_the_first_character_and_the_whole_domain(self):
        self.assertEqual(mask_email("kenji@gmail.com"), "k***@gmail.com")

    def test_the_stars_do_not_reveal_the_length(self):
        self.assertEqual(mask_email("a.very.long.name@x.io"), "a***@x.io")
        self.assertEqual(mask_email("ab@x.io"), "a***@x.io")

    def test_a_one_character_local_part_is_kept(self):
        self.assertEqual(mask_email("k@x.com"), "k***@x.com")

    def test_plus_addressing_and_subdomains(self):
        self.assertEqual(
            mask_email("ken+work@mail.corp.example.co.uk"), "k***@mail.corp.example.co.uk"
        )

    def test_an_address_inside_text_is_masked_in_place(self):
        # What oauth_views writes: "<provider>: <email>".
        self.assertEqual(mask_email("github: ken@example.com"), "github: k***@example.com")
        self.assertEqual(mask_email("a@x.com invited b@y.org"), "a***@x.com invited b***@y.org")

    def test_text_with_no_email_is_returned_untouched(self):
        for label in (
            "",
            "Q3 roadmap",
            "ci-deploy (gnos_ab12cd)",
            "github:12345",
            "https://example.com/hook",
            "uuid 8631d05b-0ad4-4c93",
        ):
            with self.subTest(label=label):
                self.assertEqual(mask_email(label), label)

    def test_a_url_with_credentials_is_not_mistaken_for_an_email_in_place(self):
        # A webhook target label is a URL. `user@host` inside it has a
        # domain, so it is masked like any address; the rest is kept.
        self.assertEqual(
            mask_email("https://bob@hooks.example.com/x"), "https://b***@hooks.example.com/x"
        )

    def test_masking_is_idempotent(self):
        once = mask_email("kenji@gmail.com")
        self.assertEqual(mask_email(once), once)

    def test_an_empty_or_none_label_is_safe(self):
        self.assertEqual(mask_email(""), "")
        self.assertIsNone(mask_email(None))

    def test_nothing_unmasked_survives(self):
        text = "from a@x.com to b.c@y.io via d+e@z.org"
        self.assertTrue(has_email(text))
        self.assertFalse(has_email(mask_email(text)))

    def test_has_email_ignores_a_masked_address(self):
        self.assertFalse(has_email("k***@gmail.com"))
        self.assertFalse(has_email(""))
        self.assertFalse(has_email("no at sign"))
        self.assertTrue(has_email("k@gmail.com"))


class BackfillMigrationTests(TransactionTestCase):
    """0207 masks the rows written before the writer did, and only those.

    Runs the migration's function with the HISTORICAL models, exactly what
    `migrate` hands it, and not the live registry: the live `SecurityEvent`
    is append-only and would raise, which is the whole point of the guard.
    """

    def _historical(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        return executor.loader.project_state(("origin", "0207_mask_security_event_labels")).apps

    def _row(self, **kwargs):
        # Straight SQL: the live manager refuses to write an unmasked
        # label's neighbour only through update(), but create() of a row with
        # a plaintext label goes via the same model; bypass the writer so the
        # row is what an old release left behind.
        return SecurityEvent.objects.create(
            action=SecurityAction.SIGNIN_SUCCEEDED, source=SecuritySource.APP, **kwargs
        )

    def _run(self):
        migration = importlib.import_module("origin.migrations.0207_mask_security_event_labels")
        migration.mask_existing_labels(self._historical(), None)

    def test_old_plaintext_labels_are_masked(self):
        a = self._row(actor_label="kenji@gmail.com", target_label="new@example.com")

        self._run()

        a = SecurityEvent.objects.get(pk=a.pk)
        self.assertEqual(a.actor_label, "k***@gmail.com")
        self.assertEqual(a.target_label, "n***@example.com")

    def test_labels_without_an_email_are_left_alone(self):
        keep = self._row(actor_label="ci-deploy (gnos_ab12)", target_label="Q3 roadmap")

        self._run()

        keep = SecurityEvent.objects.get(pk=keep.pk)
        self.assertEqual(keep.actor_label, "ci-deploy (gnos_ab12)")
        self.assertEqual(keep.target_label, "Q3 roadmap")

    def test_other_columns_are_not_touched(self):
        row = self._row(actor_label="a@x.com", target_id="42", metadata={"k": "v"}, ip="1.2.3.4")
        before = SecurityEvent.objects.filter(pk=row.pk).values().get()

        self._run()

        after = SecurityEvent.objects.filter(pk=row.pk).values().get()
        changed = {k for k in before if before[k] != after[k]}
        self.assertEqual(changed, {"actor_label"})

    def test_running_it_twice_changes_nothing_more(self):
        row = self._row(actor_label="kenji@gmail.com")
        self._run()
        once = SecurityEvent.objects.get(pk=row.pk).actor_label

        self._run()

        self.assertEqual(SecurityEvent.objects.get(pk=row.pk).actor_label, once)

    def test_more_rows_than_one_batch(self):
        migration = importlib.import_module("origin.migrations.0207_mask_security_event_labels")
        rows = [self._row(actor_label=f"user{i}@example.com") for i in range(migration.BATCH + 7)]

        self._run()

        labels = set(
            SecurityEvent.objects.filter(pk__in=[r.pk for r in rows]).values_list(
                "actor_label", flat=True
            )
        )
        self.assertEqual(labels, {"u***@example.com"})

    def test_the_append_only_guard_still_holds_for_application_code(self):
        row = self._row(actor_label="kenji@gmail.com")
        self._run()

        with self.assertRaises(AppendOnlyError):
            SecurityEvent.objects.filter(pk=row.pk).update(actor_label="x")
        with self.assertRaises(AppendOnlyError):
            SecurityEvent.objects.get(pk=row.pk).delete()

    def test_the_guard_is_what_the_historical_model_gets_past(self):
        """Why the migration is written the way it is: with the LIVE models
        the same function is refused. If this ever stops raising, the
        historical-model claim in the migration's header no longer holds."""
        from django.apps import apps as live_apps

        self._row(actor_label="kenji@gmail.com")
        migration = importlib.import_module("origin.migrations.0207_mask_security_event_labels")

        with self.assertRaises(AppendOnlyError):
            migration.mask_existing_labels(live_apps, None)

    def test_rows_with_no_email_cost_no_write(self):
        """The audit log can be large and the migration holds a transaction:
        only a row that still has an address is rewritten."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self._row(actor_label="ci-deploy (gnos_ab12)", target_label="Q3 roadmap")
        self._row(actor_label="", target_label="")

        with CaptureQueriesContext(connection) as queries:
            self._run()

        updates = [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith("UPDATE")]
        self.assertEqual(updates, [])


class AlertCopiesNoAddressTests(TransactionTestCase):
    def test_an_alert_for_a_cause_written_before_masking_holds_no_address(self):
        """`_alert` copies its cause's label into a NEW row. A cause stored
        before MIP-765 (or before 0207 ran) still holds the address, so the
        copy has to be masked itself, whatever the backfill's timing."""
        from origin.services import security_events

        cause = SecurityEvent.objects.create(
            action=SecurityAction.EXPORT,
            source=SecuritySource.APP,
            actor_label="kenji@gmail.com",  # as an old release left it
        )

        security_events._alert(SecurityAction.ALERT_EXPORT, cause, {"summary": "x"})

        alert = SecurityEvent.objects.get(action=SecurityAction.ALERT_EXPORT)
        self.assertEqual(alert.actor_label, "k***@gmail.com")
