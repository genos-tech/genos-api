"""The note rule of `get_timeline` (MIP-1281, design check (b)), now ONE rule (MIP-1474).

Four ways to answer "may this member open this note now?" exist in the code:

  * `load_readable_note` (what `get_note_history`, `update_note`... apply): the note is in the
    team, and the member owns it or holds a role on it (`may_read_note` -> `get_effective_role`);
  * `fetch_note` (`get_note` over MCP and in the app): the owner-or-ACL SET rule of the search
    index (`task_note_acl_user_ids`, `chat_note_acl_user_ids`);
  * `briefing._may_read_note` (the briefing and `get_item_line`);
  * `timeline.readable_notes`: one batch of queries for many notes.

They used to disagree on four chat-note cases (a stray channel row on a project chat, a project
member with no mirror row, a deleted channel, a `chat_type` that differs from the channel's kind):
`get_effective_role` read the channel-member row alone, the index and the briefing read the
channel's own rule (`channel_scope.chat_member_q`). MIP-1474 made the per-note resolver, its
batch, the sidebar list and the briefing one rule (`channel_scope.chat_note_member_q`), equal to
the index's. These tests generate every way a note can be readable or not, across the three note
families, and pin that (1) the batch equals the per-note rule for each, (2) ALL answers agree on
every case, and (3) the four cases that used to differ now have the answer below.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.fetch_note import _run as fetch_note_run
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services import briefing, timeline
from origin.tests.test_item_lines import LineBase

User = get_user_model()
PERSONAL, TASK, CHAT = 1, 2, 3
FAMILY = {PERSONAL: "personal", TASK: "task", CHAT: "chat"}


class NoteRuleMatrix(LineBase):
    def setUp(self):
        super().setUp()
        self.viewer = self.outsider  # a member of the team on no project, in no chat
        self.other = User.objects.create_user(
            username="author", email="author@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.other)
        self.foreign_team = TeamMaster.objects.create(
            team_name="Else", team_email="e@example.com", owner=self.other
        )
        self.cases: dict[str, tuple[int, object]] = {}

    # -- builders ----------------------------------------------------------------------------- #

    def grant(self, note_type, note, user, role=3):
        NotePermissionMaster.objects.create(
            team=self.team, user=user, note_type=note_type, note_id=note.note_id, role_id=role
        )

    def personal(self, name, *, owner=None, team=None, folder=None):
        note = PersonalNoteMaster.objects.create(
            team=team or self.team,
            owner=owner or self.other,
            title=name,
            body=[],
            folder_id=folder.folder_id if folder else None,
        )
        self.cases[name] = (PERSONAL, note)
        return note

    def folder(self, visibility, perm_for=None):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.other,
            name=f"f{visibility}{perm_for}",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )
        if perm_for is not None:
            NoteFolderPermission.objects.create(
                folder=folder, user=perm_for, team=self.team, role_id=3
            )
        return folder

    def task_note(self, name, *, owner=None, project=None, team=None):
        task = self.make_task(name)
        note = TaskNoteMaster.objects.create(
            team=team or self.team,
            project=project or task.project,
            owner=owner or self.other,
            task=task,
            title=name,
            body=[],
        )
        self.cases[name] = (TASK, note)
        return note

    def chat_note(self, name, *, channel, chat_type=None, owner=None, team=None):
        note = ChatNoteMaster.objects.create(
            team=team or self.team,
            owner=owner or self.other,
            chat_type=chat_type if chat_type is not None else channel.kind,
            channel=channel,
            is_thread=False,
            title=name,
            body=[],
        )
        self.cases[name] = (CHAT, note)
        return note

    def build(self):
        v = self.viewer
        # personal
        self.personal("P owner", owner=v)
        self.personal("P stranger")
        grant = self.personal("P explicit viewer grant")
        self.grant(PERSONAL, grant, v)
        other_grant = self.personal("P grant for someone else")
        self.grant(PERSONAL, other_grant, self.user2)
        self.personal(
            "P public team folder", folder=self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC)
        )
        self.personal(
            "P private team folder, no permission",
            folder=self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE),
        )
        self.personal(
            "P private team folder, permission",
            folder=self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE, perm_for=v),
        )
        foreign = self.personal("P other team with a grant", team=self.foreign_team)
        self.grant(PERSONAL, foreign, v)
        self.personal("P other team, owned", owner=v, team=self.foreign_team)
        # task
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Notes",
            code="NTS",
            owner=self.other,
            project_system_user=self.other,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.other)
        self.task_note("T owner not on the project", owner=v, project=project)
        member_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Mine",
            code="MNE",
            owner=self.other,
            project_system_user=self.other,
        )
        ProjectMembers.objects.create(team=self.team, project=member_project, attendee=v)
        self.task_note("T project member", project=member_project)
        self.task_note("T no relation", project=project)
        granted = self.task_note("T explicit grant only", project=project)
        self.grant(TASK, granted, v)
        self.task_note("T other team, owned", owner=v, project=project, team=self.foreign_team)
        # chat
        gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="gm")
        ChannelMember.objects.create(channel=gm, user=v)
        self.chat_note(
            "C owner, not a member",
            channel=Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="x"),
            owner=v,
        )
        self.chat_note("C channel member", channel=gm)
        left = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="left")
        ChannelMember.objects.create(channel=left, user=v, is_deleted=True)
        self.chat_note("C member row deleted", channel=left)
        gone = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="gone", is_deleted=True
        )
        ChannelMember.objects.create(channel=gone, user=v)
        self.chat_note("C channel deleted, still a member", channel=gone)
        self.chat_note(
            "C chat_type differs from the channel kind", channel=gm, chat_type=ChannelKind.DM
        )
        # a project's chat exists from the day the project does: take it, then set the two rows
        # (the channel row that mirrors a member, the project membership) one by one
        pm_mirror = Channel.objects.get(kind=ChannelKind.PM, project=project)
        ChannelMember.objects.update_or_create(
            channel=pm_mirror, user=v, defaults={"is_deleted": False}
        )
        self.chat_note("C project chat, channel row only", channel=pm_mirror)
        pm_project = Channel.objects.get(kind=ChannelKind.PM, project=member_project)
        ChannelMember.objects.filter(channel=pm_project, user=v).delete()
        self.chat_note("C project chat, on the project only", channel=pm_project)
        both = ProjectMaster.objects.create(
            team=self.team,
            project_name="Both",
            code="BTH",
            owner=self.other,
            project_system_user=self.other,
        )
        ProjectMembers.objects.create(team=self.team, project=both, attendee=v)
        pm_both = Channel.objects.get(kind=ChannelKind.PM, project=both)
        ChannelMember.objects.update_or_create(
            channel=pm_both, user=v, defaults={"is_deleted": False}
        )
        self.chat_note("C project chat, both", channel=pm_both)
        granted_chat = self.chat_note(
            "C explicit grant only",
            channel=Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="y"),
        )
        self.grant(CHAT, granted_chat, v)
        self.chat_note(
            "C not a member",
            channel=Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="z"),
        )
        other_chat = Channel.objects.create(team=self.foreign_team, kind=ChannelKind.GM, title="w")
        ChannelMember.objects.create(channel=other_chat, user=v)
        self.chat_note("C other team, a member", channel=other_chat, team=self.foreign_team)

    # -- the three answers ---------------------------------------------------------------------- #

    def viewer_ctx(self):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(self.viewer.id))

    def get_note_rule(self, note_type, note) -> bool:
        try:
            load_readable_note(
                {"note_type": FAMILY[note_type], "note_id": note.note_id}, self.viewer_ctx()
            )
        except ToolError:
            return False
        return True

    def briefing_rule(self, note_type, note) -> bool:
        return briefing._may_read_note(self.viewer_ctx(), note_type, note)

    def fetch_note_rule(self, note_type, note) -> bool:
        """`get_note` over MCP and in the app: the search index's set rule."""
        try:
            fetch_note_run(
                {"note_type": FAMILY[note_type], "note_id": note.note_id}, self.viewer_ctx()
            )
        except ToolError:
            return False
        return True

    # -- the tests ---------------------------------------------------------------------------- #

    def test_the_batch_equals_get_notes_rule_for_every_case(self):
        self.build()
        keys = [(t, n.note_id) for t, n in self.cases.values()]
        batch = timeline.readable_notes(self.viewer_ctx(), keys)
        for name, (note_type, note) in self.cases.items():
            self.assertEqual(
                (note_type, note.note_id) in batch, self.get_note_rule(note_type, note), name
            )
        # the matrix is not trivial: both answers occur in every family
        for note_type in (PERSONAL, TASK, CHAT):
            answers = {self.get_note_rule(t, n) for t, n in self.cases.values() if t == note_type}
            self.assertEqual(answers, {True, False}, FAMILY[note_type])

    def test_the_batch_costs_a_fixed_number_of_queries_however_many_notes(self):
        from django.db import connection  # noqa: PLC0415
        from django.test.utils import CaptureQueriesContext  # noqa: PLC0415

        self.build()
        keys = [(t, n.note_id) for t, n in self.cases.values()]
        with CaptureQueriesContext(connection) as many:
            timeline.readable_notes(self.viewer_ctx(), keys)
        with CaptureQueriesContext(connection) as few:
            timeline.readable_notes(self.viewer_ctx(), keys[:3] + keys[-3:])
        self.assertLessEqual(len(many), 24)
        self.assertLessEqual(len(few), len(many))
        print("[MIP-1281 note batch queries] 41 notes:", len(many), "6 notes:", len(few))

    def test_every_way_of_asking_gives_the_same_answer_for_every_case(self):
        """One rule: `get_note`'s per-note resolver, `get_note` over MCP (the index's set rule),
        the briefing and the timeline's batch agree on all cases of all three families."""
        self.build()
        keys = [(t, n.note_id) for t, n in self.cases.values()]
        batch = timeline.readable_notes(self.viewer_ctx(), keys)
        differ = {
            name: {
                "load_readable_note": self.get_note_rule(t, n),
                "fetch_note": self.fetch_note_rule(t, n),
                "briefing": self.briefing_rule(t, n),
                "timeline batch": (t, n.note_id) in batch,
            }
            for name, (t, n) in self.cases.items()
            if len(
                {
                    self.get_note_rule(t, n),
                    self.fetch_note_rule(t, n),
                    self.briefing_rule(t, n),
                    (t, n.note_id) in batch,
                }
            )
            > 1
        }
        self.assertEqual(differ, {})

    def test_the_four_cases_that_used_to_differ_have_the_one_answer(self):
        """Flipped by MIP-1474, each with its reason (the old pin was
        `{name: (get_note's answer, the briefing's answer)}`)."""
        self.build()
        answers = {name: self.get_note_rule(t, n) for name, (t, n) in self.cases.items()}
        for name, expected in FORMERLY_DISAGREEING.items():
            self.assertEqual(answers[name], expected, name)


# `name: the one answer now`.
FORMERLY_DISAGREEING: dict[str, bool] = {
    # the channel is deleted: the member row outlives it, but nobody reads a deleted chat.
    # Was (get_note True, briefing False).
    "C channel deleted, still a member": False,
    # the note was filed under another kind than its channel's: the channel is looked up BY that
    # kind, found nothing, so it opens through its owner and grants only. Was (True, False).
    "C chat_type differs from the channel kind": False,
    # a project chat's readers are the project's (`chat_member_q`: "a stray channel row opens
    # nothing"): a channel-member row for someone NOT on the project, which `get_effective_role`
    # used to honour (the security hole of MIP-1474). Was (True, False).
    "C project chat, channel row only": False,
    # ...and a project member whose mirror row is missing still reads. Was (False, True).
    "C project chat, on the project only": True,
}
