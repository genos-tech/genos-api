"""`missing_fields` and `require_fields`, the one required-field check for a body a view
reads with `body[...]` afterwards (MIP-1020).

Presence, not value: a `null` is a value. `validate_request_data` refuses `None`, which is right
for an id and wrong for a field like a task's `due_date`, which is sent as `null` for "no date".
"""

from django.test import SimpleTestCase

from origin.views.utils.request_validators import missing_fields, require_fields


class MissingFieldsTests(SimpleTestCase):
    def test_names_the_absent_keys_in_the_order_asked(self):
        self.assertEqual(missing_fields({"b": 1}, "a", "b", "c"), ["a", "c"])

    def test_a_null_a_zero_and_an_empty_value_are_present(self):
        body = {"a": None, "b": 0, "c": "", "d": [], "e": {}, "f": False}
        self.assertEqual(missing_fields(body, *body), [])

    def test_nothing_missing_is_an_empty_list(self):
        self.assertEqual(missing_fields({}), [])


class RequireFieldsTests(SimpleTestCase):
    def refused(self, resp):
        self.assertEqual(resp.status_code, 400)
        return resp.data["error"]

    def test_a_body_that_has_everything_is_none(self):
        self.assertIsNone(require_fields({"a": None, "o": {}}, "a", objects=("o",)))

    def test_one_missing_field_is_singular(self):
        self.assertEqual(self.refused(require_fields({}, "a")), "a is required.")

    def test_several_missing_fields_are_named_together(self):
        self.assertEqual(self.refused(require_fields({}, "a", "b")), "a and b are required.")
        self.assertEqual(
            self.refused(require_fields({}, "a", "b", "c")), "a, b and c are required."
        )

    def test_an_absent_object_field_is_required_too(self):
        self.assertEqual(
            self.refused(require_fields({"a": 1}, "a", objects=("o",))), "o is required."
        )

    def test_the_views_own_words_replace_the_default(self):
        self.assertEqual(self.refused(require_fields({}, "a", message="Nope.")), "Nope.")

    def test_an_object_field_that_is_not_an_object_is_refused(self):
        for value in (None, "x", 5, [], [1], True):
            with self.subTest(value=value):
                self.assertEqual(
                    self.refused(require_fields({"o": value}, objects=("o",))),
                    "o must be an object.",
                )

    def test_an_empty_object_is_an_object(self):
        self.assertIsNone(require_fields({"o": {}}, objects=("o",)))

    def test_a_plain_field_is_not_type_checked(self):
        self.assertIsNone(require_fields({"a": [1]}, "a"))
