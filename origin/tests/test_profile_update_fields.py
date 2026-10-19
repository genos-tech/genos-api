"""What `PUT /api/v2/user/profile/` may change (MIP-742).

The path used to hand its whole body to `UserSerializer(partial=True)`,
which let a signed-in user set their own email (no proof of the new
address, so a stolen session could be made permanent with a password
reset), `is_staff`, groups, permissions and `is_active`. With the default
parsers it also took a multipart upload into `profile_image_url`, around
the avatar endpoint's size check, and a hand-written
`profile_image_file_name`.

Now it accepts only the fields the profile editor sends, and refuses
anything else by name.
"""

from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from origin.models.common.user_models import CustomUser
from origin.serializers.common.user_serializers import UserSerializer
from origin.views.common.user_views import PROFILE_EDITABLE_FIELDS

URL = "/api/v2/user/profile/"


class ProfileUpdateFieldTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username="owner",
            email="owner@example.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def put(self, body, **kwargs):
        return self.client.put(URL, {"user_id": str(self.user.id), **body}, **kwargs)

    def assert_refused(self, res, field):
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["fields"], [field])
        self.assertIn(field, res.data["error"])

    # ── refused fields ───────────────────────────────────────────────

    def test_email_is_refused(self):
        res = self.put({"email": "attacker@example.com"}, format="json")
        self.assert_refused(res, "email")
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "owner@example.com")

    def test_is_staff_is_refused(self):
        res = self.put({"is_staff": True}, format="json")
        self.assert_refused(res, "is_staff")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)

    def test_is_active_is_refused(self):
        res = self.put({"is_active": False}, format="json")
        self.assert_refused(res, "is_active")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_groups_are_refused(self):
        group = Group.objects.create(name="admins")
        res = self.put({"groups": [group.id]}, format="json")
        self.assert_refused(res, "groups")
        self.assertFalse(self.user.groups.exists())

    def test_user_permissions_are_refused(self):
        perm = Permission.objects.first()
        res = self.put({"user_permissions": [perm.id]}, format="json")
        self.assert_refused(res, "user_permissions")
        self.assertFalse(self.user.user_permissions.exists())

    def test_is_superuser_is_refused(self):
        # Not in the serializer's field list, so it was never writable,
        # but a request naming it is still told no rather than ignored.
        res = self.put({"is_superuser": True}, format="json")
        self.assert_refused(res, "is_superuser")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_superuser)

    def test_avatar_path_is_refused(self):
        res = self.put({"profile_image_file_name": "https://evil.example/x.png"}, format="json")
        self.assert_refused(res, "profile_image_file_name")
        self.user.refresh_from_db()
        self.assertIsNone(self.user.profile_image_file_name)

    def test_avatar_upload_is_refused(self):
        # The avatar endpoint checks the upload size; this path doesn't,
        # so a file here would bypass it.
        upload = SimpleUploadedFile("a.png", b"x" * 64, content_type="image/png")
        res = self.put({"profile_image_url": upload}, format="multipart")
        self.assert_refused(res, "profile_image_url")
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile_image_url)

    def test_a_refused_field_blocks_the_whole_request(self):
        # No half-applied edit: an allowed field alongside a refused one
        # saves nothing.
        res = self.put({"username": "renamed", "email": "attacker@example.com"}, format="json")
        self.assert_refused(res, "email")
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "owner")
        self.assertEqual(self.user.email, "owner@example.com")

    def test_every_refused_field_is_named(self):
        res = self.put({"email": "a@example.com", "is_staff": True}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["fields"], ["email", "is_staff"])

    def test_refused_even_when_null(self):
        # The view strips None values before saving; the refusal comes
        # first, so a null doesn't slip a name past it.
        res = self.put({"email": None}, format="json")
        self.assert_refused(res, "email")

    def test_a_non_object_body_is_a_400(self):
        res = self.client.put(URL, [1, 2], format="json")
        self.assertEqual(res.status_code, 400)

    # ── what the profile editor sends still saves ────────────────────

    def test_every_editor_field_saves(self):
        body = {
            "username": "New Name",
            "custom_status": "In a meeting",
            "custom_status_expiry": "2026-08-11T09:00:00Z",
            "is_offline_forced": True,
            "role": "Engineer",
            "base_country": "Japan",
            "phone_number": "+81-3-0000-0000",
            "current_location": "Asia/Tokyo",
            "location_shared": False,
            "about_me": "Hello",
        }
        # Guards the test itself: a field added to the allow-list must be
        # added here too, or it ships untested.
        self.assertEqual(set(body), PROFILE_EDITABLE_FIELDS)
        res = self.put(body, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "New Name")
        self.assertEqual(self.user.custom_status, "In a meeting")
        self.assertIsNotNone(self.user.custom_status_expiry)
        self.assertTrue(self.user.is_offline_forced)
        self.assertEqual(self.user.role, "Engineer")
        self.assertEqual(self.user.base_country, "Japan")
        self.assertEqual(self.user.phone_number, "+81-3-0000-0000")
        self.assertEqual(self.user.current_location, "Asia/Tokyo")
        self.assertFalse(self.user.location_shared)
        self.assertEqual(self.user.about_me, "Hello")

    def test_the_response_still_shows_the_locked_fields(self):
        # Read-only, not hidden: the profile and sign-in payloads still
        # carry them.
        res = self.put({"role": "Engineer"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["email"], "owner@example.com")
        self.assertIn("is_active", res.data)
        self.assertIn("profile_image_file_name", res.data)


class UserSerializerLockTests(TestCase):
    """The second lock: `UserSerializer` itself won't write these, for any
    other path that saves through it."""

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username="owner", email="owner@example.com", password="testpass123"
        )

    def test_locked_fields_are_dropped(self):
        group = Group.objects.create(name="admins")
        s = UserSerializer(
            self.user,
            data={
                "email": "attacker@example.com",
                "is_active": False,
                "is_staff": True,
                "groups": [group.id],
                "user_permissions": [Permission.objects.first().id],
                "profile_image_file_name": "https://evil.example/x.png",
            },
            partial=True,
        )
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data, {})
        s.save()
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "owner@example.com")
        self.assertTrue(self.user.is_active)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.groups.exists())
        self.assertFalse(self.user.user_permissions.exists())
        self.assertIsNone(self.user.profile_image_file_name)

    def test_the_avatar_upload_still_writes_through_it(self):
        # `UserProfileImageView` saves the file through this serializer,
        # so `profile_image_url` must stay writable.
        upload = SimpleUploadedFile("a.png", b"x" * 64, content_type="image/png")
        s = UserSerializer(self.user, data={"profile_image_url": upload}, partial=True)
        self.assertTrue(s.is_valid(), s.errors)
        saved = s.save()
        self.assertTrue(saved.profile_image_url)
        saved.profile_image_url.delete(save=False)
