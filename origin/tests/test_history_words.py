"""MIP-2551: a briefing line and an agent work report line say names, never ids, and no name the
reader may not see.

`self.user` is on both projects, `self.user2` only on `self.open`.
"""

import re
from datetime import timedelta

from django.utils import timezone

from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectCustomField, ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import agent_work, briefing
from origin.services.history_words import HistoryWords
from origin.services.milestone_service import create_milestone
from origin.tests.test_base import BaseAPITestCase

A = TaskActivityActionType
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
KEY = {"kind": "api_key", "key_id": "k-1"}


class HistoryWordsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        for member in (self.user, self.user2):
            TeamMembers.objects.get_or_create(team=self.team, attendee=member)
        self.open = self._project("Open", [self.user, self.user2])
        self.vault = self._project("Vault", [self.user])
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.open, title="T", reporter=self.user, status="Open"
        )
        self.launch = create_milestone(self.open, reporter_id=self.user.id, title="Launch")
        self.secret = create_milestone(self.vault, reporter_id=self.user.id, title="Takeover")

    def _project(self, name, members):
        project = ProjectMaster.objects.create(
            team=self.team, project_name=name, owner=self.user, project_system_user=self.user
        )
        for member in members:
            ProjectMembers.objects.create(team=self.team, project=project, attendee=member)
        return project

    def row(self, action, field, old, new, metadata=None, project=None):
        return TaskActivity.objects.create(
            team=self.team,
            project=project or self.open,
            task=self.task,
            actor=self.user,
            action_type=action,
            field_name=field,
            old_value=old,
            new_value=new,
            metadata={"via": KEY, **(metadata or {})},
        )

    def milestone_row(self, old, new):
        return self.row(
            A.MILESTONE_LINK,
            "milestone_id",
            old and old.milestone_id,
            new.milestone_id,
            {"oldLabel": old and old.title, "newLabel": new.title},
        )

    def said(self, viewer, row):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(viewer.id))
        words = HistoryWords(ctx, [row], viewer_id=str(viewer.id))
        return briefing.activity_event(row, str(viewer.id), words)

    def report_text(self, viewer, row):
        out = agent_work.build(
            viewer_id=viewer.id,
            team_id=str(self.team.team_id),
            f=agent_work.Filters(7, None, None, None, None),
            offset=0,
            limit=200,
            now=timezone.now() + timedelta(seconds=1),
        )
        items = {i["id"]: i for g in out["groups"] for i in g["items"]}
        return items[f"activity:{row.pk}"]["text"]

    # -- the briefing's words --------------------------------------------------------------

    def test_the_briefing_names_the_reporter(self):
        row = self.row(A.REPORTER, "reporter_id", str(self.user.id), str(self.user2.id))
        event = self.said(self.user, row)
        self.assertEqual(
            event["summary"], f"reporter: {self.user.username} → {self.user2.username}"
        )
        self.assertEqual(event["_data"]["new"], self.user2.username)

    def test_a_person_the_reader_may_not_see_is_not_named(self):
        gone = CustomUser.objects.create_user(
            email="gone@example.com", username="departed", password="x"
        )
        row = self.row(A.REPORTER, "reporter_id", str(gone.id), str(self.user.id))
        event = self.said(self.user, row)
        self.assertEqual(event["summary"], "changed the reporter")
        self.assertEqual(event["_data"], {"code": "field_edited", "field": "reporter"})

    def test_the_briefing_names_a_milestone_only_to_who_may_see_it(self):
        row = self.milestone_row(self.launch, self.secret)
        self.assertEqual(self.said(self.user, row)["summary"], "milestone: Launch → Takeover")
        event = self.said(self.user2, row)
        self.assertEqual(event["summary"], "changed the milestone")
        self.assertNotIn("Takeover", repr(event["_data"]))
        self.assertNotIn(str(self.secret.milestone_id), event["summary"])

    def test_an_unset_side_reads_as_empty(self):
        row = self.milestone_row(None, self.launch)
        event = self.said(self.user, row)
        self.assertEqual(event["summary"], "milestone: — → Launch")
        self.assertIsNone(event["_data"]["old"])

    def test_without_words_the_briefing_says_no_values(self):
        row = self.row(A.REPORTER, "reporter_id", str(self.user.id), str(self.user2.id))
        event = briefing.activity_event(row, str(self.user.id))
        self.assertEqual(event["summary"], "changed the reporter")
        self.assertIsNone(UUID_RE.search(repr(event["_data"])))

    # -- the agent work report ---------------------------------------------------------------

    def test_the_report_names_people(self):
        for action, field in ((A.ASSIGNEE, "assignee_id"), (A.REPORTER, "reporter_id")):
            row = self.row(action, field, str(self.user.id), str(self.user2.id))
            self.assertEqual(
                self.report_text(self.user, row),
                f"{self.user.username} → {self.user2.username}",
            )

    def test_the_report_names_a_milestone_or_says_nothing(self):
        row = self.milestone_row(self.launch, self.secret)
        self.assertEqual(self.report_text(self.user, row), "Launch → Takeover")
        self.assertIsNone(self.report_text(self.user2, row))

    def test_the_report_says_options_and_members_by_name(self):
        field = ProjectCustomField.objects.create(
            team=self.team, project=self.open, field_name="Client", field_type="tag"
        )
        tag = self.row(
            A.CUSTOM_FIELD,
            "custom_field",
            ["o-1"],
            ["o-2", "o-3"],
            {
                "fieldId": field.field_id,
                "fieldType": "tag",
                "oldLabel": ["Acme"],
                "newLabel": ["Beta", "Gamma"],
            },
        )
        self.assertEqual(self.report_text(self.user, tag), "Acme → Beta, Gamma")
        deleted = self.row(
            A.CUSTOM_FIELD,
            "custom_field",
            ["o-1"],
            ["o-gone"],
            {"fieldType": "tag", "oldLabel": ["Acme"], "newLabel": ["o-gone"]},
        )
        self.assertIsNone(self.report_text(self.user, deleted))
        member = self.row(
            A.CUSTOM_FIELD,
            "custom_field",
            str(self.user.id),
            str(self.user2.id),
            {"fieldType": "member", "oldLabel": "x", "newLabel": "y"},
        )
        self.assertEqual(
            self.report_text(self.user, member),
            f"{self.user.username} → {self.user2.username}",
        )
