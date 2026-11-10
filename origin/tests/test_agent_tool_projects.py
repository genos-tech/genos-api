"""`manage_project` and `manage_project_members` (MIP-882).

Projects could be read by the agent and nothing else. The page creates one in
several browser steps (a system user for the project, the project, the creator's
membership, the system user's team membership, then the other members), so a tool
has to do every step on the server, or the project is half made and breaks the
team's project list. These pin:

* **A project is made whole or not at all.** The same rows the page leaves, in one
  transaction, and nothing left behind when any step fails.
* **The page's rules, from one implementation.** The edit and role views now call
  the service the tools call; tests through REST pin their exact messages and
  status codes, which is what shows the extraction changed nothing for the UI.
* **Access is the page's.** The owner or an editor renames, recodes and sets roles;
  any project member adds a teammate; nobody outside the team gets anywhere; and a
  tool never trusts an owner it is told, because it is always the person asking.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden
from origin.search_engine.agent.tools.manage_project import MANAGE_PROJECT
from origin.search_engine.agent.tools.manage_project_members import MANAGE_PROJECT_MEMBERS

from .test_base import BaseAPITestCase

User = get_user_model()
PROJECT = "/api/v2/project/"
ROLE = "/api/v2/project/member-role/"


class ProjectToolBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Release",
            code="REL",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self.authenticate()

    def create(self, ctx=None, **args):
        return MANAGE_PROJECT.run({"action": "create", **args}, ctx or self.ctx)

    def update(self, project=None, ctx=None, **args):
        return MANAGE_PROJECT.run(
            {"action": "update", "project_id": (project or self.project).project_id, **args},
            ctx or self.ctx,
        )

    def members(self, action, ctx=None, **args):
        return MANAGE_PROJECT_MEMBERS.run(
            {"action": action, "project_id": self.project.project_id, **args}, ctx or self.ctx
        )

    def new_person(self, name, *, in_team=True):
        person = User.objects.create_user(username=name, email=f"{name}@example.com", password="pw")
        if in_team:
            TeamMembers.objects.create(team=self.team, attendee=person)
        return person

    def role_of(self, user, project=None):
        return ProjectMembers.objects.get(
            project=project or self.project, attendee=user
        ).member_role


class TheToolsAreApprovalGatedWrites(ProjectToolBase):
    def test_both_need_approval_and_are_registered(self):
        for tool in (MANAGE_PROJECT, MANAGE_PROJECT_MEMBERS):
            self.assertTrue(tool.requires_approval, tool.name)
            self.assertIs(REGISTRY[tool.name], tool)

    def test_the_descriptions_lead_with_the_read_they_need(self):
        self.assertIn("list_projects", MANAGE_PROJECT.description)
        self.assertIn("get_team_members", MANAGE_PROJECT.description)
        self.assertIn("list_projects", MANAGE_PROJECT_MEMBERS.description)
        self.assertIn("get_team_members", MANAGE_PROJECT_MEMBERS.description)


class CreateProjectTests(ProjectToolBase):
    def test_it_makes_the_whole_project_the_page_makes(self):
        out = self.create(name="Website Rebuild")
        project = ProjectMaster.objects.get(project_id=out["project_id"])

        self.assertEqual(project.project_name, "Website Rebuild")
        self.assertEqual(project.team_id, self.team.team_id)
        self.assertEqual(project.owner_id, self.user.id)
        self.assertFalse(project.is_private)
        self.assertTrue(project.code)
        # The creator is a member, and so is in the project's chat.
        self.assertTrue(ProjectMembers.objects.filter(project=project, attendee=self.user).exists())
        pm = Channel.objects.get(project_id=project.project_id, kind=ChannelKind.PM)
        self.assertTrue(ChannelMember.objects.filter(channel=pm, user=self.user).exists())
        # The project's own account: no password, never signs in, on the team.
        system_user = project.project_system_user
        self.assertTrue(system_user.is_system_user)
        self.assertFalse(system_user.has_usable_password())
        self.assertTrue(TeamMembers.objects.filter(team=self.team, attendee=system_user).exists())
        self.assertIn("Created project", out["__summary__"])

    def test_the_owner_is_always_the_person_asking(self):
        """The REST create trusts an owner the client sends. A tool never does."""
        out = self.create(ctx=self.ctx2, name="Mine")
        self.assertEqual(
            ProjectMaster.objects.get(project_id=out["project_id"]).owner_id, self.user2.id
        )

    def test_a_private_project_is_private(self):
        out = self.create(name="Skunkworks", is_private=True)
        self.assertTrue(ProjectMaster.objects.get(project_id=out["project_id"]).is_private)
        self.assertTrue(out["is_private"])

    def test_the_code_is_derived_and_unique_in_the_team(self):
        first = self.create(name="Website")
        second = self.create(name="Web Wizards")
        self.assertNotEqual(first["code"], second["code"])
        self.assertNotIn(first["code"], {"REL"})

    def test_people_are_added_besides_the_owner(self):
        sam = self.new_person("sam")
        out = self.create(name="Crew", member_ids=[str(sam.id), str(self.user2.id)])
        project = ProjectMaster.objects.get(project_id=out["project_id"])
        self.assertEqual(
            set(
                ProjectMembers.objects.filter(project=project).values_list("attendee_id", flat=True)
            ),
            {self.user.id, sam.id, self.user2.id},
        )
        self.assertEqual(out["members"], 3)
        self.assertEqual(
            SecurityEvent.objects.filter(
                action=SecurityAction.MEMBER_ADDED, metadata__project_id=project.project_id
            ).count(),
            2,
            "the owner's own membership is not an 'added' event",
        )

    def test_duplicates_and_the_owner_in_the_list_are_ignored(self):
        sam = self.new_person("sam")
        out = self.create(name="Crew", member_ids=[str(sam.id), str(sam.id), str(self.user.id)])
        self.assertEqual(out["members"], 2)

    def test_a_person_outside_the_team_is_refused_and_nothing_is_created(self):
        outsider = self.new_person("outsider", in_team=False)
        users_before = User.objects.count()
        with self.assertRaises(ToolError) as caught:
            self.create(name="Leaky", member_ids=[str(outsider.id)])
        self.assertIn("not a member of this team", str(caught.exception))
        self.assertFalse(ProjectMaster.objects.filter(project_name="Leaky").exists())
        self.assertEqual(User.objects.count(), users_before, "no system user either")

    def test_a_failure_part_way_leaves_nothing_behind(self):
        sam = self.new_person("sam")
        users_before = User.objects.count()
        with mock.patch(
            "origin.services.project_manage.record_member_added", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(ToolError) as caught:
                self.create(name="Half", member_ids=[str(sam.id)])
        self.assertIn("Failed to create", str(caught.exception))
        self.assertFalse(ProjectMaster.objects.filter(project_name="Half").exists())
        self.assertEqual(User.objects.count(), users_before)

    def test_a_name_already_used_in_the_team_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.create(name="Release")
        self.assertIn("already uses that name", str(caught.exception))
        self.assertEqual(ProjectMaster.objects.filter(project_name="Release").count(), 1)

    def test_the_same_name_in_another_team_is_fine(self):
        other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@team.example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user2)
        ctx = ToolContext(team_id=str(other_team.team_id), user_id=str(self.user2.id))
        out = self.create(ctx=ctx, name="Release")
        self.assertEqual(
            ProjectMaster.objects.get(project_id=out["project_id"]).team_id, other_team.team_id
        )

    def test_a_bad_name_is_refused_with_nothing_created(self):
        for name in (None, "", "   ", "n" * 121):
            with self.subTest(name=name), self.assertRaises(ToolError):
                self.create(name=name)
        self.assertEqual(ProjectMaster.objects.count(), 1)

    def test_a_long_name_says_how_long_it_may_be(self):
        with self.assertRaises(ToolError) as caught:
            self.create(name="n" * 121)
        self.assertIn("120", str(caught.exception))

    def test_an_address_an_earlier_project_already_holds_gets_a_suffix(self):
        """The page names the project's account after the team and the project, so
        a project of that name that existed before already holds that address."""
        User.objects.create_user(
            email=f"{self.team.team_id}-Phoenix@genos.tech",
            username="Phoenix",
            password=None,
            is_system_user=True,
        )
        out = self.create(name="Phoenix")
        project = ProjectMaster.objects.get(project_id=out["project_id"])
        self.assertTrue(project.project_system_user.is_system_user)
        self.assertNotEqual(
            project.project_system_user.email, f"{self.team.team_id}-Phoenix@genos.tech"
        )

    def test_member_ids_must_be_a_list(self):
        """A dict keyed by user ids would add them if it were walked like a list, so
        the type check is what stops it, not the lookup that follows."""
        sam = self.new_person("sam")
        for bad in ("sam", {str(sam.id): True}, 5):
            with self.subTest(member_ids=bad):
                with self.assertRaisesMessage(ToolError, "`member_ids` must be a list"):
                    self.create(name="x", member_ids=bad)
        self.assertFalse(ProjectMaster.objects.filter(project_name="x").exists())

    def test_someone_outside_the_team_cannot_create(self):
        outsider = self.new_person("outsider", in_team=False)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(outsider.id))
        with self.assertRaises(ToolForbidden):
            self.create(ctx=ctx, name="Nope")
        self.assertFalse(ProjectMaster.objects.filter(project_name="Nope").exists())


class UpdateProjectTests(ProjectToolBase):
    def test_the_owner_renames(self):
        out = self.update(name="Launch")
        self.project.refresh_from_db()
        self.assertEqual(self.project.project_name, "Launch")
        self.assertEqual(out["name"], "Launch")

    def test_the_code_is_changed_and_upper_cased(self):
        out = self.update(code="web")
        self.project.refresh_from_db()
        self.assertEqual(self.project.code, "WEB")
        self.assertEqual(out["code"], "WEB")

    def test_a_code_change_reindexes_the_projects_tasks(self):
        with mock.patch("origin.services.project_manage.mark_projects_dirty") as dirty:
            self.update(code="WEB")
        dirty.assert_called_once_with([self.project.project_id])

    def test_an_invalid_or_taken_code_is_refused(self):
        ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="TAKEN",
            owner=self.user,
            project_system_user=self.user,
        )
        for code, needle in (
            ("1ab", "2-6 uppercase"),
            ("A", "2-6 uppercase"),
            ("TOOLONGX", "2-6"),
            ("TAKEN", "already uses that code"),
        ):
            with self.subTest(code=code), self.assertRaises(ToolError) as caught:
                self.update(code=code)
            self.assertIn(needle, str(caught.exception))
        self.project.refresh_from_db()
        self.assertEqual(self.project.code, "REL")

    def test_a_name_taken_in_the_team_is_refused(self):
        ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        with self.assertRaises(ToolError) as caught:
            self.update(name="Other")
        self.assertIn("already uses that name", str(caught.exception))
        self.project.refresh_from_db()
        self.assertEqual(self.project.project_name, "Release")

    def test_keeping_its_own_name_is_not_a_collision(self):
        self.update(name="Release")

    def test_an_edit_that_changes_nothing_is_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.update()
        self.assertIn("Nothing to change", str(caught.exception))

    def test_an_editor_may_rename_but_a_viewer_may_not(self):
        editor = self.new_person("ed")
        viewer = self.new_person("vi")
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=editor, member_role="editor"
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=viewer, member_role="viewer"
        )
        self.update(
            ctx=ToolContext(team_id=str(self.team.team_id), user_id=str(editor.id)),
            name="By editor",
        )
        with self.assertRaises(ToolForbidden):
            self.update(
                ctx=ToolContext(team_id=str(self.team.team_id), user_id=str(viewer.id)),
                name="By viewer",
            )
        self.project.refresh_from_db()
        self.assertEqual(self.project.project_name, "By editor")

    def test_someone_who_is_not_in_the_project_is_refused(self):
        with self.assertRaises(ToolForbidden):
            self.update(ctx=self.ctx2, name="Hijacked")
        self.project.refresh_from_db()
        self.assertEqual(self.project.project_name, "Release")

    def test_a_member_acting_for_another_team_is_refused(self):
        other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@team.example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user)
        ctx = ToolContext(team_id=str(other_team.team_id), user_id=str(self.user.id))
        with self.assertRaises(ToolForbidden) as caught:
            self.update(ctx=ctx, name="Wrong team")
        self.assertIn("different team", str(caught.exception))

    def test_a_deleted_or_unknown_project_is_not_found(self):
        ProjectMaster.objects.filter(pk=self.project.pk).update(is_deleted=True)
        with self.assertRaises(ToolError):
            self.update(name="x")
        with self.assertRaises(ToolError):
            MANAGE_PROJECT.run({"action": "update", "project_id": 999999, "name": "x"}, self.ctx)

    def test_an_unknown_action_is_refused_and_named(self):
        for args in ({"action": "delete"}, {}):
            with self.subTest(args=args), self.assertRaises(ToolError) as caught:
                MANAGE_PROJECT.run(args, self.ctx)
            self.assertIn("`action` must be one of", str(caught.exception))


class ThePagesRulesDidNotChange(ProjectToolBase):
    """The REST edit and role views now run through the service. Their answers are
    the contract the page depends on."""

    def put(self, **body):
        body.setdefault("project_id", self.project.project_id)
        return self.client.put(PROJECT, body, format="json")

    def test_rename_collision_is_409_with_the_same_message(self):
        ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        res = self.put(project_name="Other")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["error"], "Another project in this team already uses that name.")

    def test_an_empty_name_is_400(self):
        res = self.put(project_name="  ")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error"], "project_name cannot be empty.")

    def test_code_rules_and_messages(self):
        res = self.put(code="1x")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            res.data["error"], "Code must be 2-6 uppercase letters/digits, starting with a letter."
        )
        res = self.put(code="")
        self.assertEqual((res.status_code, res.data["error"]), (400, "Code cannot be empty."))

    def test_a_code_change_still_works_and_is_upper_cased(self):
        res = self.put(code="web")
        self.assertEqual(res.status_code, 200, res.content)
        self.project.refresh_from_db()
        self.assertEqual(self.project.code, "WEB")

    def test_a_non_manager_is_403(self):
        self.authenticate(self.user2)
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.user2, member_role="viewer"
        )
        res = self.put(project_name="x")
        self.assertEqual(res.status_code, 403)

    def test_ownership_transfer_still_works_for_the_owner_only(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        res = self.put(owner_id=str(self.user2.id))
        self.assertEqual(res.status_code, 200, res.content)
        self.project.refresh_from_db()
        self.assertEqual(self.project.owner_id, self.user2.id)
        self.assertTrue(
            SecurityEvent.objects.filter(action=SecurityAction.OWNER_TRANSFERRED).exists()
        )
        # user is no longer the owner, so cannot hand it back.
        res = self.put(owner_id=str(self.user.id))
        self.assertEqual(res.status_code, 403)

    def test_a_new_owner_must_already_be_a_member(self):
        res = self.put(owner_id=str(self.user2.id))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            res.data["error"], "The new owner must already be a member of the project."
        )

    def test_name_and_owner_in_one_request(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        res = self.put(project_name="Both", owner_id=str(self.user2.id))
        self.assertEqual(res.status_code, 200, res.content)
        self.project.refresh_from_db()
        self.assertEqual(
            (self.project.project_name, self.project.owner_id), ("Both", self.user2.id)
        )

    def test_role_endpoint_messages(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        base = {"project_id": self.project.project_id, "user_id": str(self.user2.id)}
        res = self.client.put(ROLE, {**base, "member_role": "owner"}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.put(ROLE, {**base, "member_role": "editor"}, format="json")
        self.assertEqual((res.status_code, res.data["memberRole"]), (200, "editor"))
        res = self.client.put(
            ROLE, {**base, "user_id": str(self.user.id), "member_role": "viewer"}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("owner's role cannot be changed", res.data["error"])
        res = self.client.put(
            ROLE,
            {
                "project_id": self.project.project_id,
                "user_id": "00000000-0000-0000-0000-000000000000",
                "member_role": "viewer",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)


class AddMemberTests(ProjectToolBase):
    def test_a_member_adds_a_teammate_who_also_gets_the_chat(self):
        sam = self.new_person("sam")
        out = self.members("add", user_id=str(sam.id))
        self.assertFalse(out["already_member"])
        self.assertTrue(ProjectMembers.objects.filter(project=self.project, attendee=sam).exists())
        pm = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        self.assertTrue(ChannelMember.objects.filter(channel=pm, user=sam).exists())
        self.assertTrue(
            SecurityEvent.objects.filter(
                action=SecurityAction.MEMBER_ADDED, target_id=str(sam.id)
            ).exists()
        )

    def test_a_new_member_starts_as_a_viewer_like_the_page(self):
        sam = self.new_person("sam")
        self.members("add", user_id=str(sam.id))
        self.assertEqual(self.role_of(sam), "viewer")

    def test_adding_someone_already_in_is_harmless_and_records_nothing_twice(self):
        sam = self.new_person("sam")
        self.members("add", user_id=str(sam.id))
        events = SecurityEvent.objects.filter(action=SecurityAction.MEMBER_ADDED).count()
        again = self.members("add", user_id=str(sam.id))
        self.assertTrue(again["already_member"])
        self.assertEqual(
            SecurityEvent.objects.filter(action=SecurityAction.MEMBER_ADDED).count(), events
        )
        self.assertEqual(
            ProjectMembers.objects.filter(project=self.project, attendee=sam).count(), 1
        )

    def test_the_person_must_be_named(self):
        """Left out, it must say so, not fall through to a lookup of nobody."""
        for action in ("add", "set_role"):
            for missing in ("", "  ", None):
                with self.subTest(action=action, user_id=missing):
                    with self.assertRaisesMessage(ToolError, "`user_id` is required"):
                        self.members(action, user_id=missing, role="editor")
        self.assertEqual(ProjectMembers.objects.filter(project=self.project).count(), 1)

    def test_a_person_outside_the_team_cannot_be_added(self):
        outsider = self.new_person("outsider", in_team=False)
        with self.assertRaises(ToolError) as caught:
            self.members("add", user_id=str(outsider.id))
        self.assertIn("not a member of this team", str(caught.exception))
        self.assertFalse(ProjectMembers.objects.filter(attendee=outsider).exists())

    def test_any_project_member_may_add_but_a_stranger_may_not(self):
        sam = self.new_person("sam")
        member = self.new_person("member")
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=member, member_role="viewer"
        )
        self.members(
            "add",
            ctx=ToolContext(team_id=str(self.team.team_id), user_id=str(member.id)),
            user_id=str(sam.id),
        )
        self.assertTrue(ProjectMembers.objects.filter(project=self.project, attendee=sam).exists())
        bystander = self.new_person("bystander")
        with self.assertRaises(ToolForbidden):
            self.members(
                "add",
                ctx=ToolContext(team_id=str(self.team.team_id), user_id=str(bystander.id)),
                user_id=str(bystander.id),
            )
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=bystander).exists()
        )

    def test_a_member_acting_for_another_team_is_refused(self):
        sam = self.new_person("sam")
        other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@team.example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user)
        ctx = ToolContext(team_id=str(other_team.team_id), user_id=str(self.user.id))
        with self.assertRaises(ToolForbidden):
            self.members("add", ctx=ctx, user_id=str(sam.id))

    def test_a_user_id_is_required(self):
        for value in (None, "", "  "):
            with self.subTest(value=value), self.assertRaises(ToolError):
                self.members("add", user_id=value)

    def test_an_unknown_action_is_refused_and_named(self):
        with self.assertRaises(ToolError) as caught:
            MANAGE_PROJECT_MEMBERS.run(
                {"action": "archive", "project_id": 1, "user_id": "x"}, self.ctx
            )
        self.assertIn("`action` must be one of", str(caught.exception))


class SetRoleTests(ProjectToolBase):
    def setUp(self):
        super().setUp()
        self.sam = self.new_person("sam")
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.sam, member_role="viewer"
        )

    def test_the_owner_makes_someone_an_editor_and_it_is_recorded(self):
        out = self.members("set_role", user_id=str(self.sam.id), role="editor")
        self.assertEqual((out["previous_role"], out["role"]), ("viewer", "editor"))
        self.assertEqual(self.role_of(self.sam), "editor")
        self.assertTrue(
            SecurityEvent.objects.filter(
                action=SecurityAction.ROLE_CHANGED, target_id=str(self.sam.id)
            ).exists()
        )

    def test_setting_the_same_role_records_nothing(self):
        self.members("set_role", user_id=str(self.sam.id), role="viewer")
        self.assertFalse(SecurityEvent.objects.filter(action=SecurityAction.ROLE_CHANGED).exists())

    def test_owner_is_not_a_role_anyone_can_be_given(self):
        for role in ("owner", "admin", "", None):
            with self.subTest(role=role), self.assertRaises(ToolError):
                self.members("set_role", user_id=str(self.sam.id), role=role)
        self.assertEqual(self.role_of(self.sam), "viewer")

    def test_the_owners_own_role_cannot_be_changed(self):
        with self.assertRaises(ToolError) as caught:
            self.members("set_role", user_id=str(self.user.id), role="viewer")
        self.assertIn("owner's role cannot be changed", str(caught.exception))

    def test_someone_who_is_not_a_member_is_refused(self):
        stranger = self.new_person("stranger")
        with self.assertRaises(ToolError) as caught:
            self.members("set_role", user_id=str(stranger.id), role="editor")
        self.assertIn("not a member of this project", str(caught.exception))

    def test_an_editor_may_set_roles_but_a_viewer_may_not(self):
        editor = self.new_person("ed")
        viewer = self.new_person("vi")
        for person, role in ((editor, "editor"), (viewer, "viewer")):
            ProjectMembers.objects.create(
                team=self.team, project=self.project, attendee=person, member_role=role
            )
        team = str(self.team.team_id)

        self.members(
            "set_role",
            ctx=ToolContext(team_id=team, user_id=str(editor.id)),
            user_id=str(self.sam.id),
            role="editor",
        )
        self.assertEqual(self.role_of(self.sam), "editor")

        with self.assertRaises(ToolForbidden):
            self.members(
                "set_role",
                ctx=ToolContext(team_id=team, user_id=str(viewer.id)),
                user_id=str(self.sam.id),
                role="viewer",
            )
        self.assertEqual(self.role_of(self.sam), "editor", "the viewer changed nothing")

    def test_someone_who_is_not_in_the_project_cannot_set_roles(self):
        with self.assertRaises(ToolForbidden):
            self.members("set_role", ctx=self.ctx2, user_id=str(self.sam.id), role="editor")
        self.assertEqual(self.role_of(self.sam), "viewer")
