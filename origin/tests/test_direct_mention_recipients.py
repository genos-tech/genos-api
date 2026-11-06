"""A direct @user mention reaches only people who belong to the team (MIP-1061).

MIP-1030 made a GROUP mention notify only the people on the team now. A direct mention had
no such check: the `userId` in a body is whatever the client wrote, and each one becomes a
feed row and a web push carrying the comment text, so a hand-written id named a stranger.

Now the directly mentioned people are filtered the way the group's members are, with two
deliberate differences: a guest (an external collaborator with a project row and no member
row) stays mentionable, and a team member who is not in the project, channel or folder still
gets the mention, which is how they find out the thing exists.
"""

from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
)
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.body_mentions import (
    notify_note_mentions,
    notify_task_body_mentions,
)
from origin.services.milestone_service import ensure_backing_task
from origin.tests.test_mention_group_recipients import TaskFixture
from origin.views.utils.mention_handler import people_of_team, resolve_group_members
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_OWNER

User = get_user_model()

COMMENT = "/api/v2/task/comment/"
TASK = "/api/v2/task/"
SURFACE = "/api/v3/activities/surface/"


def mention_body(*users):
    return [
        {
            "type": "paragraph",
            "content": [
                {"type": "mention", "props": {"userId": str(u.id), "userName": "x"}} for u in users
            ],
        }
    ]


class People(TaskFixture):
    """`self.member` and `self.user2` are on the team (neither is in the project), `self.guest`
    is an external collaborator, `self.outsider` is on no team, `self.ex_member` left."""

    def setUp(self):
        super().setUp()
        self.guest = User.objects.create_user(username="g", email="g@e.com", password="pass12345")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.guest)
        self.everyone = (self.user2, self.member, self.guest, self.outsider, self.ex_member)
        self.reached = {str(self.user2.id), str(self.member.id), str(self.guest.id)}

    def body(self):
        return mention_body(*self.everyone)


class PeopleOfTeamTests(People):
    def test_members_are_kept_and_strangers_dropped(self):
        got = people_of_team([u.id for u in self.everyone], team_id=self.team.team_id)
        self.assertEqual(got, {str(self.user2.id), str(self.member.id)})

    def test_guests_only_when_asked(self):
        ids = [u.id for u in self.everyone]
        got = people_of_team(ids, team_id=self.team.team_id, include_guests=True)
        self.assertEqual(got, self.reached)

    def test_the_owner_counts_even_without_a_member_row(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user).delete()
        got = people_of_team([self.user.id], team_id=self.team.team_id)
        self.assertEqual(got, {str(self.user.id)})

    def test_a_group_member_who_is_the_owner_without_a_member_row_is_still_reached(self):
        """MIP-1030's resolver checked `TeamMembers` only; old data has owners with no row."""
        from origin.models.common.mention_group_models import MentionGroupMembers

        MentionGroupMembers.objects.create(team=self.team, group=self.group, user=self.user)
        TeamMembers.objects.filter(team=self.team, attendee=self.user).delete()
        got = resolve_group_members({str(self.group.group_id)}, team_id=self.team.team_id)
        self.assertIn(str(self.user.id), got)

    def test_malformed_ids_drop_out_and_spellings_are_canonical(self):
        upper = str(self.member.id).upper()
        got = people_of_team(
            ["abc", "", None, 5, ["x"], {"a": 1}, upper], team_id=self.team.team_id
        )
        self.assertEqual(got, {str(self.member.id)})
        self.assertEqual(people_of_team([self.member.id], team_id="abc"), set())
        self.assertEqual(people_of_team([], team_id=self.team.team_id), set())
        self.assertEqual(people_of_team(None, team_id=self.team.team_id), set())


class CommentTests(People):
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
        return {str(a.recipient_id) for call in push.call_args_list for a in call.args[0]}

    def test_a_direct_mention_notifies_people_of_the_team_and_nobody_else(self):
        pushed = self.comment(self.body())
        self.assertEqual(self.mention_recipients(), self.reached)
        for stranger in (self.outsider, self.ex_member):
            self.assertFalse(Activity.objects.filter(recipient=stranger).exists())
            self.assertNotIn(str(stranger.id), pushed)

    def test_a_team_member_who_is_not_in_the_project_still_gets_the_mention(self):
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.member).exists()
        )
        self.comment(mention_body(self.member))
        self.assertEqual(self.mention_recipients(), {str(self.member.id)})

    def test_someone_removed_from_the_team_stops_being_pinged(self):
        self.comment(mention_body(self.member))
        self.assertIn(str(self.member.id), self.mention_recipients())
        Activity.objects.all().delete()
        TeamMembers.objects.filter(team=self.team, attendee=self.member).delete()
        self.comment(mention_body(self.member))
        self.assertEqual(self.mention_recipients(), set())


class SaveTests(People):
    def stored(self, model, **pk):
        return set(model.objects.get(**pk).mentioned_user_ids or [])

    def test_task_update_and_create_store_only_people_of_the_team(self):
        res = self.client.put(
            TASK, {"task_id": self.task.task_id, "content": self.body()}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.stored(TaskMaster, task_id=self.task.task_id), self.reached)
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
        self.assertEqual(self.stored(TaskMaster, title="New"), self.reached)

    def put_note(self, url, note):
        res = self.client.put(
            url, {"note_id": note.note_id, "title": "n", "body": self.body()}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        note.refresh_from_db()
        return set(note.mentioned_user_ids or [])

    def test_the_three_note_saves_store_only_people_of_the_team(self):
        personal = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=personal.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_OWNER,
        )
        self.assertEqual(self.put_note("/api/v2/note/personal/", personal), self.reached)
        task_note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            owner=self.user,
            title="n",
            body=[],
        )
        self.assertEqual(self.put_note("/api/v2/note/task/", task_note), self.reached)
        gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="gm", owner=self.user
        )
        ChannelMember.objects.create(channel=gm, user=self.user, role="owner")
        chat_note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=int(ChannelKind.GM),
            channel=gm,
            is_thread=False,
            title="n",
            body=[],
        )
        self.assertEqual(self.put_note("/api/v2/note/chat/", chat_note), self.reached)

    def test_a_milestone_description_stores_only_people_of_the_team(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="Launch", reporter=self.user
        )
        backing = ensure_backing_task(milestone)
        res = self.client.patch(
            f"/api/v2/milestone/{milestone.milestone_id}/",
            {"description": self.body()},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        backing.refresh_from_db()
        self.assertEqual(set(backing.mentioned_user_ids or []), self.reached)

    def test_the_agents_body_paths_store_only_people_of_the_team(self):
        self.task.content = self.body()
        self.task.save()
        notify_task_body_mentions(self.task, actor_id=str(self.user.id))
        self.task.refresh_from_db()
        self.assertEqual(set(self.task.mentioned_user_ids or []), self.reached)
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n", body=self.body()
        )
        notify_note_mentions(note, note_type=1, actor_id=str(self.user.id))
        note.refresh_from_db()
        self.assertEqual(set(note.mentioned_user_ids or []), self.reached)


class SocketsPathStaysSafeTests(People):
    """genos-sockets sends the recipient ids it was handed to this endpoint, whose producer
    already keeps only current team members. Pinned here so it stays that way."""

    def test_the_surface_endpoint_drops_strangers(self):
        res = self.client.post(
            SURFACE,
            {
                "surface_type": 5,
                "team_id": self.team_id,
                "entity_key": f"task:{self.task.task_id}",
                "newly_mentioned_user_ids": [str(u.id) for u in self.everyone],
                "meta": {"taskId": self.task.task_id},
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        got = {
            str(a.recipient_id) for a in Activity.objects.filter(activity_type=ActivityType.MENTION)
        }
        self.assertEqual(got, {str(self.user2.id), str(self.member.id)})
