"""Shared projects across teams.

Phase 3 adds almost no code, and that is the claim under test. An external
project participant is written as an ordinary guest-role `ProjectMembers`
row, so `can_access_task`, `is_project_member`, `serve_media` and the
search ACL should already treat them correctly without knowing cross-team
sharing exists. These tests exist to prove that rather than assume it — if
one of them needs new production code to pass, the design has drifted.

The one real gap Phase 3 closes is discovery: the project LIST gated on
team membership, so someone admitted to a host team's project could open it
by id but never see it. It is now narrowed rather than refused, and the
narrowing is the part that matters — the unnarrowed list is the "find a
project to ask to join" surface and must stay members-only.
"""

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import ShareStatus
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.external_grants import (
    add_external_participants,
    revoke_grant,
)
from origin.services.member_roles import GUEST
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.views.utils.scope_guards import (
    can_access_task,
    is_guest,
    is_project_member,
    is_team_member,
)

PROJECTS = "/api/v2/project/projects/"
SHARE_OBJECT = "/api/v2/team/share/object/"


class ExternalProjectMembershipTests(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()

    def test_an_admitted_participant_becomes_a_guest_of_the_host_team(self):
        """The whole design in one assertion.

        Access is a `ProjectMembers` row with no `TeamMembers` row — which
        is precisely what `is_guest` describes — so every team-wide gate
        keeps denying them and every per-object check keeps working, with
        no new ACL semantics anywhere.
        """
        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)
        self.assertTrue(is_project_member(self.project.project_id, self.b_viewer.id))
        self.assertTrue(is_guest(self.team_a.team_id, self.b_viewer.id))
        self.assertFalse(is_team_member(self.team_a.team_id, self.b_viewer.id))

    def test_the_row_is_the_guest_role_whatever_the_ceiling_says(self):
        """`editor` on a project confers member management.

        An outsider who could add people to the host's project would
        defeat the grant, so the project role is pinned to guest. Read and
        write on the tasks themselves come from membership
        (`can_access_task`), not from this column.
        """
        add_external_participants(self.grant, [self.b_owner.id], self.b_owner)
        row = ProjectMembers.objects.get(project=self.project, attendee=self.b_owner)
        self.assertEqual(row.member_role, GUEST)

    def test_they_can_reach_the_shared_project_tasks(self):
        from origin.models.task.task_models import TaskMaster

        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)
        task = TaskMaster.objects.create(
            team=self.team_a,
            project=self.project,
            title="Shared work",
            reporter=self.a_owner,
        )
        self.assertTrue(can_access_task(task.task_id, self.b_viewer.id))

    def test_they_cannot_reach_the_hosts_other_projects(self):
        """A share is one object, never the team."""
        from origin.models.task.task_models import TaskMaster

        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)
        other = ProjectMaster.objects.create(
            team=self.team_a,
            project_name="Internal",
            owner=self.a_owner,
            project_system_user=self.a_owner,
        )
        task = TaskMaster.objects.create(
            team=self.team_a,
            project=other,
            title="Not theirs",
            reporter=self.a_owner,
        )
        self.assertFalse(is_project_member(other.project_id, self.b_viewer.id))
        self.assertFalse(can_access_task(task.task_id, self.b_viewer.id))

    def test_the_projects_pm_channel_admits_them(self):
        """Project chat comes along, via the existing membership signal.

        Worth pinning: it is a side effect of writing `ProjectMembers`, so
        a future refactor that bypassed that row would silently take the
        project's conversation away from the people sharing the work.
        """
        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)
        pm = Channel.objects.filter(kind=ChannelKind.PM, project_id=self.project.project_id).first()
        if pm is None:
            self.skipTest("This project has no PM channel mirror in the test fixture.")
        self.assertTrue(
            ChannelMember.objects.filter(channel=pm, user=self.b_viewer, is_deleted=False).exists()
        )

    def test_revoking_the_share_removes_the_project_row(self):
        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)
        revoke_grant(self.grant, self.a_owner)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.b_viewer).exists()
        )
        self.assertFalse(is_guest(self.team_a.team_id, self.b_viewer.id))

    def test_joining_by_id_is_still_refused_to_an_outsider(self):
        """`JoinProjectView` stays closed; grants are the only way in.

        The approver holds a project row now (accepting admits them), so
        they are a project member — and the project's own policy lets any
        member add people. What stops this is the separate check that the
        actor belongs to the team owning the project: an external
        participant may not staff the host's project, in either direction.
        """
        self.authenticate(self.b_owner)
        res = self.client.post(
            "/api/v2/project/join/",
            {
                "team_id": str(self.team_a.team_id),
                "project_id": self.project.project_id,
                "attendee_id": str(self.b_editor.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.b_editor).exists()
        )

    def test_an_external_participant_cannot_add_a_host_employee_either(self):
        """The same refusal in the direction that actually tempts abuse.

        Adding a HOST-team member passes every other check in the view —
        they are in the team, the actor is in the project — so before the
        owning-team check an outsider could quietly staff the host's
        project with the host's own people.
        """
        self.authenticate(self.b_owner)
        res = self.client.post(
            "/api/v2/project/join/",
            {
                "team_id": str(self.team_a.team_id),
                "project_id": self.project.project_id,
                "attendee_id": str(self.a_editor.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.a_editor).exists()
        )


class ExternalProjectDiscoveryTests(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()
        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)
        self.internal = ProjectMaster.objects.create(
            team=self.team_a,
            project_name="Internal",
            owner=self.a_owner,
            project_system_user=self.a_owner,
        )

    def test_a_participant_sees_only_the_shared_project(self):
        self.authenticate(self.b_viewer)
        res = self.client.get(
            PROJECTS,
            {"team_id": str(self.team_a.team_id), "attendee_id": str(self.b_viewer.id)},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual([p["projectId"] for p in res.data], [self.project.project_id])

    def test_a_host_member_still_sees_every_project(self):
        """The narrowing must not leak into the members-only list."""
        self.authenticate(self.a_owner)
        res = self.client.get(
            PROJECTS,
            {"team_id": str(self.team_a.team_id), "attendee_id": str(self.a_owner.id)},
        )
        self.assertEqual(res.status_code, 200)
        ids = {p["projectId"] for p in res.data}
        self.assertIn(self.internal.project_id, ids)
        self.assertIn(self.project.project_id, ids)

    def test_a_colleague_with_no_participation_is_refused(self):
        """Their team holds the grant; they were never admitted."""
        self.authenticate(self.b_editor)
        res = self.client.get(
            PROJECTS,
            {"team_id": str(self.team_a.team_id), "attendee_id": str(self.b_editor.id)},
        )
        self.assertEqual(res.status_code, 404)

    def test_a_stranger_is_refused(self):
        self.authenticate(self.c_owner)
        res = self.client.get(
            PROJECTS,
            {"team_id": str(self.team_a.team_id), "attendee_id": str(self.c_owner.id)},
        )
        self.assertEqual(res.status_code, 404)


class ExternalProjectSharePanelTests(CrossTeamTestCase):
    """`GET /team/share/object/` — the object-keyed share lookup.

    Exists for exactly the case a team-keyed lookup cannot serve: the guest
    side, who read the project from the host team's shell and belong to
    neither team.
    """

    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()
        self.params = {
            "object_type": "project",
            "object_id": str(self.project.project_id),
        }

    def test_the_host_sees_the_share_and_cannot_admit(self):
        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)
        self.authenticate(self.a_owner)
        res = self.client.get(SHARE_OBJECT, self.params)
        self.assertEqual(res.status_code, 200)
        share = res.data["shares"][0]
        self.assertEqual(share["side"], "given")
        self.assertFalse(share["canAdmit"])
        # The approver is in the project too — accepting admits them, so
        # that Approve grants access rather than merely permitting it.
        self.assertEqual(
            {p["userId"] for p in share["participants"]},
            {str(self.b_viewer.id), str(self.b_owner.id)},
        )

    def test_a_guest_manager_can_read_and_admit(self):
        self.authenticate(self.b_owner)
        res = self.client.get(SHARE_OBJECT, self.params)
        self.assertEqual(res.status_code, 200)
        share = res.data["shares"][0]
        self.assertEqual(share["side"], "received")
        self.assertTrue(share["canAdmit"])

    def test_a_stranger_gets_a_404_not_an_empty_list(self):
        self.authenticate(self.c_owner)
        res = self.client.get(SHARE_OBJECT, self.params)
        self.assertEqual(res.status_code, 404)

    def test_a_revoked_share_reports_no_participants(self):
        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)
        revoke_grant(self.grant, self.a_owner)
        self.authenticate(self.a_owner)
        res = self.client.get(SHARE_OBJECT, self.params)
        share = res.data["shares"][0]
        self.assertEqual(share["status"], ShareStatus.REVOKED)
        self.assertEqual(share["participants"], [])

    def test_a_malformed_object_type_is_a_400(self):
        self.authenticate(self.a_owner)
        res = self.client.get(SHARE_OBJECT, {**self.params, "object_type": "nonsense"})
        self.assertEqual(res.status_code, 400)
