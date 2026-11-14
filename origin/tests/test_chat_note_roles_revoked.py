"""MIP-1482: leaving a chat, a project or a team ends the roles written on its chat notes.

`chat_note_roles` writes an explicit `NotePermissionMaster` row for the people in the chat when
a chat note is created (every member of a DM, the members of a group chat, the creator of a
project-chat note), and `get_explicit_role` honours it ahead of every membership check. Nothing
took it back: someone who left the group chat, was taken off the project or the team, or whose
note was moved to another chat, kept Editor/Owner on the note.

What is revoked, and what is not:

  * a row that came from MEMBERSHIP (written while the person was in the chat, so
    `row.ts_created_at >= membership.ts_joined_at`) goes when that membership ends;
  * a SHARE the owner made on purpose to someone outside the chat (`manage_note_sharing`,
    written before the person joined the chat, if they ever did) is the note's own ACL and stays,
    except when the person leaves the TEAM, which ends every grant inside it;
  * a move re-derives the membership rows from the target chat.

Every removal path asks the same readers `test_chat_note_one_rule` does.
"""

from __future__ import annotations

from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.services import note_sharing
from origin.services.team_membership import remove_team_member
from origin.tests.test_chat_note_one_rule import CHAT, ChatNoteFixtures
from origin.views.utils.note_role import (
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_VIEWER,
    get_effective_role,
    get_explicit_role,
    readable_note_ids,
)


class Base(ChatNoteFixtures):
    def create_note_via_rest(self, channel, creator, title="Notes"):
        """The real writer of the explicit rows: `POST /api/v2/note/chat/`."""
        self.authenticate(creator)
        res = self.client.post(
            "/api/v2/note/chat/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(creator.id),
                "chat_id": str(channel.id),
                "title": title,
                "body": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "secret chat body"}]}
                ],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return ChatNoteMaster.objects.get(note_id=res.json()["noteId"])

    def owned_gm(self, *members):
        """A group chat owned by `self.owner` (so the owner's note row is Owner, the rest Editor)."""
        channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="gm", owner=self.owner
        )
        ChannelMember.objects.create(channel=channel, user=self.owner, role="owner")
        for m in members:
            if m != self.owner:
                ChannelMember.objects.create(channel=channel, user=m)
        return channel

    def assertNoAccess(self, note):
        """Every reader says no, the briefing, the timeline and MCP `get_note` included: the
        note's author is no exception (MIP-1492)."""
        got = self.answers(note)
        self.assertEqual(got, {k: False for k in got}, got)

    def explicit(self, user, note):
        return get_explicit_role(user.id, CHAT, note.note_id)

    def leave_group(self, channel, user, *, by=None):
        self.authenticate(by or user)
        res = self.client.delete(f"/api/v3/channels/{channel.id}/members/{user.id}/")
        self.assertIn(res.status_code, (200, 204), res.content)

    def share(self, note, target, role=ROLE_VIEWER, granter=None):
        note_sharing.share_note(
            team_id=self.team.team_id,
            granter_id=(granter or self.owner).id,
            note_type=CHAT,
            note_id=note.note_id,
            target_user_id=target.id,
            role_id=role,
        )


class LeavingAChat(Base):
    def setUp(self):
        super().setUp()
        self.chat = self.owned_gm(self.reader)
        self.note = self.create_note_via_rest(self.chat, self.owner)

    def test_the_writer_gives_the_member_an_explicit_editor_row(self):
        self.assertEqual(self.explicit(self.reader, self.note), ROLE_EDITOR)
        self.assertEveryoneSays(self.note, True)

    def test_leaving_a_group_chat_ends_access_to_its_notes(self):
        self.leave_group(self.chat, self.reader)
        self.assertIsNone(self.explicit(self.reader, self.note))
        self.assertIsNone(get_effective_role(self.reader.id, CHAT, self.note.note_id))
        self.assertEveryoneSays(self.note, False)

    def test_being_removed_from_a_group_chat_ends_access_too(self):
        self.leave_group(self.chat, self.reader, by=self.owner)
        self.assertIsNone(self.explicit(self.reader, self.note))
        self.assertEveryoneSays(self.note, False)

    def test_withdrawing_a_guest_from_a_shared_chat_ends_access_to_its_notes(self):
        """`remove_external_participants` soft-deletes the member row by queryset update."""
        from types import SimpleNamespace

        from origin.models.common.team_models import ExternalGrant
        from origin.services.external_grants import _remove_participant

        grant = SimpleNamespace(
            object_type=ExternalGrant.ObjectType.CHANNEL,
            object_id=self.chat.id,
            owner_team_id=self.team.team_id,
        )
        self.assertEqual(_remove_participant(grant, self.reader.id), 1)
        self.assertIsNone(self.explicit(self.reader, self.note))
        self.assertEveryoneSays(self.note, False)

    def test_the_others_keep_their_rows(self):
        third = self.gm_member("third")
        self.leave_group(self.chat, self.reader)
        self.assertEqual(self.explicit(self.owner, self.note), ROLE_OWNER)
        self.assertEqual(self.explicit(third, self.note), ROLE_EDITOR)

    def test_rejoining_reads_through_membership_again(self):
        self.leave_group(self.chat, self.reader)
        self.authenticate(self.owner)
        res = self.client.post(
            f"/api/v3/channels/{self.chat.id}/members/",
            {"user_ids": [str(self.reader.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEveryoneSays(self.note, True)
        self.assertEqual(get_effective_role(self.reader.id, CHAT, self.note.note_id), ROLE_EDITOR)

    def test_a_note_of_another_chat_is_left_alone(self):
        other = self.owned_gm(self.reader)
        other_note = self.create_note_via_rest(other, self.owner, "other")
        self.leave_group(self.chat, self.reader)
        self.assertEqual(self.explicit(self.reader, other_note), ROLE_EDITOR)

    def gm_member(self, name):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            username=name, email=f"{name}@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=user)
        ChannelMember.objects.create(channel=self.chat, user=user)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=user,
            note_type=CHAT,
            note_id=self.note.note_id,
            role_id=ROLE_EDITOR,
        )
        return user


class AShareIsNotRevoked(Base):
    """The owner's deliberate share to someone outside the chat is the note's own ACL."""

    def setUp(self):
        super().setUp()
        self.chat = self.owned_gm()
        self.note = self.create_note_via_rest(self.chat, self.owner)
        self.share(self.note, self.reader, ROLE_VIEWER)

    def test_the_share_works(self):
        self.assertEqual(self.explicit(self.reader, self.note), ROLE_VIEWER)
        self.assertEveryoneSays(self.note, True)

    def test_the_share_survives_the_sharee_joining_and_leaving_the_chat(self):
        self.authenticate(self.owner)
        self.client.post(
            f"/api/v3/channels/{self.chat.id}/members/",
            {"user_ids": [str(self.reader.id)]},
            format="json",
        )
        self.leave_group(self.chat, self.reader)
        self.assertEqual(self.explicit(self.reader, self.note), ROLE_VIEWER)
        self.assertEveryoneSays(self.note, True)

    def test_the_share_survives_someone_else_leaving(self):
        other = self.owned_gm(self.reader)  # noqa: F841 - another chat entirely
        third = self.make_member("third")
        ChannelMember.objects.create(channel=self.chat, user=third)
        self.leave_group(self.chat, third)
        self.assertEqual(self.explicit(self.reader, self.note), ROLE_VIEWER)

    def test_the_share_survives_the_owner_leaving_the_chat(self):
        ChannelMember.objects.filter(channel=self.chat, user=self.owner).update(is_deleted=False)
        self.leave_group_member_row(self.chat, self.owner)
        self.assertEqual(self.explicit(self.reader, self.note), ROLE_VIEWER)
        self.assertIsNone(self.explicit(self.owner, self.note))

    def test_a_role_change_on_a_member_is_still_a_membership_row(self):
        """Downgrading someone who is in the chat is not a share to an outsider: leaving ends it."""
        member = self.make_member("member")
        ChannelMember.objects.create(channel=self.chat, user=member)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=member,
            note_type=CHAT,
            note_id=self.note.note_id,
            role_id=ROLE_EDITOR,
        )
        self.share(self.note, member, ROLE_VIEWER)
        self.leave_group(self.chat, member)
        self.assertIsNone(self.explicit(member, self.note))

    def test_a_share_made_while_they_were_out_is_revoked_by_their_next_leave(self):
        """KNOWN LIMITATION, pinned so it is a decision and not a surprise. Rejoining un-deletes
        the member row in place (`ts_joined_at` keeps its first value), so a share the owner made
        between someone's leave and their return reads as written during the membership, and the
        next leave takes it back. The owner can share again."""
        member = self.make_member("member")
        ChannelMember.objects.create(channel=self.chat, user=member)
        self.leave_group(self.chat, member)
        self.share(self.note, member, ROLE_VIEWER)
        ChannelMember.objects.filter(channel=self.chat, user=member).update(is_deleted=False)
        self.leave_group(self.chat, member)
        self.assertIsNone(self.explicit(member, self.note))

    def test_leaving_the_team_ends_even_a_share(self):
        remove_team_member(self.team.team_id, self.reader.id)
        self.assertIsNone(self.explicit(self.reader, self.note))

    def make_member(self, name):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            username=name, email=f"{name}@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=user)
        return user

    def leave_group_member_row(self, channel, user):
        """The owner leaving: only possible after a transfer, so drive the row directly."""
        row = ChannelMember.objects.get(channel=channel, user=user)
        row.is_deleted = True
        row.save()
        from origin.services.chat_note_access import revoke_membership_roles

        revoke_membership_roles(user_id=user.id, channel_id=channel.id, joined_at=row.ts_joined_at)


class LeavingAProject(Base):
    def setUp(self):
        super().setUp()
        self.project = self.project_of("LEA", self.reader)
        self.chat = self.pm_chat(self.project)
        # the member who creates a note on the project chat is its Owner
        self.note = self.create_note_via_rest(self.chat, self.reader)

    def test_the_creator_holds_owner_while_on_the_project(self):
        self.assertEqual(self.explicit(self.reader, self.note), ROLE_OWNER)
        self.assertEveryoneSays(self.note, True)

    def test_a_creator_taken_off_the_project_loses_the_note(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.reader).delete()
        self.assertIsNone(self.explicit(self.reader, self.note))
        self.assertNoAccess(self.note)

    def test_leaving_through_the_endpoint_ends_it_too(self):
        self.authenticate(self.reader)
        res = self.client.post(
            "/api/v2/project/leave/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "attendee_id": str(self.reader.id),
            },
            format="json",
        )
        self.assertIn(res.status_code, (200, 204), res.content)
        self.assertIsNone(self.explicit(self.reader, self.note))
        self.assertNoAccess(self.note)

    def test_the_author_no_longer_passes_the_briefing_timeline_and_mcp_after_leaving(self):
        """Flipped by MIP-1492 (it pinned the opposite): the note's `owner_id` was its author's
        own ACL for the briefing, the timeline and the search ACL behind MCP `get_note`; a chat
        note has no author exception now, so every reader refuses the author who left."""
        ProjectMembers.objects.filter(project=self.project, attendee=self.reader).delete()
        got = self.answers(self.note)
        self.assertEqual(got, {k: False for k in got}, got)

    def test_the_other_members_keep_the_note(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.reader).delete()
        self.assertEqual(self.explicit(self.owner, self.note), None)  # never had a row
        # the project's own people read it implicitly
        self.assertEqual(get_effective_role(self.owner.id, CHAT, self.note.note_id), ROLE_EDITOR)

    def test_a_note_in_another_project_is_untouched(self):
        other = self.project_of("OTH", self.reader)
        other_note = self.create_note_via_rest(self.pm_chat(other), self.reader, "other")
        ProjectMembers.objects.filter(project=self.project, attendee=self.reader).delete()
        self.assertEqual(self.explicit(self.reader, other_note), ROLE_OWNER)

    def test_a_share_to_someone_off_the_project_survives_their_not_being_on_it(self):
        self.share(self.note, self.owner_not_on_project(), ROLE_VIEWER, granter=self.reader)
        ProjectMembers.objects.filter(project=self.project, attendee=self.reader).delete()
        self.assertEqual(self.explicit(self.stranger, self.note), ROLE_VIEWER)

    def owner_not_on_project(self):
        from django.contrib.auth import get_user_model

        self.stranger = get_user_model().objects.create_user(
            username="stranger", email="stranger@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.stranger)
        return self.stranger


class LeavingTheTeam(Base):
    def test_removing_a_team_member_ends_every_chat_note_role_in_the_team(self):
        gm = self.owned_gm(self.reader)
        gm_note = self.create_note_via_rest(gm, self.owner, "gm")
        project = self.project_of("TEM", self.reader)
        pm_note = self.create_note_via_rest(self.pm_chat(project), self.reader, "pm")
        self.assertEqual(self.explicit(self.reader, gm_note), ROLE_EDITOR)
        self.assertEqual(self.explicit(self.reader, pm_note), ROLE_OWNER)

        remove_team_member(self.team.team_id, self.reader.id)

        self.assertIsNone(self.explicit(self.reader, gm_note))
        self.assertIsNone(self.explicit(self.reader, pm_note))
        for note in (gm_note, pm_note):
            self.assertIsNone(get_effective_role(self.reader.id, CHAT, note.note_id))
            self.assertEqual(readable_note_ids(self.reader.id, CHAT, [note.note_id]), set())
            self.assertEqual(self.rest_single(note).status_code, 403)

    def test_the_sidebar_stops_listing_the_notes(self):
        gm = self.owned_gm(self.reader)
        note = self.create_note_via_rest(gm, self.owner)
        self.assertIn(note.note_id, self.rest_meta_ids())
        remove_team_member(self.team.team_id, self.reader.id)
        # the person is off the team, so the sidebar itself may refuse: either way no note
        self.authenticate(self.reader)
        res = self.client.get(
            "/api/v2/note/chat/meta/",
            {"team_id": str(self.team.team_id), "user_id": str(self.reader.id)},
        )
        ids = {n["noteId"] for n in res.json()} if res.status_code == 200 else set()
        self.assertNotIn(note.note_id, ids)

    def test_another_teams_notes_are_untouched(self):
        from origin.models.common.team_models import TeamMaster

        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.owner
        )
        TeamMembers.objects.create(team=other_team, attendee=self.reader)
        note = ChatNoteMaster.objects.create(
            team=other_team,
            owner=self.owner,
            chat_type=ChannelKind.GM,
            channel=Channel.objects.create(team=other_team, kind=ChannelKind.GM, title="o"),
            is_thread=False,
            title="t",
            body=[],
        )
        NotePermissionMaster.objects.create(
            team=other_team,
            user=self.reader,
            note_type=CHAT,
            note_id=note.note_id,
            role_id=ROLE_EDITOR,
        )
        remove_team_member(self.team.team_id, self.reader.id)
        self.assertEqual(self.explicit(self.reader, note), ROLE_EDITOR)


class MovingANote(Base):
    def test_a_move_re_derives_the_rows_from_the_target_chat(self):
        old = self.owned_gm(self.reader)
        new = self.owned_gm()  # the reader is not in the new chat
        note = self.create_note_via_rest(old, self.owner)
        self.assertEqual(self.explicit(self.reader, note), ROLE_EDITOR)

        self.move(note, new)

        self.assertIsNone(self.explicit(self.reader, note))
        self.assertEveryoneSays(note, False)
        self.assertEqual(self.explicit(self.owner, note), ROLE_OWNER)

    def test_the_target_chats_members_read_it_through_their_chat(self):
        old = self.owned_gm()
        new = self.owned_gm(self.reader)
        note = self.create_note_via_rest(old, self.owner)
        self.move(note, new)
        self.assertEveryoneSays(note, True)
        self.assertEqual(get_effective_role(self.reader.id, CHAT, note.note_id), ROLE_EDITOR)
        # the author keeps the Owner row while they are in the chat the note now lives in
        self.assertEqual(self.explicit(self.owner, note), ROLE_OWNER)

    def test_a_share_survives_the_move(self):
        old = self.owned_gm()
        new = self.owned_gm()
        note = self.create_note_via_rest(old, self.owner)
        self.share(note, self.reader, ROLE_VIEWER)
        self.move(note, new)
        self.assertEqual(self.explicit(self.reader, note), ROLE_VIEWER)

    def test_a_share_to_someone_who_joined_the_old_chat_afterwards_survives_the_move(self):
        """The row predates their joining the old chat, so it is a share and not a membership
        row, even though they were a member of that chat when the note moved."""
        old = self.owned_gm()
        new = self.owned_gm()
        note = self.create_note_via_rest(old, self.owner)
        self.share(note, self.reader, ROLE_VIEWER)
        ChannelMember.objects.create(channel=old, user=self.reader)
        self.move(note, new)
        self.assertEqual(self.explicit(self.reader, note), ROLE_VIEWER)

    def test_the_owner_row_a_move_keeps_ends_when_the_owner_leaves_the_new_chat(self):
        """The author joined the new chat AFTER the note was written, so the kept row has to be
        re-dated from the move: left as it was it would read as a share and outlive their leaving."""
        old = self.owned_gm()
        note = self.create_note_via_rest(old, self.owner)
        new = self.owned_gm(self.reader)  # created after the note: the owner joins it later
        new_owner_row = ChannelMember.objects.get(channel=new, user=self.owner)
        self.move(note, new)
        self.assertEqual(self.explicit(self.owner, note), ROLE_OWNER)
        new_owner_row.is_deleted = True
        new_owner_row.save()
        from origin.services.chat_note_access import revoke_membership_roles

        revoke_membership_roles(
            user_id=self.owner.id, channel_id=new.id, joined_at=new_owner_row.ts_joined_at
        )
        self.assertIsNone(self.explicit(self.owner, note))

    def test_a_member_of_the_old_chat_who_is_in_the_new_one_too_has_no_explicit_row_either(self):
        old = self.owned_gm(self.reader)
        new = self.owned_gm(self.reader)
        note = self.create_note_via_rest(old, self.owner)
        self.move(note, new)
        self.assertIsNone(self.explicit(self.reader, note))
        self.assertEveryoneSays(note, True)  # through the new chat

    def test_a_move_out_of_a_project_chat_drops_the_creator_who_is_not_in_the_target(self):
        project = self.project_of("MOV", self.reader)
        note = self.create_note_via_rest(self.pm_chat(project), self.reader)
        new = self.owned_gm()
        self.authenticate(self.owner)
        # the mover must be in the target and able to write the note: the project's own member
        self.move(note, new, mover=self.owner)
        self.assertIsNone(self.explicit(self.reader, note))

    def test_children_move_with_their_parent(self):
        old = self.owned_gm(self.reader)
        new = self.owned_gm()
        parent = self.create_note_via_rest(old, self.owner, "parent")
        child = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.owner,
            chat_type=ChannelKind.GM,
            channel=old,
            is_thread=False,
            title="child",
            body=[],
            parent_note_id=parent.note_id,
        )
        for uid, role in ((self.owner, ROLE_OWNER), (self.reader, ROLE_EDITOR)):
            NotePermissionMaster.objects.create(
                team=self.team, user=uid, note_type=CHAT, note_id=child.note_id, role_id=role
            )
        self.move(parent, new)
        self.assertIsNone(self.explicit(self.reader, child))
        self.assertIsNone(self.explicit(self.reader, parent))

    def move(self, note, channel, mover=None):
        self.authenticate(mover or self.owner)
        res = self.client.put(
            "/api/v2/note/chat/move/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str((mover or self.owner).id),
                "note_id": note.note_id,
                "chat_type": int(channel.kind),
                "channel_id": str(channel.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        timezone.now()


class DeletingAProject(Base):
    """MIP-1500: `delete_project` detaches the project chat (`project=None`, `is_deleted=True`)
    BEFORE it deletes the project, so by the time the `ProjectMembers` `post_delete` receiver runs
    the chat can no longer be found from the project, and the explicit Owner/Editor rows on the
    chat's notes were never revoked: the former members kept Owner on the notes of a chat nobody
    can open any more."""

    def setUp(self):
        super().setUp()
        self.project = self.project_of("DEL", self.reader)
        self.chat = self.pm_chat(self.project)
        self.reader_note = self.create_note_via_rest(self.chat, self.reader, "by reader")
        self.owner_note = self.create_note_via_rest(self.chat, self.owner, "by owner")
        self.child = self.create_note_via_rest(self.chat, self.reader, "second by reader")

    def delete_via_page(self):
        """The page's endpoint, by the project's owner."""
        self.authenticate(self.owner)
        res = self.client.delete(
            "/api/v2/project/",
            {"team_id": str(self.team.team_id), "project_id": self.project.project_id},
            QUERY_STRING=f"team_id={self.team.team_id}&project_id={self.project.project_id}",
        )
        self.assertEqual(res.status_code, 204, res.content)

    def outsider(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=user)
        return user

    def test_the_rows_exist_before_the_delete(self):
        self.assertEqual(self.explicit(self.reader, self.reader_note), ROLE_OWNER)
        self.assertEqual(self.explicit(self.owner, self.owner_note), ROLE_OWNER)

    def test_the_service_revokes_every_former_member(self):
        from origin.services.project_delete import delete_project

        delete_project(self.project)
        for user, note in (
            (self.reader, self.reader_note),
            (self.reader, self.child),
            (self.owner, self.owner_note),
        ):
            self.assertIsNone(self.explicit(user, note), (user.username, note.title))
            self.assertIsNone(get_effective_role(user.id, CHAT, note.note_id))
        self.assertFalse(NotePermissionMaster.objects.filter(note_type=CHAT).exists())

    def test_the_page_endpoint_does_the_same_and_the_rest_readers_say_no(self):
        self.delete_via_page()
        for note in (self.reader_note, self.child, self.owner_note):
            self.assertTrue(ChatNoteMaster.objects.filter(note_id=note.note_id).exists())
            self.assertIsNone(self.explicit(self.reader, note))
            self.assertNoAccess(note)
            self.assertEqual(self.rest_single(note).status_code, 403)
            self.assertEqual(readable_note_ids(self.reader.id, CHAT, [note.note_id]), set())
            self.assertNotIn(note.note_id, self.rest_meta_ids())

    def test_a_share_the_owner_made_on_purpose_to_a_non_member_survives(self):
        stranger = self.outsider()
        self.share(self.reader_note, stranger, ROLE_VIEWER, granter=self.reader)
        self.delete_via_page()
        self.assertEqual(self.explicit(stranger, self.reader_note), ROLE_VIEWER)
        self.assertEqual(
            get_effective_role(stranger.id, CHAT, self.reader_note.note_id), ROLE_VIEWER
        )
        # ... and the members still lost theirs
        self.assertIsNone(self.explicit(self.reader, self.reader_note))
        self.assertIsNone(self.explicit(self.owner, self.owner_note))

    def test_a_share_to_someone_who_joined_the_project_afterwards_is_not_a_member_row(self):
        """The two-kind rule of MIP-1482: written before the person joined the chat = a share."""
        stranger = self.outsider()
        self.share(self.reader_note, stranger, ROLE_EDITOR, granter=self.reader)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=stranger)
        self.delete_via_page()
        self.assertEqual(self.explicit(stranger, self.reader_note), ROLE_EDITOR)

    def test_a_sibling_project_is_untouched(self):
        other = self.project_of("SIB", self.reader)
        other_note = self.create_note_via_rest(self.pm_chat(other), self.reader, "sibling")
        self.delete_via_page()
        self.assertEqual(self.explicit(self.reader, other_note), ROLE_OWNER)
        self.assertEveryoneSays(other_note, True)

    def test_the_search_index_is_queued_to_drop_the_notes_for_the_old_members(self):
        from unittest import mock

        with mock.patch("origin.search_engine.acl_dirty.mark_channels_dirty") as mark:
            self.delete_via_page()
        marked = {str(c) for call in mark.call_args_list for c in call.args[0]}
        self.assertIn(str(self.chat.id), marked)

    def test_a_failed_delete_revokes_nothing(self):
        """One transaction: if the project delete fails the rows are still there."""
        from unittest import mock

        from origin.services.project_delete import delete_project

        with mock.patch.object(type(self.project), "delete", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                delete_project(self.project)
        self.assertEqual(self.explicit(self.reader, self.reader_note), ROLE_OWNER)
        self.assertFalse(Channel.objects.get(id=self.chat.id).is_deleted)
