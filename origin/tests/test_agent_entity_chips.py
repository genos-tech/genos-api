"""`@` mentions and `#` references in agent-written bodies (MIP-708).

Contract under test:

  * `entity_chips.resolve_chip` turns each explicit token into the node
    the editor's own menu inserts, prop for prop — and refuses (None)
    anything foreign-team, deleted, or not readable by the WRITER, since
    a chip copies the entity's name into a body others read.
  * The write tools store those chips: a task body, a comment, and both
    note families.
  * A chip survives a read-modify-write: `fetch_task.content_markdown` /
    `fetch_note.body_markdown` render it back to a token that re-parses
    into the same node.
  * An `@` chip in a task body or note files the same surface MENTION
    activity a typed one does, once — a re-save pings no one twice, and
    removing the mention deletes the row.
"""

import uuid
from unittest.mock import patch

from origin.models.chat.unified_models import Activity, Channel, ChannelKind, ChannelMember
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools.add_comment import _run as add_comment_run
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.create_note import _run as create_note_run
from origin.search_engine.agent.tools.create_task import _run as create_task_run
from origin.search_engine.agent.tools.entity_chips import chip_resolver, resolve_chip
from origin.search_engine.agent.tools.fetch_note import _run as fetch_note_run
from origin.search_engine.agent.tools.fetch_task import _run as fetch_task_run
from origin.search_engine.agent.tools.update_note import _run as update_note_run
from origin.search_engine.agent.tools.update_task import _run as update_task_run
from origin.search_engine.text_extraction import extract_text
from origin.services.milestone_service import ensure_backing_task
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_EDITOR, ROLE_OWNER

from .test_base import BaseAPITestCase


def _chips(body):
    """Every chip node in a body, in order (tables/children aside)."""
    out = []
    for block in body or []:
        content = block.get("content")
        if isinstance(content, list):
            out.extend(n for n in content if n.get("type") not in ("text", "link"))
    return out


class ChipTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Genos-Alpha",
            code="MIP",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Chips", content=[]
        )
        self.task.refresh_from_db()  # project_task_number comes from a signal
        self.tid = str(self.team.team_id)
        self.uid = str(self.user.id)
        self.ctx = ToolContext(team_id=self.tid, user_id=self.uid)

    def chip(self, token, user=None):
        return resolve_chip(token, team_id=self.tid, user_id=str((user or self.user).id))


class ResolveChipTests(ChipTestBase):
    def test_user_mention_takes_the_username_and_needs_team_membership(self):
        self.assertEqual(
            self.chip(f"user:{self.user2.id}"),
            {"type": "mention", "props": {"userId": str(self.user2.id), "userName": "otheruser"}},
        )
        outsider = CustomUser.objects.create_user(
            username="outsider", email="out@example.com", password="pw123456"
        )
        self.assertIsNone(self.chip(f"user:{outsider.id}"))
        TeamMembers.objects.filter(attendee=self.user2).update(is_deleted=True)
        self.assertIsNone(
            self.chip(f"user:{self.user2.id}"), "a departed member is not mentionable"
        )

    def test_group_mention_counts_members_and_is_team_scoped(self):
        group = MentionGroupMaster.objects.create(team=self.team, group_name="design")
        for u in (self.user, self.user2):
            MentionGroupMembers.objects.create(team=self.team, group=group, user=u)
        self.assertEqual(
            self.chip(f"group:{group.group_id}"),
            {
                "type": "mentionGroup",
                "props": {
                    "groupId": str(group.group_id),
                    "groupName": "design",
                    "memberCount": "2",
                },
            },
        )
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        foreign = MentionGroupMaster.objects.create(team=other_team, group_name="spies")
        self.assertIsNone(self.chip(f"group:{foreign.group_id}"))

    def test_task_by_id_and_by_display_id_are_the_same_chip(self):
        expected = {
            "type": "hashTask",
            "props": {
                "projectId": str(self.project.project_id),
                "taskId": str(self.task.task_id),
                "displayId": self.task.display_id,
                "title": "Chips",
            },
        }
        self.assertEqual(self.chip(f"task:{self.task.task_id}"), expected)
        n = self.task.project_task_number
        self.assertEqual(self.chip(f"display:MIP-{n}"), expected)
        self.assertEqual(self.chip(f"display:mip-{n}"), expected, "codes match case-insensitively")

    def test_task_the_writer_cannot_read_is_refused(self):
        # user2 is on the team but not in the project.
        self.assertIsNone(self.chip(f"task:{self.task.task_id}", user=self.user2))
        self.assertIsNone(
            self.chip(f"display:MIP-{self.task.project_task_number}", user=self.user2)
        )

    def test_deleted_and_foreign_team_tasks_are_refused(self):
        TaskMaster.objects.filter(task_id=self.task.task_id).update(is_deleted=True)
        self.assertIsNone(self.chip(f"task:{self.task.task_id}"))
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user
        )
        foreign_project = ProjectMaster.objects.create(
            team=other_team,
            project_name="Foreign",
            code="FOR",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=other_team, project=foreign_project, attendee=self.user)
        foreign = TaskMaster.objects.create(
            team=other_team, project=foreign_project, reporter=self.user, title="secret"
        )
        self.assertIsNone(self.chip(f"task:{foreign.task_id}"))

    def test_project_chip_needs_membership(self):
        self.assertEqual(
            self.chip(f"project:{self.project.project_id}"),
            {
                "type": "hashProject",
                "props": {"projectId": str(self.project.project_id), "projectName": "Genos-Alpha"},
            },
        )
        self.assertIsNone(self.chip(f"project:{self.project.project_id}", user=self.user2))

    def test_personal_note_kind_follows_owner_and_folder(self):
        mine = PersonalNoteMaster.objects.create(team=self.team, owner=self.user, title="Spec")
        self.assertEqual(self.chip(f"note:personal:{mine.note_id}")["props"]["noteKind"], "my")
        # user2 can't read an unshared personal note...
        self.assertIsNone(self.chip(f"note:personal:{mine.note_id}", user=self.user2))
        # ...and once granted, sees it as "shared".
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=mine.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_EDITOR,
        )
        self.assertEqual(
            self.chip(f"note:personal:{mine.note_id}", user=self.user2)["props"]["noteKind"],
            "shared",
        )
        folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name="Team", scope=PersonalNoteFolder.SCOPE_TEAM
        )
        team_note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Handbook", folder_id=folder.folder_id
        )
        self.assertEqual(
            self.chip(f"note:personal:{team_note.note_id}")["props"],
            {
                "noteKind": "team",
                "noteId": str(team_note.note_id),
                "title": "Handbook",
                "projectId": "",
                "taskId": "",
                "chatType": "",
                "chatId": "",
                "threadId": "0",
            },
        )

    def test_public_team_folder_note_is_chippable_by_any_member(self):
        # Reached through the `team:<id>` sentinel, not a per-user grant.
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Team",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Handbook", folder_id=folder.folder_id
        )
        chip = self.chip(f"note:personal:{note.note_id}", user=self.user2)
        self.assertEqual(chip["props"]["noteKind"], "team")
        PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).update(
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE
        )
        self.assertIsNone(self.chip(f"note:personal:{note.note_id}", user=self.user2))

    def test_milestone_chip_points_at_its_backing_task(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="Launch", reporter=self.user
        )
        backing = ensure_backing_task(milestone)
        milestone.refresh_from_db()
        self.assertEqual(
            self.chip(f"milestone:{milestone.milestone_id}"),
            {
                "type": "hashTask",
                "props": {
                    "projectId": str(self.project.project_id),
                    "taskId": str(backing.task_id),
                    "displayId": backing.display_id,
                    "title": "Launch",
                },
            },
        )
        self.assertIsNone(self.chip(f"milestone:{milestone.milestone_id}", user=self.user2))
        MilestoneMaster.objects.filter(milestone_id=milestone.milestone_id).update(is_deleted=True)
        self.assertIsNone(self.chip(f"milestone:{milestone.milestone_id}"))

    def test_task_note_chip_carries_its_parents(self):
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=self.task, owner=self.user, title="Plan"
        )
        props = self.chip(f"note:task:{note.note_id}")["props"]
        self.assertEqual(
            (props["noteKind"], props["projectId"], props["taskId"]),
            ("task", str(self.project.project_id), str(self.task.task_id)),
        )
        self.assertIsNone(self.chip(f"note:task:{note.note_id}", user=self.user2))

    def test_gm_chat_chip_needs_membership(self):
        channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="eng", owner=self.user
        )
        ChannelMember.objects.create(channel=channel, user=self.user, role="member")
        self.assertEqual(
            self.chip(f"chat:gm:{channel.id}"),
            {"type": "hashChat", "props": {"chatId": str(channel.id), "chatName": "eng"}},
        )
        self.assertIsNone(self.chip(f"chat:gm:{channel.id}", user=self.user2))

    def test_the_writers_other_team_never_leaks_in(self):
        # The writer belongs to both teams and can read all of this —
        # but from inside self.team, none of it is chippable.
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user
        )
        stranger = CustomUser.objects.create_user(
            username="stranger", email="s@example.com", password="pw123456"
        )
        TeamMembers.objects.create(team=other_team, attendee=stranger)
        TeamMembers.objects.create(team=other_team, attendee=self.user)
        note = PersonalNoteMaster.objects.create(team=other_team, owner=self.user, title="Theirs")
        channel = Channel.objects.create(
            team=other_team, kind=ChannelKind.GM, title="side", owner=self.user
        )
        ChannelMember.objects.create(channel=channel, user=self.user, role="member")
        for token in (
            f"user:{stranger.id}",
            f"note:personal:{note.note_id}",
            f"chat:gm:{channel.id}",
        ):
            self.assertIsNone(self.chip(token), token)
        # Sanity: each IS chippable from the team it lives in.
        for token in (
            f"user:{stranger.id}",
            f"note:personal:{note.note_id}",
            f"chat:gm:{channel.id}",
        ):
            self.assertIsNotNone(
                resolve_chip(token, team_id=str(other_team.team_id), user_id=self.uid), token
            )

    def test_unknown_and_malformed_tokens_are_refused(self):
        for token in (
            "",
            "task:",
            "task:abc",
            "task:0",
            "user:not-a-uuid",
            "todo:1",
            f"chat:gm:{uuid.uuid4()}",
            "display:MIP-99999",
            "note:chat:1",
        ):
            self.assertIsNone(self.chip(token), token)


class ChipRoundTripTests(ChipTestBase):
    def test_render_then_reparse_is_the_same_body(self):
        note = PersonalNoteMaster.objects.create(team=self.team, owner=self.user, title="Spec")
        resolver = chip_resolver(team_id=self.tid, user_id=self.uid)
        body = markdown_to_blocks(
            f"Hi [@x](user:{self.user2.id}), see #MIP-{self.task.project_task_number}, "
            f"[#whatever](project:{self.project.project_id}) and [note:personal:{note.note_id}].",
            entity_chip_resolver=resolver,
        )
        self.assertEqual(
            [c["type"] for c in _chips(body)], ["mention", "hashTask", "hashProject", "hashNote"]
        )
        rendered = blocks_to_markdown(body)
        self.assertIn(f"[@otheruser](user:{self.user2.id})", rendered)
        self.assertIn(f"[#{self.task.display_id} · Chips](task:{self.task.task_id})", rendered)
        self.assertEqual(markdown_to_blocks(rendered, entity_chip_resolver=resolver), body)

    def test_chip_labels_reach_the_search_text(self):
        body = markdown_to_blocks(
            f"see #MIP-{self.task.project_task_number}",
            entity_chip_resolver=chip_resolver(team_id=self.tid, user_id=self.uid),
        )
        self.assertIn(f"#{self.task.display_id} · Chips", extract_text(body))

    def test_a_citation_link_without_a_trigger_stays_a_link(self):
        body = markdown_to_blocks(
            f"[the plan](task:{self.task.task_id})",
            entity_chip_resolver=chip_resolver(team_id=self.tid, user_id=self.uid),
        )
        self.assertEqual(_chips(body), [])


@patch("origin.search_engine.agent.tools.update_task.push_task_body")
class TaskBodyChipTests(ChipTestBase):
    def _mention_rows(self, surface_type=5):
        return list(
            Activity.objects.filter(surface_type=surface_type).values_list(
                "recipient_id", flat=True
            )
        )

    def test_update_task_writes_chips_and_files_one_mention(self, _push):
        with self.captureOnCommitCallbacks(execute=True):
            update_task_run(
                {"task_id": self.task.task_id, "content_text": f"ping [@x](user:{self.user2.id})"},
                self.ctx,
            )
        self.task.refresh_from_db()
        self.assertEqual(_chips(self.task.content)[0]["type"], "mention")
        self.assertEqual(self.task.mentioned_user_ids, [str(self.user2.id)])
        self.assertEqual([str(r) for r in self._mention_rows()], [str(self.user2.id)])
        row = Activity.objects.get(surface_type=5)
        self.assertEqual(row.meta["taskId"], self.task.task_id)
        self.assertEqual(row.meta["displayId"], self.task.display_id)
        self.assertEqual(row.meta["firstLineContent"], "Mentioned in the task body: Chips")
        self.assertEqual(str(row.actor_id), self.uid)

        # Re-saving a body that still mentions them pings no one again.
        with patch("origin.services.v3_activity.create_surface_mention_activities") as producer:
            update_task_run(
                {
                    "task_id": self.task.task_id,
                    "content_text": f"still [@x](user:{self.user2.id}) here",
                },
                self.ctx,
            )
        producer.assert_not_called()

        # Removing the mention removes their row.
        update_task_run({"task_id": self.task.task_id, "content_text": "nobody"}, self.ctx)
        self.assertEqual(self._mention_rows(), [])
        self.task.refresh_from_db()
        self.assertEqual(self.task.mentioned_user_ids, [])

    def test_create_task_files_the_mention(self, _push):
        result = create_task_run(
            {
                "project_id": self.project.project_id,
                "title": "New",
                "content_text": f"for [@x](user:{self.user2.id})",
            },
            self.ctx,
        )
        task = TaskMaster.objects.get(task_id=result["task_id"])
        self.assertEqual(task.mentioned_user_ids, [str(self.user2.id)])
        self.assertEqual([str(r) for r in self._mention_rows()], [str(self.user2.id)])

    def test_a_hash_reference_notifies_no_one(self, _push):
        update_task_run(
            {
                "task_id": self.task.task_id,
                "content_text": f"see #MIP-{self.task.project_task_number}",
            },
            self.ctx,
        )
        self.task.refresh_from_db()
        self.assertEqual(_chips(self.task.content)[0]["type"], "hashTask")
        self.assertEqual(self._mention_rows(), [])

    def test_fetch_task_markdown_keeps_the_chip(self, _push):
        update_task_run(
            {"task_id": self.task.task_id, "content_text": f"ping [@x](user:{self.user2.id})"},
            self.ctx,
        )
        md = fetch_task_run({"task_id": self.task.task_id}, self.ctx)["content_markdown"]
        self.assertIn(f"[@otheruser](user:{self.user2.id})", md)

    def test_a_notify_failure_never_fails_the_write(self, _push):
        with patch(
            "origin.services.v3_activity.create_surface_mention_activities",
            side_effect=RuntimeError("boom"),
        ):
            result = update_task_run(
                {"task_id": self.task.task_id, "content_text": f"[@x](user:{self.user2.id})"},
                self.ctx,
            )
        self.assertIn("content", result["changed_fields"])
        self.task.refresh_from_db()
        # The baseline didn't move, so the next save files the mention.
        self.assertIn(self.task.mentioned_user_ids, (None, []))
        update_task_run(
            {"task_id": self.task.task_id, "content_text": f"[@x](user:{self.user2.id}) again"},
            self.ctx,
        )
        self.assertEqual([str(r) for r in self._mention_rows()], [str(self.user2.id)])


class CommentChipTests(ChipTestBase):
    @patch("origin.search_engine.agent.tools.add_comment.fan_out_task_comment")
    def test_add_comment_stores_chips(self, _fanout):
        add_comment_run(
            {
                "task_id": self.task.task_id,
                "body_text": f"[@x](user:{self.user2.id}) see #MIP-{self.task.project_task_number}",
            },
            self.ctx,
        )
        comment = TaskComments.objects.filter(task=self.task).latest("comment_id")
        self.assertEqual([c["type"] for c in _chips(comment.comment_body)], ["mention", "hashTask"])


@patch("origin.search_engine.agent.tools.update_note.push_note_body")
class NoteChipTests(ChipTestBase):
    def test_task_note_create_and_update_file_mentions(self, _push):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        result = create_note_run(
            {
                "note_type": "task",
                "project_id": self.project.project_id,
                "title": "Plan",
                "content_text": f"hey [@x](user:{self.user2.id})",
            },
            self.ctx,
        )
        note = TaskNoteMaster.objects.get(note_id=result["note_id"])
        self.assertEqual(note.mentioned_user_ids, [str(self.user2.id)])
        row = Activity.objects.get(surface_type=7)
        self.assertEqual(row.meta["noteId"], note.note_id)
        self.assertEqual(row.meta["noteType"], 2)

        update_note_run(
            {"note_type": "task", "note_id": note.note_id, "content_text": "gone"}, self.ctx
        )
        self.assertFalse(Activity.objects.filter(surface_type=7).exists())

    def test_unshared_personal_note_pages_only_people_who_can_open_it(self, _push):
        result = create_note_run(
            {
                "note_type": "personal",
                "title": "Private",
                "content_text": f"[@x](user:{self.user2.id}) and [@me](user:{self.user.id})",
            },
            self.ctx,
        )
        note = PersonalNoteMaster.objects.get(note_id=result["note_id"])
        # Both are stored as mentioned (the editor's baseline)...
        self.assertEqual(sorted(note.mentioned_user_ids), sorted([self.uid, str(self.user2.id)]))
        # ...but only the owner, who can open it, is paged.
        self.assertEqual(
            [
                str(r)
                for r in Activity.objects.filter(surface_type=6).values_list(
                    "recipient_id", flat=True
                )
            ],
            [self.uid],
        )
        self.assertTrue(
            NotePermissionMaster.objects.filter(
                note_id=note.note_id, user=self.user, role_id=ROLE_OWNER
            ).exists()
        )

    def test_fetch_note_markdown_keeps_chips_through_an_edit(self, _push):
        note = PersonalNoteMaster.objects.create(team=self.team, owner=self.user, title="Spec")
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_OWNER,
        )
        update_note_run(
            {
                "note_type": "personal",
                "note_id": note.note_id,
                "content_text": f"## Plan\n\nsee #MIP-{self.task.project_task_number}",
            },
            self.ctx,
        )
        fetched = fetch_note_run({"note_type": "personal", "note_id": note.note_id}, self.ctx)
        self.assertIn(f"(task:{self.task.task_id})", fetched["body_markdown"])
        note.refresh_from_db()
        before = note.body
        # An agent re-saving what it read changes nothing.
        md = fetched["body_markdown"]
        # wrap_workspace_content fences the payload; take the inner text.
        inner = md.split("\n", 1)[1].rsplit("\n", 1)[0] if md.startswith("<") else md
        result = update_note_run(
            {"note_type": "personal", "note_id": note.note_id, "content_text": inner}, self.ctx
        )
        self.assertEqual(result["changed_fields"], [], result)
        note.refresh_from_db()
        self.assertEqual(note.body, before)


class TaskPlanChipTests(ChipTestBase):
    def test_plan_bodies_carry_chips_and_ping_once_each(self):
        from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN

        mention = f"[@x](user:{self.user2.id})"
        out = CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "milestone": {"title": "Beta", "description_markdown": f"owner {mention}"},
                "tasks": [
                    {
                        "title": "A",
                        "content_markdown": f"for {mention}, after #{self.task.display_id}",
                    },
                    {"title": "B"},
                ],
            },
            self.ctx,
        )
        backing = TaskMaster.objects.get(
            task_id=MilestoneMaster.objects.get(
                milestone_id=out["milestone"]["milestone_id"]
            ).task_id
        )
        task_a = TaskMaster.objects.get(task_id=out["tasks"][0]["task_id"])
        self.assertEqual([c["type"] for c in _chips(backing.content)], ["mention"])
        self.assertEqual([c["type"] for c in _chips(task_a.content)], ["mention", "hashTask"])
        # One row per body that mentions them — the milestone and task A.
        self.assertEqual(
            sorted(
                Activity.objects.filter(surface_type=5, recipient=self.user2).values_list(
                    "meta__taskId", flat=True
                )
            ),
            sorted([backing.task_id, task_a.task_id]),
        )
