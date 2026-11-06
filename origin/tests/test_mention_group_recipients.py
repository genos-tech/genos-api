"""An @group mention reaches the people who are on the team NOW (MIP-1030).

A mention group's members come back as notification recipients: each gets a feed
row and a web push carrying the comment text, or the task and project names. Two
things were never checked at send time:

* the group's members. `user_ids` had to be team members only from MIP-1018 on, so a
  user added before that, or one who has since left the team, stayed in the group and
  kept being told about the team's work;
* the group's team. `resolve_group_members` took any group id, so a body written in one
  team could name another team's group (ids are small sequential numbers) and notify its
  members, with text the author chose.

Both are now resolved when the mention is written: only the object's team's live groups,
and only the people who are members of that team at that moment.
"""

from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools.body_mentions import (
    notify_note_mentions,
    notify_task_body_mentions,
)
from origin.services.group_expansion import GROUP_MENTION, resolve_group_user_ids
from origin.services.milestone_service import ensure_backing_task
from origin.services.task_comment_manage import edit_comment
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.mention_handler import resolve_group_members
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_OWNER

User = get_user_model()

COMMENT = "/api/v2/task/comment/"
TASK = "/api/v2/task/"
RESOLVE = "/api/v2/mention-group/resolve/"


def group_body(*group_ids):
    return [
        {
            "type": "paragraph",
            "content": [
                {"type": "mentionGroup", "props": {"groupId": str(g), "groupName": "g"}}
                for g in group_ids
            ],
        }
    ]


class Base(BaseAPITestCase):
    """Team A holds the work. Its group G has five rows: two people who are on the team,
    one who never was (added before MIP-1018), one who left, and the caller's colleague."""

    def setUp(self):
        super().setUp()
        self.member = User.objects.create_user(username="m", email="m@e.com", password="pass12345")
        self.outsider = User.objects.create_user(
            username="o", email="o@e.com", password="pass12345"
        )
        self.ex_member = User.objects.create_user(
            username="x", email="x@e.com", password="pass12345"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.member)
        TeamMembers.objects.create(team=self.team, attendee=self.ex_member, is_deleted=True)

        self.group = MentionGroupMaster.objects.create(
            team=self.team, group_name="design", created_by=self.user
        )
        for u in (self.user2, self.member, self.outsider, self.ex_member):
            MentionGroupMembers.objects.create(team=self.team, group=self.group, user=u)

        # Team B, with a group of its own that the caller (self.user) is also in.
        self.team_b = TeamMaster.objects.create(
            team_name="B", team_email="b@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=self.team_b, attendee=self.user2)
        TeamMembers.objects.create(team=self.team_b, attendee=self.user)
        self.b_only = User.objects.create_user(username="b", email="b@e.com", password="pass12345")
        TeamMembers.objects.create(team=self.team_b, attendee=self.b_only)
        self.group_b = MentionGroupMaster.objects.create(
            team=self.team_b, group_name="b-design", created_by=self.user2
        )
        for u in (self.b_only, self.user2):
            MentionGroupMembers.objects.create(team=self.team_b, group=self.group_b, user=u)

        self.on_team = {str(self.user2.id), str(self.member.id)}
        self.team_id = str(self.team.team_id)

    def ids(self, *users):
        return {str(u.id) for u in users}


class ResolverTests(Base):
    def test_a_group_expands_to_the_members_who_are_on_the_team(self):
        got = resolve_group_members({str(self.group.group_id)}, team_id=self.team.team_id)
        self.assertEqual(got, self.on_team)

    def test_removing_someone_from_the_team_stops_them_receiving_group_mentions(self):
        ids = {str(self.group.group_id)}
        self.assertIn(str(self.member.id), resolve_group_members(ids, team_id=self.team.team_id))
        TeamMembers.objects.filter(team=self.team, attendee=self.member).update(is_deleted=True)
        self.assertNotIn(str(self.member.id), resolve_group_members(ids, team_id=self.team.team_id))
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).delete()
        self.assertEqual(resolve_group_members(ids, team_id=self.team.team_id), set())

    def test_another_teams_group_resolves_to_nothing(self):
        got = resolve_group_members({str(self.group_b.group_id)}, team_id=self.team.team_id)
        self.assertEqual(got, set())
        # and it is still its own team's group
        got = resolve_group_members({str(self.group_b.group_id)}, team_id=self.team_b.team_id)
        self.assertEqual(got, self.ids(self.b_only, self.user2))

    def test_a_malformed_group_id_or_team_names_nothing_and_does_not_raise(self):
        for bad in ("abc", "", "1.5", "1; drop"):
            with self.subTest(group_id=bad):
                self.assertEqual(
                    resolve_group_members(
                        {bad, str(self.group.group_id)}, team_id=self.team.team_id
                    ),
                    self.on_team,
                )
        for team in ("abc", "", None, 5):
            with self.subTest(team_id=team):
                self.assertEqual(
                    resolve_group_members({str(self.group.group_id)}, team_id=team), set()
                )

    def test_the_team_is_required(self):
        with self.assertRaises(TypeError):
            resolve_group_members({str(self.group.group_id)})  # noqa

    def test_a_folder_invite_snapshot_of_a_group_is_current_members_too(self):
        got = resolve_group_user_ids(self.team.team_id, GROUP_MENTION, self.group.group_id)
        self.assertEqual(got, self.on_team)


class ResolveEndpointTests(Base):
    """The sockets service resolves a group through this, with the caller's own JWT."""

    def setUp(self):
        super().setUp()
        self.authenticate(self.user)  # a member of BOTH teams

    def resolve(self, ids, **extra):
        res = self.client.post(RESOLVE, {"group_ids": ids, **extra}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        return {k: set(v) for k, v in res.json()["resolved"].items()}

    def test_members_come_back_only_if_they_are_on_the_groups_team(self):
        got = self.resolve([self.group.group_id])
        self.assertEqual(got, {str(self.group.group_id): self.on_team})

    def test_a_team_narrows_the_groups_to_the_one_the_mention_was_written_in(self):
        both = [self.group.group_id, self.group_b.group_id]
        wide = self.resolve(both)
        self.assertEqual(wide[str(self.group_b.group_id)], self.ids(self.b_only, self.user2))
        narrow = self.resolve(both, team_id=self.team_id)
        self.assertEqual(narrow[str(self.group.group_id)], self.on_team)
        self.assertEqual(narrow[str(self.group_b.group_id)], set())

    def test_a_team_that_is_not_a_uuid_resolves_nothing_and_a_malformed_group_is_no_500(self):
        got = self.resolve([self.group.group_id], team_id="abc")
        self.assertEqual(got[str(self.group.group_id)], set())
        got = self.resolve(["abc", self.group.group_id, {"a": 1}, [1]])
        self.assertEqual(got[str(self.group.group_id)], self.on_team)


class TaskFixture(Base):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code="WEB",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Task",
            status="WIP",
        )
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )
        self.authenticate(self.user)

    def comment(self, body):
        with mock.patch("origin.services.task_comment_fanout.schedule_push_for_activities") as push:
            res = self.client.post(
                COMMENT,
                {
                    "task_id": str(self.task.task_id),
                    "sender_id": str(self.user.id),
                    "comment_body": body,
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.content)
        pushed = {str(a.recipient_id) for call in push.call_args_list for a in call.args[0]}
        return pushed

    def mention_recipients(self):
        return {
            str(a.recipient_id) for a in Activity.objects.filter(activity_type=ActivityType.MENTION)
        }


class TaskCommentTests(TaskFixture):
    def test_a_group_mention_notifies_the_members_on_the_team_and_nobody_else(self):
        pushed = self.comment(group_body(self.group.group_id))
        self.assertEqual(self.mention_recipients(), self.on_team)
        self.assertTrue(self.on_team >= pushed & self.on_team)
        for stranger in (self.outsider, self.ex_member):
            self.assertNotIn(str(stranger.id), pushed)
            self.assertFalse(Activity.objects.filter(recipient=stranger).exists())

    def test_naming_another_teams_group_notifies_nobody_there(self):
        pushed = self.comment(group_body(self.group_b.group_id))
        self.assertEqual(self.mention_recipients(), set())
        self.assertFalse(Activity.objects.filter(recipient=self.b_only).exists())
        self.assertNotIn(str(self.b_only.id), pushed)

    def test_someone_removed_from_the_team_is_no_longer_pinged_by_the_group(self):
        self.comment(group_body(self.group.group_id))
        self.assertIn(str(self.member.id), self.mention_recipients())
        Activity.objects.all().delete()
        TeamMembers.objects.filter(team=self.team, attendee=self.member).delete()
        self.comment(group_body(self.group.group_id))
        self.assertNotIn(str(self.member.id), self.mention_recipients())
        self.assertEqual(self.mention_recipients(), {str(self.user2.id)})

    def test_an_edit_that_adds_a_group_mentions_the_same_people(self):
        self.comment([{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}])
        comment = TaskComments.objects.get(task=self.task)
        with mock.patch("origin.services.task_comment_manage.schedule_push_for_activities"):
            added = edit_comment(comment, group_body(self.group.group_id))
        self.assertEqual(set(added), self.on_team)


class BodySaveTests(TaskFixture):
    def body(self, text="x"):
        return group_body(self.group.group_id)

    def stored(self):
        return set(TaskMaster.objects.get(task_id=self.task.task_id).mentioned_user_ids or [])

    def test_a_task_update_stores_only_members_who_are_on_the_team(self):
        res = self.client.put(
            TASK, {"task_id": self.task.task_id, "content": self.body()}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.stored(), self.on_team)

    def test_a_task_create_stores_only_members_who_are_on_the_team(self):
        res = self.client.post(
            TASK,
            {
                "project": self.project.project_id,
                "assignee": str(self.user.id),
                "reporter": str(self.user.id),
                "title": "New",
                "priority": "Medium",
                "effort_level": "S",
                "status": "Open",
                "content": self.body(),
                "due_date": None,
                "links": [],
                "tags": [],
                "is_init_task": False,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        created = TaskMaster.objects.get(title="New")
        self.assertEqual(set(created.mentioned_user_ids or []), self.on_team)


class NoteSaveTests(TaskFixture):
    """The three note PUTs persist the mention set their body expands to."""

    def put(self, url, note):
        res = self.client.put(
            url,
            {"note_id": note.note_id, "title": "n", "body": group_body(self.group.group_id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        note.refresh_from_db()
        return set(note.mentioned_user_ids or [])

    def test_a_personal_note_save_stores_only_members_who_are_on_the_team(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_OWNER,
        )
        self.assertEqual(self.put("/api/v2/note/personal/", note), self.on_team)

    def test_a_task_note_save_stores_only_members_who_are_on_the_team(self):
        note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            owner=self.user,
            title="n",
            body=[],
        )
        self.assertEqual(self.put("/api/v2/note/task/", note), self.on_team)

    def test_a_chat_note_save_stores_only_members_who_are_on_the_team(self):
        gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="gm", owner=self.user
        )
        ChannelMember.objects.create(channel=gm, user=self.user, role="owner")
        note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=int(ChannelKind.GM),
            channel=gm,
            is_thread=False,
            title="n",
            body=[],
        )
        self.assertEqual(self.put("/api/v2/note/chat/", note), self.on_team)


class MilestoneAndAgentTests(TaskFixture):
    def test_a_milestone_description_stores_only_members_who_are_on_the_team(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="Launch", reporter=self.user
        )
        backing = ensure_backing_task(milestone)
        res = self.client.patch(
            f"/api/v2/milestone/{milestone.milestone_id}/",
            {"description": group_body(self.group.group_id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        backing.refresh_from_db()
        self.assertEqual(set(backing.mentioned_user_ids or []), self.on_team)

    def test_the_agents_task_body_path_notifies_only_members_who_are_on_the_team(self):
        self.task.content = group_body(self.group.group_id)
        self.task.save()
        notify_task_body_mentions(self.task, actor_id=str(self.user.id))
        self.task.refresh_from_db()
        self.assertEqual(set(self.task.mentioned_user_ids or []), self.on_team)

    def test_the_agents_note_path_notifies_only_members_who_are_on_the_team(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n", body=group_body(self.group.group_id)
        )
        notify_note_mentions(note, note_type=1, actor_id=str(self.user.id))
        note.refresh_from_db()
        self.assertEqual(set(note.mentioned_user_ids or []), self.on_team)
