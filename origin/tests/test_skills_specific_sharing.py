"""Sharing a skill with chosen people (MIP-2212): the `specific` visibility and its grants.

What matters, in the order tested:

  * ACL per grant type: a person, a LIVE mention group, a LIVE group chat, a project (member and
    owner). A teammate nobody named, a person on another team, a project guest or a chat member who
    is not on the team, and a person who left the team all get the same "not found" as a missing id.
    `can_read` and the `visible_to` filter answer identically over a matrix of people.
  * Membership is checked at every read and run: join a group and the skill appears, leave and it is
    gone; a deleted group, chat or project ends its grant.
  * A revoked grant stops access at once; moving the skill to private stops everybody, and moving it
    back brings the list back.
  * Versions like a team skill: going `specific` cuts one and a grantee reads the published text, not
    the draft; a `specific` skill with nobody chosen is refused.
  * Hidden, in review, reported and taken-down skills are not read or run by grantees (the same
    rules as a team skill), and the team owner can show a hidden one again.
  * `/name`, run by id, the picker, a schedule and the MCP tools all use the one rule; the author is
    a teammate; the caller's own tool ceiling still applies.
  * The owner's choices are checked: people on the team, groups of the team, chats and projects the
    OWNER belongs to, never an external chat, a DM, or a hidden chat the owner is not in.
  * Leaving the team ends the leaver's own `specific` sharing and the grants that named them.
"""

from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.skills.skill_models import Skill, SkillGrant
from origin.services import team_membership
from origin.services.skills import run, service
from origin.tests.test_mcp_skills import McpSkillBase
from origin.tests.test_skills_api import LIST, detail
from origin.tests.test_skills_run import BODY, RunBase, fields

User = get_user_model()


class SpecificBase(RunBase):
    """`self.user2` owns the skills, `self.third` is the one they are shared with, `self.user` (the
    team owner) and `self.fourth` are teammates nobody names."""

    def setUp(self):
        super().setUp()
        self.owner = self.user2
        self.fourth = User.objects.create_user(
            username="fourth", email="fourth@example.com", password="x-pass-123"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.fourth)
        self.group = MentionGroupMaster.objects.create(
            team=self.team, group_name="design", created_by=self.owner
        )
        self.chat = self.group_chat("Design chat", [self.owner])
        self.project = self.make_project("Rocket", [self.owner])

    # -- fixtures ------------------------------------------------------------------------------
    def group_chat(self, title, members, **over):
        chat = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title=title, owner=self.owner, **over
        )
        for member in members:
            ChannelMember.objects.get_or_create(
                channel=chat, user=member, defaults={"role": "member"}
            )
        return chat

    def make_project(self, name, members, owner=None):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            owner=owner or self.owner,
            project_system_user=owner or self.owner,
        )
        for member in members:
            ProjectMembers.objects.get_or_create(team=self.team, project=project, attendee=member)
        return project

    def specific(self, shares, owner=None, **over):
        owner = owner or self.owner
        return service.create(
            owner,
            self.team.team_id,
            fields(visibility="specific", shares=shares, slug=over.pop("slug", "shared"), **over),
        )

    @staticmethod
    def person(user):
        return {"kind": "user", "id": str(user.pk)}

    def readable(self, skill, user):
        return service.can_read(skill, user.pk)

    def filtered(self, skill, user):
        return Skill.objects.filter(service.visible_to(user.pk), pk=skill.pk).exists()

    def assert_reads(self, skill, user, expected, note=""):
        self.assertEqual(self.readable(skill, user), expected, f"can_read {user.username} {note}")
        self.assertEqual(self.filtered(skill, user), expected, f"visible_to {user.username} {note}")


class AccessPerGrantTypeTests(SpecificBase):
    def test_a_named_person_reads_and_nobody_else_on_the_team_does(self):
        skill = self.specific([self.person(self.third)])
        self.assert_reads(skill, self.owner, True)
        self.assert_reads(skill, self.third, True)
        for other in (self.user, self.fourth):
            self.assert_reads(skill, other, False)
        self.assertEqual(service.get_readable(self.third.pk, skill.id).pk, skill.pk)
        with self.assertRaises(service.SkillNotFound):
            service.get_readable(self.fourth.pk, skill.id)

    def test_a_mention_group_is_a_live_link(self):
        skill = self.specific([{"kind": "mentionGroup", "id": self.group.group_id}])
        self.assert_reads(skill, self.third, False, "not in the group yet")
        MentionGroupMembers.objects.create(team=self.team, group=self.group, user=self.third)
        self.assert_reads(skill, self.third, True, "joined the group")
        self.assert_reads(skill, self.fourth, False)
        MentionGroupMembers.objects.filter(group=self.group, user=self.third).delete()
        self.assert_reads(skill, self.third, False, "left the group")

    def test_a_group_chat_is_a_live_link(self):
        skill = self.specific([{"kind": "groupChat", "id": str(self.chat.pk)}])
        self.assert_reads(skill, self.third, False)
        ChannelMember.objects.create(channel=self.chat, user=self.third, role="member")
        self.assert_reads(skill, self.third, True, "joined the chat")
        ChannelMember.objects.filter(channel=self.chat, user=self.third).update(is_deleted=True)
        self.assert_reads(skill, self.third, False, "left the chat")

    def test_a_project_is_a_live_link_and_its_owner_counts_without_a_row(self):
        skill = self.specific([{"kind": "project", "id": self.project.project_id}])
        self.assert_reads(skill, self.third, False)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.third)
        self.assert_reads(skill, self.third, True, "joined the project")
        ProjectMembers.objects.filter(project=self.project, attendee=self.third).delete()
        self.assert_reads(skill, self.third, False, "left the project")
        # A project's owner may have no member row of their own.
        self.project.owner = self.fourth
        self.project.save(update_fields=["owner"])
        ProjectMembers.objects.filter(project=self.project, attendee=self.fourth).delete()
        self.assert_reads(skill, self.fourth, True, "owner without a row")

    def test_a_deleted_group_chat_or_project_ends_its_grant(self):
        ChannelMember.objects.create(channel=self.chat, user=self.third, role="member")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.third)
        MentionGroupMembers.objects.create(team=self.team, group=self.group, user=self.third)
        for kind, ident, kill in (
            (
                "mentionGroup",
                self.group.group_id,
                lambda: MentionGroupMaster.objects.filter(pk=self.group.pk).update(is_deleted=True),
            ),
            (
                "groupChat",
                str(self.chat.pk),
                lambda: Channel.objects.filter(pk=self.chat.pk).update(is_deleted=True),
            ),
            (
                "project",
                self.project.project_id,
                lambda: ProjectMaster.objects.filter(pk=self.project.pk).update(is_deleted=True),
            ),
        ):
            skill = self.specific([{"kind": kind, "id": ident}], slug=f"del-{kind.lower()}")
            self.assert_reads(skill, self.third, True, kind)
            kill()
            self.assert_reads(skill, self.third, False, f"{kind} deleted")

    def test_a_dm_is_not_a_group_chat(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.owner)
        ChannelMember.objects.create(channel=dm, user=self.third, role="member")
        skill = self.specific([self.person(self.fourth)])
        SkillGrant.objects.create(skill=skill, channel=dm)  # not reachable through the API
        self.assert_reads(skill, self.third, False)

    def test_a_grant_never_reaches_outside_the_team(self):
        MentionGroupMembers.objects.create(team=self.team, group=self.group, user=self.third)
        skill = self.specific(
            [
                {"kind": "mentionGroup", "id": self.group.group_id},
                {"kind": "groupChat", "id": str(self.chat.pk)},
                {"kind": "project", "id": self.project.project_id},
            ]
        )
        # In the chat and the project but on no team here: an outsider, or a guest.
        ChannelMember.objects.create(channel=self.chat, user=self.outsider, role="member")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.outsider)
        MentionGroupMembers.objects.create(team=self.team, group=self.group, user=self.outsider)
        self.assert_reads(skill, self.outsider, False, "no team membership")
        self.assert_reads(skill, self.third, True)
        # A person who leaves the team loses it with no cleanup of the group rows.
        TeamMembers.objects.filter(team=self.team, attendee=self.third).update(is_deleted=True)
        self.assert_reads(skill, self.third, False, "left the team")


class RevokeAndVisibilityTests(SpecificBase):
    def test_replacing_the_shares_takes_access_away_at_once(self):
        skill = self.specific([self.person(self.third), self.person(self.fourth)])
        self.assert_reads(skill, self.fourth, True)
        service.update(self.owner, skill.id, service.Fields(shares=[self.person(self.third)]))
        self.assert_reads(skill, self.fourth, False)
        self.assert_reads(skill, self.third, True)
        self.assertEqual(skill.grants.count(), 1)

    def test_private_stops_everyone_and_specific_again_brings_the_list_back(self):
        skill = self.specific([self.person(self.third)])
        service.update(self.owner, skill.id, service.Fields(visibility="private"))
        skill.refresh_from_db()
        self.assert_reads(skill, self.third, False)
        self.assertEqual(skill.grants.count(), 1)
        service.update(self.owner, skill.id, service.Fields(visibility="specific"))
        skill.refresh_from_db()
        self.assert_reads(skill, self.third, True)

    def test_team_and_specific_do_not_mix(self):
        skill = self.specific([self.person(self.third)])
        service.update(self.owner, skill.id, service.Fields(visibility="team"))
        skill.refresh_from_db()
        self.assert_reads(skill, self.fourth, True, "now a team skill")
        service.update(self.owner, skill.id, service.Fields(visibility="specific"))
        skill.refresh_from_db()
        self.assert_reads(skill, self.fourth, False, "back to chosen people")
        self.assert_reads(skill, self.third, True)

    def test_going_specific_cuts_a_version_and_a_grantee_reads_the_published_text(self):
        skill = self.specific([self.person(self.third)], body_markdown="# V1\n\n1. First text.")
        self.assertEqual(skill.published_version_no, 1)
        service.update(self.owner, skill.id, service.Fields(body_markdown="# V2\n\n1. Draft only."))
        skill.refresh_from_db()
        self.assertIn("First text", service.body_markdown_for(skill, self.third.pk))
        self.assertIn("Draft only", service.body_markdown_for(skill, self.owner.pk))
        self.assertEqual(
            [v.version_no for v in service._readable_versions(skill, self.third.pk)], [1]
        )

    def test_specific_with_nobody_chosen_is_refused(self):
        with self.assertRaises(service.SkillError):
            self.specific([])
        skill = self.specific([self.person(self.third)])
        with self.assertRaises(service.SkillError):
            service.update(self.owner, skill.id, service.Fields(shares=[]))
        skill.refresh_from_db()
        self.assertEqual(skill.grants.count(), 1, "the refusal rolled the change back")
        private = self.skill(self.owner, "private", slug="solo")
        with self.assertRaises(service.SkillError):
            service.update(self.owner, private.id, service.Fields(visibility="specific"))

    def test_names_are_only_unique_per_owner(self):
        self.specific([self.person(self.third)], slug="same")
        self.specific(
            [self.person(self.third)], owner=self.fourth, slug="same"
        )  # another owner: fine
        with self.assertRaises(service.SkillError):
            self.specific([self.person(self.third)], slug="same")


class StatusRulesTests(SpecificBase):
    def test_hidden_review_reported_and_taken_down_are_not_read_or_run_by_grantees(self):
        for status in ("hidden_by_team", "in_review", "reported_hidden", "taken_down"):
            skill = self.specific([self.person(self.third)], slug=f"s-{status.replace('_', '-')}")
            Skill.objects.filter(pk=skill.pk).update(status=status)
            skill.refresh_from_db()
            self.assert_reads(skill, self.third, False, status)
            with self.assertRaises(service.SkillError):
                self.resolve({"id": str(skill.id)}, user=self.third)
            with self.assertRaises(service.SkillError):
                self.resolve({"name": skill.slug}, user=self.third)

    def test_the_team_owner_can_show_a_skill_hidden_while_it_was_a_team_skill(self):
        skill = self.skill(self.owner, "team", slug="was-team")
        service.set_hidden(self.user, skill.id, True)
        service.update(
            self.owner,
            skill.id,
            service.Fields(visibility="specific", shares=[self.person(self.third)]),
        )
        skill.refresh_from_db()
        self.assertEqual(skill.status, "hidden_by_team")
        self.assert_reads(skill, self.third, False)
        self.assertTrue(service.can_read(skill, self.user.pk), "the team owner sees what they hid")
        self.assertTrue(service.can_hide(skill, self.user.pk))
        service.set_hidden(self.user, skill.id, False)
        skill.refresh_from_db()
        self.assert_reads(skill, self.third, True)
        # A team owner who is one of the readers (a chat of the whole team, say) can hide it again,
        # so a skill shared that widely is not one nobody can moderate. One who is not named cannot
        # even see it: hiding needs reading.
        with self.assertRaises(service.SkillNotFound):
            service.set_hidden(self.user, skill.id, True)
        service.update(
            self.owner,
            skill.id,
            service.Fields(shares=[self.person(self.third), self.person(self.user)]),
        )
        service.set_hidden(self.user, skill.id, True)
        skill.refresh_from_db()
        self.assertEqual(skill.status, "hidden_by_team")
        self.assert_reads(skill, self.third, False)


class RunTests(SpecificBase):
    def test_a_grantee_runs_it_by_id_and_by_name_as_a_teammates_skill(self):
        skill = self.specific([self.person(self.third)], slug="mine-to-share")
        by_id = self.resolve({"id": str(skill.id)}, user=self.third)
        by_name = self.resolve({"name": "mine-to-share"}, user=self.third)
        for got in (by_id, by_name):
            self.assertEqual(got.skill_id, str(skill.id))
            self.assertEqual(got.author, run.AUTHOR_TEAMMATE)
            self.assertFalse(got.draft)
        for stranger in (self.fourth, self.user):
            with self.assertRaises(service.SkillNotFound):
                self.resolve({"id": str(skill.id)}, user=stranger)
            with self.assertRaises(service.SkillNotFound):
                self.resolve({"name": "mine-to-share"}, user=stranger)

    def test_a_revoked_grant_stops_the_next_run(self):
        skill = self.specific([self.person(self.third), self.person(self.fourth)], slug="revoke")
        self.assertEqual(self.resolve({"name": "revoke"}, user=self.fourth).skill_id, str(skill.id))
        service.update(self.owner, skill.id, service.Fields(shares=[self.person(self.third)]))
        with self.assertRaises(service.SkillNotFound):
            self.resolve({"name": "revoke"}, user=self.fourth)

    def test_a_group_member_who_leaves_stops_running_it(self):
        skill = self.specific([{"kind": "groupChat", "id": str(self.chat.pk)}], slug="chat-run")
        ChannelMember.objects.create(channel=self.chat, user=self.third, role="member")
        self.assertEqual(
            self.resolve({"name": "chat-run"}, user=self.third).skill_id, str(skill.id)
        )
        ChannelMember.objects.filter(channel=self.chat, user=self.third).update(is_deleted=True)
        with self.assertRaises(service.SkillNotFound):
            self.resolve({"name": "chat-run"}, user=self.third)

    def test_a_team_and_a_specific_skill_with_one_name_are_both_offered_not_guessed(self):
        self.skill(self.user, "team", slug="clash")
        self.specific([self.person(self.third)], slug="clash")
        with self.assertRaises(run.SkillAmbiguous):
            self.resolve({"name": "clash"}, user=self.third)
        # A teammate who is not named sees only the team skill.
        self.assertEqual(
            self.resolve({"name": "clash"}, user=self.fourth).author, run.AUTHOR_TEAMMATE
        )

    def test_the_picker_lists_it_for_the_grantee_only(self):
        skill = self.specific([self.person(self.third)], slug="picked")
        rows = run.runnable(self.third, self.team.team_id)
        row = next(r for r in rows if r["id"] == str(skill.id))
        self.assertEqual(
            (row["group"], row["visibility"], row["mine"]), ("team", "specific", False)
        )
        for other in (self.fourth, self.user):
            self.assertNotIn(
                str(skill.id), [r["id"] for r in run.runnable(other, self.team.team_id)]
            )

    def test_the_callers_tool_ceiling_still_applies(self):
        read = self.specific([self.person(self.third)], slug="read-only", tool_level="read")
        act = self.specific([self.person(self.third)], slug="may-act", tool_level="act")
        from origin.search_engine.agent.tool_tiers import SINGLE_WRITE_TOOLS

        self.assertTrue(
            SINGLE_WRITE_TOOLS
            & run.disabled_tools(self.resolve({"id": str(read.id)}, user=self.third))
        )
        hidden = run.disabled_tools(self.resolve({"id": str(act.id)}, user=self.third))
        self.assertFalse(SINGLE_WRITE_TOOLS & hidden, "act may propose single changes")

    def test_only_the_owner_copies_it_so_the_audience_cannot_be_widened(self):
        skill = self.specific([self.person(self.third)], slug="copyable")
        with self.assertRaises(service.SkillForbidden):
            service.copy(self.third, skill.id, self.team.team_id)
        with self.assertRaises(service.SkillNotFound):
            service.copy(self.fourth, skill.id, self.team.team_id)
        self.assertEqual(
            service.copy(self.owner, skill.id, self.team.team_id).visibility, "private"
        )


class ListAndRestTests(SpecificBase):
    def test_the_team_tab_lists_skills_shared_with_me_in_one_query(self):
        for i in range(6):
            self.specific([self.person(self.third)], slug=f"many-{i}")
        self.specific([self.person(self.fourth)], slug="not-mine")
        service.list_for(self.third, self.team.team_id, "team")  # warm anything cached
        with CaptureQueriesContext(connection) as small:
            rows = list(service.list_for(self.third, self.team.team_id, "team"))
        self.assertEqual(len(rows), 6)
        self.assertNotIn("not-mine", [r.slug for r in rows])
        for i in range(6, 12):
            self.specific([self.person(self.third)], slug=f"many-{i}")
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(len(list(service.list_for(self.third, self.team.team_id, "team"))), 12)
        self.assertEqual(len(small), len(large), "the list is one query however many skills")

    def test_rest_saves_the_shares_and_only_the_owner_reads_them_back(self):
        self.as_user(self.owner)
        made = self.client.post(
            LIST,
            {
                "team_id": str(self.team.team_id),
                "title": "Shared review",
                "slug": "shared-review",
                "bodyMarkdown": BODY,
                "visibility": "specific",
                "shares": [
                    self.person(self.third),
                    {"kind": "mentionGroup", "id": self.group.group_id},
                    {"kind": "groupChat", "id": str(self.chat.pk)},
                    {"kind": "project", "id": self.project.project_id},
                ],
            },
            format="json",
        )
        self.assertEqual(made.status_code, 201, made.content)
        skill_id = made.json()["skill"]["id"]
        got = self.client.get(detail(skill_id)).json()["skill"]
        self.assertEqual(got["visibility"], "specific")
        by_kind = {s["kind"]: s for s in got["shares"]}
        self.assertEqual(set(by_kind), {"user", "mentionGroup", "groupChat", "project"})
        self.assertEqual(by_kind["user"]["name"], "third")
        self.assertEqual(by_kind["groupChat"]["name"], "Design chat")
        self.assertEqual(by_kind["groupChat"]["memberCount"], 1)
        self.assertEqual(by_kind["project"]["name"], "Rocket")
        self.assertTrue(all(s["available"] for s in got["shares"]))
        # The grantee reads it without the list of who else was chosen.
        self.as_user(self.third)
        seen = self.client.get(detail(skill_id))
        self.assertEqual(seen.status_code, 200)
        self.assertNotIn("shares", seen.json()["skill"])
        self.assertEqual(seen.json()["skill"]["role"], "reader")
        # Somebody nobody named gets the not-found.
        self.as_user(self.fourth)
        self.assertEqual(self.client.get(detail(skill_id)).status_code, 404)

    def test_a_chat_the_owner_has_left_is_listed_without_its_name(self):
        skill = self.specific([{"kind": "groupChat", "id": str(self.chat.pk)}])
        ChannelMember.objects.filter(channel=self.chat, user=self.owner).update(is_deleted=True)
        (row,) = service.grants_view(skill, self.owner.pk)
        self.assertEqual((row["name"], row["available"]), ("", False))

    def test_a_co_editor_cannot_change_who_it_is_shared_with(self):
        skill = self.specific([self.person(self.third)])
        service.add_editor(self.owner, skill.id, self.fourth.pk)
        with self.assertRaises(service.SkillForbidden):
            service.update(self.fourth, skill.id, service.Fields(shares=[self.person(self.fourth)]))
        self.assertTrue(service.can_read(skill, self.fourth.pk), "co-editors read it anyway")


class ValidationTests(SpecificBase):
    def refuse(self, shares):
        skill = Skill.objects.filter(owner=self.owner, slug="vv").first() or self.skill(
            self.owner, "private", slug="vv"
        )
        with self.assertRaises(service.SkillError):
            service.update(self.owner, skill.id, service.Fields(shares=shares))
        self.assertEqual(skill.grants.count(), 0)

    def test_each_refusal(self):
        elsewhere_group = MentionGroupMaster.objects.create(
            team=self.other_team, group_name="out", created_by=self.outsider
        )
        not_mine_chat = self.group_chat("Secret", [self.fourth])
        external_chat = self.group_chat("Shared out", [self.owner], is_external=True)
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.owner)
        ChannelMember.objects.create(channel=dm, user=self.owner, role="member")
        not_mine_project = self.make_project("Hidden", [self.fourth], owner=self.fourth)
        gone_project = self.make_project("Gone", [self.owner])
        ProjectMaster.objects.filter(pk=gone_project.pk).update(is_deleted=True)
        for shares in (
            [self.person(self.outsider)],  # not on the team
            [self.person(self.owner)],  # themselves
            [{"kind": "user", "id": "not-a-uuid"}],
            [{"kind": "mentionGroup", "id": elsewhere_group.group_id}],  # another team's group
            [{"kind": "mentionGroup", "id": 99999999}],
            [{"kind": "groupChat", "id": str(not_mine_chat.pk)}],  # a chat the owner is not in
            [{"kind": "groupChat", "id": str(external_chat.pk)}],  # a cross-team chat
            [{"kind": "groupChat", "id": str(dm.pk)}],  # a DM
            [
                {"kind": "project", "id": not_mine_project.project_id}
            ],  # a project the owner is not in
            [{"kind": "project", "id": gone_project.project_id}],
            [{"kind": "team", "id": "x"}],
            [{"kind": "user"}],
            ["not a dict"],
        ):
            self.refuse(shares)
        with self.assertRaises(service.SkillError):
            service.update(
                self.owner,
                self.skill(self.owner, "private", slug="ww").id,
                service.Fields(shares="all"),
            )

    def test_the_same_sentence_for_a_missing_and_a_forbidden_target(self):
        not_mine_chat = self.group_chat("Secret", [self.fourth])
        skill = self.skill(self.owner, "private", slug="same-words")
        words = set()
        for ident in (str(not_mine_chat.pk), "00000000-0000-4000-8000-000000000000"):
            with self.assertRaises(service.SkillError) as ctx:
                service.update(
                    self.owner,
                    skill.id,
                    service.Fields(shares=[{"kind": "groupChat", "id": ident}]),
                )
            words.add(str(ctx.exception))
        self.assertEqual(len(words), 1, "a hidden chat is not confirmed")

    def test_at_most_fifty_and_duplicates_count_once(self):
        people = []
        for i in range(service.MAX_GRANTS + 1):
            u = User.objects.create_user(
                username=f"p{i}", email=f"p{i}@example.com", password="x-pass-123"
            )
            TeamMembers.objects.create(team=self.team, attendee=u)
            people.append(self.person(u))
        self.refuse(people)
        skill = self.skill(self.owner, "private", slug="fifty")
        service.update(self.owner, skill.id, service.Fields(shares=[*people[:50], *people[:5]]))
        self.assertEqual(skill.grants.count(), 50)

    def test_a_grant_the_owner_can_no_longer_see_is_kept_when_the_list_is_saved_again(self):
        skill = self.specific(
            [{"kind": "groupChat", "id": str(self.chat.pk)}, self.person(self.third)]
        )
        ChannelMember.objects.filter(channel=self.chat, user=self.owner).update(is_deleted=True)
        service.update(
            self.owner,
            skill.id,
            service.Fields(shares=[{"kind": "groupChat", "id": str(self.chat.pk)}]),
        )
        self.assertEqual(skill.grants.count(), 1)

    def test_a_huge_list_is_refused_before_it_is_walked_and_ids_are_normalised(self):
        with self.assertRaises(service.SkillError):
            service._parse_shares(
                [{"kind": "user", "id": str(i)} for i in range(service.MAX_GRANTS * 2 + 1)]
            )
        upper = str(self.chat.pk).upper()
        self.assertEqual(
            service._parse_shares(
                [
                    {"kind": "groupChat", "id": upper},
                    {"kind": "groupChat", "id": str(self.chat.pk)},
                    {"kind": "project", "id": "007"},
                ]
            ),
            [("groupChat", str(self.chat.pk)), ("project", "7")],
        )
        skill = self.skill(self.owner, "private", slug="unicode")
        with self.assertRaises(service.SkillError):  # a Unicode digit is a refusal, not a 500
            service.update(
                self.owner, skill.id, service.Fields(shares=[{"kind": "project", "id": "²"}])
            )

    def test_a_specific_skill_needs_somebody_who_can_still_be_named(self):
        private = self.skill(self.owner, "private", slug="p-only")
        SkillGrant.objects.create(skill=private, mention_group=self.group)
        MentionGroupMaster.objects.filter(pk=self.group.pk).update(is_deleted=True)
        with self.assertRaises(service.SkillError):
            service.update(self.owner, private.id, service.Fields(visibility="specific"))

    def test_a_moderated_skill_cannot_be_shared_with_more_people(self):
        skill = self.specific([self.person(self.third)], slug="held")
        Skill.objects.filter(pk=skill.pk).update(status="in_review")
        with self.assertRaises(service.SkillForbidden):
            service.update(
                self.owner,
                skill.id,
                service.Fields(visibility="team"),
            )


class MembershipChangeTests(SpecificBase):
    def test_leaving_the_team_ends_the_leavers_own_sharing_and_the_grants_naming_them(self):
        mine = self.specific([self.person(self.third)], slug="leaver-owned")
        theirs = self.specific(
            [self.person(self.third), self.person(self.fourth)], owner=self.user, slug="other"
        )
        team_membership.remove_team_member(self.team.team_id, self.owner.pk)
        mine.refresh_from_db()
        self.assertEqual(mine.visibility, "private")
        self.assertEqual(mine.grants.count(), 1, "kept in case they return, inert while private")
        team_membership.remove_team_member(self.team.team_id, self.third.pk)
        self.assertFalse(SkillGrant.objects.filter(skill=theirs, user=self.third).exists())
        self.assertTrue(SkillGrant.objects.filter(skill=theirs, user=self.fourth).exists())
        # Back on the team, the named person does not get the skill back silently.
        TeamMembers.objects.filter(team=self.team, attendee=self.third).update(is_deleted=False)
        theirs.refresh_from_db()
        self.assert_reads(theirs, self.third, False)

    def test_deleting_an_account_removes_the_grants_that_named_it(self):
        skill = self.specific([self.person(self.third), self.person(self.fourth)])
        User.objects.filter(pk=self.third.pk).delete()
        self.assertEqual(skill.grants.count(), 1)
        service.erase_for_user(self.fourth)  # an erased (soft-deleted) account keeps no grant
        self.assertEqual(skill.grants.count(), 0)


class McpTests(SpecificBase, McpSkillBase):
    def test_list_get_and_run_honour_the_grant(self):
        skill = self.specific([self.person(self.third)], slug="mcp-shared")
        sid = str(skill.id)
        # The member named: listed as a teammate's skill, readable, runnable.
        self.other_key(self.third)
        rows = {r["id"]: r for r in self.listed()}
        self.assertEqual(rows[sid]["author"], "teammate")
        self.assertEqual(rows[sid]["group"], "team")
        _, got = self.ok("get_skill", skill="mcp-shared")
        self.assertEqual(got["author"], "teammate")
        text, _ = self.ok("run_skill", skill=sid)
        self.assertIn("a teammate of the person", text)
        # A teammate nobody named: not listed, the same refusal as a missing id.
        self.other_key(self.fourth)
        self.assertNotIn(sid, {r["id"] for r in self.listed()})
        refused = self.refused("get_skill", skill=sid)
        self.assertEqual(
            refused, self.refused("get_skill", skill="00000000-0000-4000-8000-000000000000")
        )
        self.refused("run_skill", skill=sid)
        # Revoked while the key is in use: the next call is refused.
        service.update(self.owner, skill.id, service.Fields(shares=[self.person(self.fourth)]))
        self.other_key(self.third)
        self.refused("run_skill", skill=sid)
        self.assertNotIn(sid, {r["id"] for r in self.listed()})
        self.assertTrue(json.dumps(rows[sid]))  # the row is plain data
