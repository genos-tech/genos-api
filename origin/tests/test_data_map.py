"""Every model column is classified as pii, content or none (MIP-2256).

A new field with no entry in origin/data_map/model_field_classes.yaml fails this test, so nobody can
add a column that holds personal data without the data map hearing about it. When a field is pii or
content, also add its row (copies, retention, readers, purpose) to genos-docs operations/DATA_MAP.md.
"""

from pathlib import Path

import yaml
from django.apps import apps
from django.test import SimpleTestCase

CLASSES = {"pii", "content", "none"}
APP_LABELS = {"origin", "search_engine", "oauth2_provider", "token_blacklist"}
YAML_PATH = Path(__file__).resolve().parent.parent / "data_map" / "model_field_classes.yaml"


def _live_fields():
    live = {}
    for model in apps.get_models():
        meta = model._meta
        if meta.app_label not in APP_LABELS or meta.proxy or meta.auto_created:
            continue
        live[f"{meta.app_label}.{model.__name__}"] = {
            f.name for f in meta.get_fields() if getattr(f, "concrete", False)
        }
    return live


class DataMapClassificationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.declared = yaml.safe_load(YAML_PATH.read_text()) or {}
        cls.live = _live_fields()

    def test_every_field_is_classified(self):
        missing = sorted(
            f"{model}.{field}"
            for model, fields in self.live.items()
            for field in fields
            if field not in (self.declared.get(model) or {})
        )
        self.assertEqual(
            missing,
            [],
            "Unclassified model fields. Add each to origin/data_map/model_field_classes.yaml as "
            "pii (about a person), content (what users write or upload) or none, and add pii/content "
            "rows to genos-docs operations/DATA_MAP.md.",
        )

    def test_no_entry_for_a_field_that_is_gone(self):
        stale = sorted(
            f"{model}.{field}"
            for model, fields in self.declared.items()
            for field in (fields or {})
            if field not in self.live.get(model, set())
        )
        self.assertEqual(stale, [], "Remove these from origin/data_map/model_field_classes.yaml.")

    def test_every_value_is_a_known_class(self):
        bad = sorted(
            f"{model}.{field}={value}"
            for model, fields in self.declared.items()
            for field, value in (fields or {}).items()
            if value not in CLASSES
        )
        self.assertEqual(bad, [])

    def test_no_app_with_models_is_left_out(self):
        # Django's own apps are out of scope (sessions hold signed server state, not our data);
        # any other app that has models must be in APP_LABELS so its fields are classified.
        django_own = {"auth", "contenttypes", "sessions"}
        left_out = {
            c.label
            for c in apps.get_app_configs()
            if c.label not in APP_LABELS and c.label not in django_own and any(c.get_models())
        }
        self.assertEqual(left_out, set())

    def test_removed_unused_profile_column_stays_removed(self):
        self.assertNotIn("base_country", self.live["origin.CustomUser"])
        # phone_number is edited in the profile modal, so it stays and is pii.
        self.assertEqual(self.declared["origin.CustomUser"]["phone_number"], "pii")
