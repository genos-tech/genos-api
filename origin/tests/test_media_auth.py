"""Authenticated AND authorized /media/ serving (`common/media_views.py`).

Two layers, tested separately because they fail differently:

  **Authentication** — avatars and custom emoji stay public; every other
  tree needs a valid access token (Bearer) or refresh cookie. Anonymous
  callers get 401 *before* the filesystem is touched, so existence is
  never revealed.

  **Authorization** — a signed-in user now only gets files belonging to
  objects they can already see. This is what `TestMediaObjectACL` covers,
  and it is the layer that did not exist before: any authenticated user
  who knew a path could fetch any team's attachments. Production had a
  `notes/personal/<id>/gcp-service-account-key.json` sitting behind
  exactly that.

`TestMediaTraversal` is the one that would not be caught by testing each
family in isolation: a path can name a family whose ACL admits the
caller and *point* at a different family's file.

Files are written into a throwaway MEDIA_ROOT, per test, because the
paths embed real primary keys.
"""

import shutil
import tempfile
import uuid
from pathlib import Path

from django.test import override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase
from origin.views.common.media_views import _is_public_media

_MEDIA_ROOT = tempfile.mkdtemp(prefix="test-media-auth-")


def _write(relpath: str, content: bytes = b"file-bytes") -> str:
    target = Path(_MEDIA_ROOT) / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return f"/media/{relpath}"


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class MediaTestBase(BaseAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def as_user(self, user=None):
        """Authenticate via the refresh cookie — the path a browser
        `<img>` actually takes."""
        self.client.credentials()
        self.client.cookies["refresh"] = str(RefreshToken.for_user(user or self.user))


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class TestMediaAuth(MediaTestBase):
    """Layer 1: is there a valid session at all?"""

    def setUp(self):
        super().setUp()
        self.avatar = _write("user_profiles/u1/avatar.png")
        self.emoji = _write("team_emoji/global/party.gif")
        self.protected = _write("task_attachments/42/secret-plan.pdf")
        self.unmapped = _write("future_prefix/1/file.txt")

    # ---- public prefixes -------------------------------------------------

    def test_avatar_is_public(self):
        self.assertEqual(self.client.get(self.avatar).status_code, 200)

    def test_custom_emoji_is_public(self):
        """Emoji render dozens of times per viewport; they are
        deliberately outside both layers."""
        self.assertEqual(self.client.get(self.emoji).status_code, 200)

    def test_public_prefixes_need_no_credentials_at_all(self):
        """Regression guard for web-push icons: the browser fetches those
        from a context that does not reliably attach our cookies, so any
        change that makes avatars require a session breaks notifications
        rather than any visible page."""
        self.client.credentials()
        self.assertNotIn("refresh", self.client.cookies)
        for path in (self.avatar, self.emoji):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    # ---- protected prefixes ----------------------------------------------

    def test_protected_requires_auth(self):
        for path in (self.protected, self.unmapped):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_garbage_credentials_rejected(self):
        self.client.cookies["refresh"] = "not-a-jwt"
        resp = self.client.get(self.protected, HTTP_AUTHORIZATION="Bearer also-not-a-jwt")
        self.assertEqual(resp.status_code, 401)

    def test_missing_protected_file_is_401_when_anonymous(self):
        self.assertEqual(self.client.get("/media/task_attachments/42/nope.pdf").status_code, 401)

    def test_a_deactivated_user_is_refused(self):
        """`is_active` is enforced inside simplejwt's `get_user`, not by
        DRF — reading the `user_id` claim directly would have skipped it,
        which is the bug this repo already shipped once on OAuth."""
        self.as_user()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(self.protected).status_code, 401)

    def test_a_password_change_invalidates_media_access(self):
        """`CHECK_REVOKE_TOKEN` binds a token to the password it was
        minted under. It is what makes a password reset evict a stolen
        session — and it, too, lives only in `get_user`."""
        self.as_user()
        self.user.set_password("a-brand-new-password")
        self.user.save(update_fields=["password"])
        self.assertEqual(self.client.get(self.protected).status_code, 401)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class TestMediaObjectACL(MediaTestBase):
    """Layer 2: does the file's owning object admit this user?

    `self.user` is in `mine`; `self.user2` is in `theirs`. Both are in the
    team, which is the point — a teammate is not automatically an
    audience.
    """

    def setUp(self):
        super().setUp()
        self.mine = ProjectMaster.objects.create(
            team=self.team, project_name="Mine", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.mine, attendee=self.user)
        self.theirs = ProjectMaster.objects.create(
            team=self.team, project_name="Theirs", owner=self.user2
        )
        ProjectMembers.objects.create(team=self.team, project=self.theirs, attendee=self.user2)

        self.my_task = TaskMaster.objects.create(
            team=self.team, project=self.mine, title="Mine", status="Open", reporter=self.user
        )
        self.their_task = TaskMaster.objects.create(
            team=self.team, project=self.theirs, title="Theirs", status="Open", reporter=self.user2
        )

        self.my_attachment = _write(f"task_attachments/{self.my_task.task_id}/plan.pdf")
        self.their_attachment = _write(f"task_attachments/{self.their_task.task_id}/secret.pdf")
        self.my_body = _write(f"tasks/{self.my_task.task_id}/inline.png")
        self.their_body = _write(f"tasks/{self.their_task.task_id}/secret.png")

    # ---- tasks -----------------------------------------------------------

    def test_i_can_read_my_own_task_attachment(self):
        self.as_user()
        self.assertEqual(self.client.get(self.my_attachment).status_code, 200)

    def test_i_cannot_read_a_task_attachment_from_a_project_i_am_not_in(self):
        self.as_user()
        self.assertEqual(self.client.get(self.their_attachment).status_code, 404)

    def test_task_body_attachments_follow_the_same_rule(self):
        self.as_user()
        self.assertEqual(self.client.get(self.my_body).status_code, 200)
        self.assertEqual(self.client.get(self.their_body).status_code, 404)

    def test_bearer_tokens_are_authorized_too(self):
        """The cookie is the browser path; API consumers use a header,
        and must not take a different route through the ACL."""
        self.client.cookies.clear()
        self.authenticate(self.user)
        self.assertEqual(self.client.get(self.my_attachment).status_code, 200)
        self.assertEqual(self.client.get(self.their_attachment).status_code, 404)

    def test_an_assignee_without_a_project_row_can_read(self):
        """`can_access_task` mirrors the search ACL deliberately: a task
        you can *find* must be one whose attachments you can open."""
        self.their_task.assignee = self.user
        self.their_task.save(update_fields=["assignee"])
        self.as_user()
        self.assertEqual(self.client.get(self.their_attachment).status_code, 200)

    def test_denied_and_nonexistent_are_indistinguishable(self):
        """404 both ways — a 403 would confirm the file is real."""
        self.as_user()
        denied = self.client.get(self.their_attachment)
        absent = self.client.get(f"/media/task_attachments/{self.their_task.task_id}/nope.pdf")
        self.assertEqual(denied.status_code, absent.status_code, 404)

    # ---- chats -----------------------------------------------------------

    def test_channel_attachments_require_membership(self):
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Private")
        ChannelMember.objects.create(channel=channel, user=self.user2, role="owner")
        inline = _write(f"chats/{channel.id}/inline/{uuid.uuid4()}-leak.png")
        message = _write(f"chats/{channel.id}/messages/9/leak.bin")

        self.as_user()
        self.assertEqual(self.client.get(inline).status_code, 404)
        self.assertEqual(self.client.get(message).status_code, 404)

        ChannelMember.objects.create(channel=channel, user=self.user, role="member")
        self.assertEqual(self.client.get(inline).status_code, 200)
        self.assertEqual(self.client.get(message).status_code, 200)

    def test_a_removed_member_loses_access(self):
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Was in")
        membership = ChannelMember.objects.create(channel=channel, user=self.user, role="member")
        path = _write(f"chats/{channel.id}/inline/after.png")
        self.as_user()
        self.assertEqual(self.client.get(path).status_code, 200)

        membership.is_deleted = True
        membership.save(update_fields=["is_deleted"])
        self.assertEqual(self.client.get(path).status_code, 404)

    # ---- notes -----------------------------------------------------------

    def test_a_personal_note_attachment_is_private(self):
        note = PersonalNoteMaster.objects.create(team=self.team, owner=self.user2, title="Diary")
        path = _write(f"notes/personal/{note.note_id}/diary.md")
        self.as_user()
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_my_own_personal_note_attachment_is_readable(self):
        note = PersonalNoteMaster.objects.create(team=self.team, owner=self.user, title="Mine")
        # The create flow (`personal_note_views.py:218`) writes this row;
        # `owner` alone is NOT what grants access, so a fixture without
        # it would be testing a state the product never produces.
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=note.note_id, note_type=1, role_id=1
        )
        path = _write(f"notes/personal/{note.note_id}/mine.md")
        self.as_user()
        self.assertEqual(self.client.get(path).status_code, 200)

    def test_media_agrees_with_the_note_endpoint_even_when_that_denies_the_owner(self):
        """The ACL is borrowed, not re-derived — including its edge.

        A personal note whose owner has no `NotePermissionMaster` row is
        unreadable *to its owner* through `/api/v2/note/personal/`, which
        gates on the same `require_read_role`. Production has exactly one
        such row (of 24). Media therefore refuses it too. Granting on the
        `owner` FK here instead would make `/media/` strictly more
        permissive than the endpoint that hands out its URL, which is the
        divergence this whole module exists to remove.
        """
        note = PersonalNoteMaster.objects.create(team=self.team, owner=self.user, title="Orphan")
        path = _write(f"notes/personal/{note.note_id}/orphan.md")
        self.as_user()
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_task_note_attachments_follow_the_note_acl(self):
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.theirs, task=self.their_task, title="N", owner=self.user2
        )
        path = _write(f"notes/task/{note.note_id}/spec.md")
        self.as_user()
        self.assertEqual(self.client.get(path).status_code, 404)

        # Joining the project grants implicit Editor on its task notes —
        # borrowed wholesale rather than re-derived here.
        ProjectMembers.objects.create(team=self.team, project=self.theirs, attendee=self.user)
        self.assertEqual(self.client.get(path).status_code, 200)

    # ---- fail-closed defaults --------------------------------------------

    def test_an_unmapped_prefix_is_denied_for_authenticated_users_too(self):
        """Legacy `chats/<chat_type>/…` from the dropped pre-v3 tables,
        and any future `upload_to` nobody wired up."""
        legacy = _write("chats/1/10/44/2/old-attachment.png")
        future = _write("future_prefix/1/file.txt")
        self.as_user()
        for path in (legacy, future):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_unparseable_ids_are_refused_not_500(self):
        """Path segments are attacker-supplied; `int("abc")` or a bad
        UUID reaching a PK filter is a 500, which is both a scanner
        signal and an error-budget leak."""
        self.as_user()
        for path in (
            "/media/task_attachments/not-an-int/x.pdf",
            "/media/tasks/1e9999/x.pdf",
            "/media/chats/not-a-uuid/inline/x.png",
            "/media/notes/personal/abc/x.md",
            "/media/notes/nonsense/1/x.md",
        ):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_a_directory_path_is_refused(self):
        self.as_user()
        self.assertEqual(self.client.get("/media/notes/personal/").status_code, 404)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class TestMediaTraversal(MediaTestBase):
    """Classify and serve must agree, because they are the same string.

    Django's `safe_join` only stops escapes *out of* MEDIA_ROOT. Moving
    between families *inside* it is invisible to that guard, so a second,
    independent classification of the same path is a bypass.
    """

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Mine", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="Mine", status="Open", reporter=self.user
        )
        self.my_note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=self.task, title="N", owner=self.user
        )
        self.their_note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Diary"
        )
        _write(f"notes/personal/{self.their_note.note_id}/secret.md")
        _write(f"task_attachments/{self.task.task_id}/plan.pdf")

    def test_cannot_hop_families_via_dot_dot(self):
        """Reads as a task note I own; points at a personal note I don't.

        Authenticated on purpose — anonymous would 401 at the first gate
        and prove nothing about the dispatch.
        """
        self.as_user()
        resp = self.client.get(
            f"/media/notes/task/{self.my_note.note_id}/../../personal/"
            f"{self.their_note.note_id}/secret.md"
        )
        self.assertEqual(resp.status_code, 404)

    def test_the_served_path_is_the_authorized_path(self):
        """Called directly, bypassing any URL normalization a client or
        proxy might do on the way in — the guarantee has to hold at the
        function boundary, which is the only place it is enforced."""
        from django.test import RequestFactory  # noqa: PLC0415

        from origin.views.common.media_views import serve_media  # noqa: PLC0415

        request = RequestFactory().get("/media/x")
        request.COOKIES["refresh"] = str(RefreshToken.for_user(self.user))
        resp = serve_media(
            request,
            f"notes/task/{self.my_note.note_id}/../../personal/{self.their_note.note_id}/secret.md",
        )
        self.assertEqual(resp.status_code, 404)

    def test_cannot_hop_from_a_readable_task_into_a_private_note(self):
        self.as_user()
        resp = self.client.get(
            f"/media/task_attachments/{self.task.task_id}/../../notes/personal/"
            f"{self.their_note.note_id}/secret.md"
        )
        self.assertEqual(resp.status_code, 404)

    def test_cannot_launder_a_protected_path_through_a_public_prefix(self):
        resp = self.client.get(
            f"/media/user_profiles/../notes/personal/{self.their_note.note_id}/secret.md"
        )
        self.assertEqual(resp.status_code, 401)

    # ---- classification unit checks --------------------------------------

    def test_traversal_cannot_reclassify_as_public(self):
        self.assertFalse(_is_public_media("user_profiles/../notes/personal/7/diary.md"))
        self.assertTrue(_is_public_media("team_profiles/t1/logo.png"))
        self.assertFalse(_is_public_media("task_attachments/42/f.pdf"))
        # A path that *mentions* a public dir mid-string is not public.
        self.assertFalse(_is_public_media("notes/user_profiles/x.png"))
