"""Two-team fixture shared by every cross-team sharing test.

Deliberately three teams, not two. Most of the interesting failures in
this feature are about the team that was NOT invited — an unconnected
team, or a person who belongs to neither side — and a two-team fixture
quietly makes those cases unwritable.

Named for the roles they play rather than for their ids, because every
assertion in these suites reads as a sentence about who may do what:

    team_a   the HOST. Owns the objects being shared.
    team_b   the GUEST. Its managers run their own participant roster.
    team_c   CONNECTED TO NOBODY. The control group.

This module is not named `test_*` on purpose: it holds no tests, and
letting the runner collect it would report an empty case.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.models.project.prj_models import ProjectMaster
from origin.services.member_roles import EDITOR, VIEWER
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class CrossTeamTestCase(BaseAPITestCase):
    """`BaseAPITestCase` plus a guest team, a stranger team, and objects."""

    def setUp(self):
        super().setUp()

        # Team A — the host. `self.team` and `self.user` come from the
        # base fixture; aliased so host/guest reads unambiguously below.
        self.team_a = self.team
        self.a_owner = self.user
        self.a_editor = self._member(self.team_a, "a_editor", EDITOR)
        # `self.user2` is already a plain member of team A.
        self.a_viewer = self.user2

        # Team B — the guest team whose managers administer the roster.
        self.b_owner = self._user("b_owner")
        self.team_b = TeamMaster.objects.create(
            team_name="Guest Team",
            team_email="guest@example.com",
            owner=self.b_owner,
        )
        TeamMembers.objects.create(team=self.team_b, attendee=self.b_owner)
        self.b_editor = self._member(self.team_b, "b_editor", EDITOR)
        self.b_viewer = self._member(self.team_b, "b_viewer", VIEWER)

        # Team C — connected to nobody, and the source of "a stranger".
        self.c_owner = self._user("c_owner")
        self.team_c = TeamMaster.objects.create(
            team_name="Stranger Team",
            team_email="stranger@example.com",
            owner=self.c_owner,
        )
        TeamMembers.objects.create(team=self.team_c, attendee=self.c_owner)

        # Objects to share. One per surface, so a writer regression shows
        # up in the phase that owns it rather than three phases later.
        # `project_system_user` is normally stamped by the create endpoint
        # and is non-null in every real row; the project list serializes it
        # unconditionally, so a fixture row without one 500s that endpoint.
        self.project = ProjectMaster.objects.create(
            team=self.team_a,
            project_name="Host Project",
            owner=self.a_owner,
            project_system_user=self.a_owner,
        )
        self.foreign_project = ProjectMaster.objects.create(
            team=self.team_c,
            project_name="Stranger Project",
            owner=self.c_owner,
            project_system_user=self.c_owner,
        )
        self.folder = PersonalNoteFolder.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            name="Shared Folder",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE,
        )

    # ------------------------------------------------------------------

    def _user(self, name):
        return User.objects.create_user(
            username=name,
            email=f"{name}@example.com",
            password="pass12345",
        )

    def _member(self, team, name, role):
        user = self._user(name)
        TeamMembers.objects.create(team=team, attendee=user, member_role=role)
        return user

    def _in_project(self, user, project=None) -> bool:
        """Is this person on the host project's roster?

        Asked about one person rather than by counting rows, because
        accepting a share admits the approver: a suite that asserted
        `ProjectMembers.objects.count() == 0` to mean "the person we just
        refused did not get in" now counts the approver as well and says
        nothing about the person it is testing.
        """
        from origin.models.project.prj_models import ProjectMembers

        return ProjectMembers.objects.filter(
            project=project or self.project, attendee=user
        ).exists()

    # ------------------------------------------------------------------

    def connect_a_and_b(self):
        """An ACTIVE connection between the host and the guest team.

        Idempotent, because a test that shares two objects connects the
        teams twice and the connection is deliberately once-per-pair —
        re-requesting a live one is an error in production and noise here.
        """
        from origin.models.common.team_models import ShareStatus
        from origin.services.team_connection import (
            get_connection,
            request_connection,
            respond_to_connection,
        )

        existing = get_connection(self.team_a.team_id, self.team_b.team_id)
        if existing is not None and existing.status == ShareStatus.ACTIVE:
            return existing
        conn = request_connection(self.team_a.team_id, self.team_b.team_id, self.a_owner)
        return respond_to_connection(conn, self.b_owner, accept=True)

    def active_project_grant(self, role_ceiling=EDITOR):
        """A connection, a project grant, and B's acceptance of it."""
        from origin.models.common.team_models import ExternalGrant

        return self._active_grant(
            ExternalGrant.ObjectType.PROJECT, self.project.project_id, role_ceiling
        )

    def active_folder_grant(self, role_ceiling=EDITOR, folder=None):
        """A connection, a note-folder grant, and B's acceptance of it."""
        from origin.models.common.team_models import ExternalGrant

        return self._active_grant(
            ExternalGrant.ObjectType.NOTE_FOLDER,
            (folder or self.folder).folder_id,
            role_ceiling,
        )

    def create_external_chat(self, guest_team_ids=None, **extra):
        """Create an external GM through the API, as team A's owner.

        Through the API rather than the ORM because the create path is what
        stamps `is_external`, forces privacy and writes the offers — a
        hand-built `Channel` row is a chat no product code would produce.
        """
        from origin.models.chat.unified_models import ChannelKind

        self.authenticate(self.a_owner)
        body = {
            "kind": ChannelKind.GM,
            "team_id": str(self.team_a.team_id),
            "title": "Cross-team room",
            "is_external": True,
        }
        if guest_team_ids is not None:
            body["guest_team_ids"] = [str(t) for t in guest_team_ids]
        body.update(extra)
        return self.client.post("/api/v3/channels/", body, format="json")

    def shared_chat(self, role_ceiling=EDITOR):
        """A connected, granted, accepted external chat: (channel, grant)."""
        from origin.models.chat.unified_models import Channel
        from origin.models.common.team_models import ExternalGrant
        from origin.services.external_grants import respond_to_grant

        self.connect_a_and_b()
        res = self.create_external_chat([self.team_b.team_id], role_ceiling=role_ceiling)
        self.assertEqual(res.status_code, 201, res.data)
        channel = Channel.objects.get(id=res.data["channel"]["id"])
        grant = ExternalGrant.objects.get(
            object_type=ExternalGrant.ObjectType.CHANNEL,
            object_id=str(channel.id),
            guest_team_id=self.team_b.team_id,
        )
        return channel, respond_to_grant(grant, self.b_owner, accept=True)

    def _active_grant(self, object_type, object_id, role_ceiling):
        from origin.services.external_grants import offer_grant, respond_to_grant

        self.connect_a_and_b()
        grant = offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=object_type,
            object_id=object_id,
            role_ceiling=role_ceiling,
            actor=self.a_owner,
        )
        return respond_to_grant(grant, self.b_owner, accept=True)
