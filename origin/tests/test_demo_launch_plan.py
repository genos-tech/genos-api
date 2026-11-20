"""The v2 demo's launch plan, part of it written through a connected Claude app (MIP-1740).

The landing page's hero screenshot shows "by the agent / by you" labels, which come from the `via` a
write is stamped with. What is pinned here:

  * **The records exist** in both languages: the project, its four tasks, five comments, and the
    decision note with its two versions.
  * **They are stamped by the real funnel**: Claude's task, priority, comment rows, the note's
    first version and the chat card and comment mirrors carry what `McpOAuthCredential.via()`
    returns (kind "oauth", `app: "claude"`); the visitor's own writes do not earn a chip.
  * **Privacy is unchanged**: a colleague reading through the real endpoints sees `{"kind":
    "oauth", "app": "claude"}` and never the connection's name or id; the visitor reads their own
    row whole.
  * **v1 is untouched** (and `test_demo_v1_golden` still pins its text).
  * The thread-local source does not leak, the chat cards survive the commit-time refresh, and the
    plan is swept with the team.
"""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.middleware.current_user import get_current_source
from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.common.team_models import TeamMembers
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.serializers.chat.unified_serializers import MessageSerializer
from origin.services import demo_launch_plan
from origin.services.demo_seeder import create_demo_environment, delete_demo_environment
from origin.views.public.mcp.via_privacy import via_for_viewer

User = get_user_model()

CLAUDE_FAMILY = {"kind": "oauth", "app": "claude"}
NOTE_TYPE_TASK = 2


def _user(name):
    return User.objects.create_user(
        username=name, email=f"{name}@example.com", password="x", is_demo=True
    )


def _client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
    return client


def _is_claude(via):
    return isinstance(via, dict) and via.get("kind") == "oauth" and via.get("app") == "claude"


def _earns_no_chip(via):
    """The web app (`{"kind": "app"}`) or nothing: neither is an oauth or api_key write."""
    return via is None or via.get("kind") == "app"


class _Workspace:
    """One seeded visitor with handles on the launch plan."""

    def __init__(self, lang, short):
        self.lang = lang
        self.visitor = _user(f"lp_{short}")
        self.env = create_demo_environment(self.visitor, short=short, lang=lang, version="v2")
        self.team_id = self.env["team_id"]
        self.copy = demo_launch_plan.COPY[lang]
        self.project = ProjectMaster.objects.get(
            team_id=self.team_id, project_name=self.copy["project"]
        )
        self.tasks = {
            key: TaskMaster.objects.get(project=self.project, title=title)
            for key, (title, _summary) in self.copy["tasks"].items()
        }
        self.note = TaskNoteMaster.objects.get(project=self.project, title=self.copy["note_title"])
        self.colleague = User.objects.get(
            pk__in=TeamMembers.objects.filter(team_id=self.team_id).values("attendee_id"),
            username="Alice Chen",
        )

    def activity(self, key, action_type=None):
        rows = TaskActivity.objects.filter(task=self.tasks[key]).order_by("activity_id")
        return rows.filter(action_type=action_type) if action_type else rows


class DemoLaunchPlanTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.en = _Workspace("en", "lpen")
        cls.ja = _Workspace("ja", "lpja")
        cls.v1_user = _user("lp_v1")
        cls.v1 = create_demo_environment(cls.v1_user, short="lpv1", lang="en")

    def each_language(self):
        for ws in (self.en, self.ja):
            with self.subTest(lang=ws.lang):
                yield ws

    # --- the records exist ------------------------------------------------------------------

    def test_the_plan_exists_in_both_languages(self):
        for ws in self.each_language():
            self.assertEqual(len(ws.tasks), 4)
            self.assertEqual(
                TaskMaster.objects.filter(project=ws.project, is_milestone=False).count(), 4
            )
            self.assertTrue(
                ProjectMembers.objects.filter(project=ws.project, attendee=ws.visitor).exists()
            )
            self.assertEqual(TaskComments.objects.filter(task__project=ws.project).count(), 5)
            self.assertEqual(
                list(
                    NoteVersionMaster.objects.filter(
                        note_type=NOTE_TYPE_TASK, note_id=ws.note.note_id
                    )
                    .values_list("version_no", flat=True)
                    .order_by("version_no")
                ),
                [1, 2],
            )

    def test_the_decision_note_says_what_and_why(self):
        self.assertEqual(self.en.note.title, "Decision: launch on March 12")
        text = json.dumps(self.en.note.body)
        self.assertIn("March 5", text)
        self.assertIn("Proposed by Claude", text)
        self.assertEqual(self.en.note.task_id, self.en.tasks["legal"].task_id)
        self.assertIn("3月5日", json.dumps(self.ja.note.body, ensure_ascii=False))

    def test_both_languages_carry_the_same_plan(self):
        en, ja = demo_launch_plan.COPY["en"], demo_launch_plan.COPY["ja"]
        self.assertEqual(set(en), set(ja))
        for key in ("tasks", "comments"):
            self.assertEqual(list(en[key]), list(ja[key]))
        self.assertEqual(len(en["tags"]), len(ja["tags"]))
        self.assertEqual(len(en["note_sections"]("x")), len(ja["note_sections"]("x")))

    def test_every_task_is_filed_with_tags_and_effort(self):
        for ws in self.each_language():
            for task in ws.tasks.values():
                self.assertTrue(task.tags and task.effort_level and task.effort_level_code)

    def test_the_due_dates_and_the_text_agree_on_the_same_march(self):
        for ws in self.each_language():
            due = {key: task.due_date for key, task in ws.tasks.items()}
            self.assertEqual({d.month for d in due.values()}, {3})
            self.assertEqual(due["legal"].day, 5)
            self.assertEqual(len({d.year for d in due.values()}), 1)

    # --- Claude's writes are stamped by the real funnel ---------------------------------------

    def test_the_via_is_what_the_credential_produces(self):
        via = demo_launch_plan.claude_via(self.en.visitor)
        self.assertEqual(set(via), {"kind", "connection_id", "client_name", "app"})
        self.assertEqual((via["kind"], via["app"]), ("oauth", "claude"))
        self.assertEqual(via["client_name"], demo_launch_plan.CLAUDE_CONNECTION_NAME)

    def test_claude_created_its_three_tasks_as_the_visitor(self):
        for ws in self.each_language():
            for key in ("legal", "announce", "room"):
                created = ws.activity(key, TaskActivityActionType.CREATED).get()
                self.assertTrue(_is_claude(created.metadata["via"]), key)
                self.assertEqual(
                    created.actor_id, ws.visitor.id, "a connected app writes as its owner"
                )
                self.assertEqual(ws.tasks[key].reporter_id, ws.visitor.id)

    def test_the_visitors_own_task_earns_no_chip(self):
        for ws in self.each_language():
            created = ws.activity("script", TaskActivityActionType.CREATED).get()
            self.assertTrue(_earns_no_chip(created.metadata.get("via")), created.metadata)
            self.assertEqual(created.actor_id, ws.visitor.id)

    def test_one_priority_change_by_claude_and_one_by_the_visitor(self):
        for ws in self.each_language():
            legal = ws.activity("legal", TaskActivityActionType.PRIORITY).get()
            self.assertTrue(_is_claude(legal.metadata["via"]))
            self.assertEqual((legal.old_value, legal.new_value), ("Normal", "High"))
            room = ws.activity("room", TaskActivityActionType.PRIORITY).get()
            self.assertTrue(_earns_no_chip(room.metadata.get("via")), room.metadata)
            self.assertEqual((room.old_value, room.new_value), ("Normal", "High"))
            self.assertEqual(room.actor_id, ws.visitor.id)
            self.assertEqual(TaskMaster.objects.get(pk=ws.tasks["room"].pk).priority, "High")

    def test_comments_carry_who_wrote_them(self):
        for ws in self.each_language():
            claude_rows = member_rows = 0
            for row in TaskActivity.objects.filter(
                task__project=ws.project, action_type=TaskActivityActionType.COMMENT_ADDED
            ):
                if _is_claude(row.metadata["via"]):
                    claude_rows += 1
                else:
                    self.assertTrue(_earns_no_chip(row.metadata.get("via")), row.metadata)
                    member_rows += 1
            self.assertEqual((claude_rows, member_rows), (3, 2))

    def test_the_note_version_by_claude_and_the_visitors_line_after_it(self):
        for ws in self.each_language():
            v1, v2 = NoteVersionMaster.objects.filter(
                note_type=NOTE_TYPE_TASK, note_id=ws.note.note_id
            ).order_by("version_no")
            self.assertTrue(_is_claude(v1.via))
            self.assertIsNone(v2.via)
            self.assertEqual((v1.editor_id, v2.editor_id), (ws.visitor.id, ws.visitor.id))
            self.assertEqual(str(v1.team_id), ws.team_id, "kept on the team so cleanup finds it")
            self.assertNotEqual(v1.body, v2.body)

    def test_cards_and_comment_mirrors_carry_the_kind_and_family_only(self):
        for ws in self.each_language():
            channel = Channel.objects.get(project=ws.project, kind=ChannelKind.PM)
            cards = {
                m.task_id: m for m in Message.objects.filter(channel=channel, is_thread_reply=False)
            }
            self.assertEqual(set(cards), {t.task_id for t in ws.tasks.values()})
            for key in ("legal", "announce", "room"):
                self.assertEqual(cards[ws.tasks[key].task_id].metadata["via"], CLAUDE_FAMILY)
            self.assertNotIn("via", cards[ws.tasks["script"].task_id].metadata)
            mirrors = Message.objects.filter(channel=channel, is_thread_reply=True)
            self.assertEqual(mirrors.count(), 5)
            self.assertEqual(sum(1 for m in mirrors if "via" in m.metadata), 3)
            for message in Message.objects.filter(channel=channel):
                wire = MessageSerializer(message).data["metadata"]
                self.assertIn(wire.get("via"), (None, CLAUDE_FAMILY))

    def test_the_source_does_not_leak_out_of_the_seeding(self):
        self.assertIsNone(get_current_source())

    # --- privacy: unchanged ---------------------------------------------------------------------

    def _assert_no_secret(self, ws, payload):
        text = json.dumps(payload, default=str)
        for secret in ("client_name", "connection_id", "key_name"):
            self.assertNotIn(secret, text, secret)
        for row in TaskActivity.objects.filter(
            task__project=ws.project, metadata__via__kind="oauth"
        ):
            self.assertNotIn(row.metadata["via"]["connection_id"], text)

    def test_a_colleague_sees_only_the_kind_and_family_in_the_activity_feed(self):
        for ws in self.each_language():
            client = _client_for(ws.colleague)
            for key in ("legal", "announce", "room"):
                res = client.get(
                    "/api/v2/task/activity/",
                    {"team_id": ws.team_id, "task_id": ws.tasks[key].task_id},
                )
                self.assertEqual(res.status_code, 200, res.content)
                vias = [r["metadata"]["via"] for r in res.data if "via" in r["metadata"]]
                self.assertTrue(vias)
                claude_rows = [v for v in vias if v.get("kind") == "oauth"]
                self.assertTrue(claude_rows, key)
                for via in claude_rows:
                    self.assertEqual(via, CLAUDE_FAMILY)
                self._assert_no_secret(ws, res.data)

    def test_a_colleague_sees_only_the_kind_and_family_on_the_comments(self):
        for ws in self.each_language():
            res = _client_for(ws.colleague).get(
                "/api/v2/task/comment/", {"task_id": ws.tasks["announce"].task_id}
            )
            self.assertEqual(res.status_code, 200, res.content)
            vias = {c["commentId"]: c["via"] for c in res.data}
            self.assertEqual(len(vias), 2)
            self.assertEqual(vias[1], CLAUDE_FAMILY)
            self.assertTrue(_earns_no_chip(vias[2]), vias)
            self._assert_no_secret(ws, res.data)

    def test_a_colleague_sees_only_the_kind_and_family_in_the_note_history(self):
        for ws in self.each_language():
            res = _client_for(ws.colleague).get(
                "/api/v2/note/version/all/",
                {"team_id": ws.team_id, "note_type": NOTE_TYPE_TASK, "note_id": ws.note.note_id},
            )
            self.assertEqual(res.status_code, 200, res.content)
            by_no = {row["versionNo"]: row["via"] for row in res.data}
            self.assertEqual(by_no[1], CLAUDE_FAMILY)
            self.assertIsNone(by_no[2])
            self._assert_no_secret(ws, res.data)

    def test_the_visitor_reads_their_own_rows_whole(self):
        for ws in self.each_language():
            res = _client_for(ws.visitor).get(
                "/api/v2/task/comment/", {"task_id": ws.tasks["announce"].task_id}
            )
            via = {c["commentId"]: c["via"] for c in res.data}[1]
            self.assertEqual(via["client_name"], demo_launch_plan.CLAUDE_CONNECTION_NAME)
            self.assertEqual(via["app"], "claude")
            stored = (
                TaskActivity.objects.filter(task__project=ws.project, metadata__via__kind="oauth")
                .first()
                .metadata["via"]
            )
            self.assertEqual(via_for_viewer(stored, ws.visitor.id, ws.colleague.id), CLAUDE_FAMILY)

    # --- v1 is untouched ------------------------------------------------------------------------

    def test_v1_gets_no_launch_plan_and_no_connected_app_writes(self):
        team = self.v1["team_id"]
        self.assertFalse(
            ProjectMaster.objects.filter(team_id=team, project_name="Launch plan").exists()
        )
        self.assertEqual(ProjectMaster.objects.filter(team_id=team).count(), 2)
        self.assertFalse(
            TaskActivity.objects.filter(task__team_id=team, metadata__via__kind="oauth").exists()
        )
        self.assertFalse(NoteVersionMaster.objects.filter(team_id=team, via__isnull=False).exists())
        self.assertFalse(
            Message.objects.filter(channel__team_id=team, metadata__has_key="via").exists()
        )


class DemoLaunchPlanLifecycleTests(TestCase):
    def test_the_chat_cards_are_current_when_the_signin_commits(self):
        """Claude's writes schedule a card refresh for commit (the app would rewrite its own card;
        a connected app has no browser). The cards are built by the refresh's own builder, so
        running it changes nothing."""
        user = _user("lp_commit")
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            ws = create_demo_environment(user, short="lpcommit", lang="en", version="v2")
        project = ProjectMaster.objects.get(team_id=ws["team_id"], project_name="Launch plan")
        channel = Channel.objects.get(project=project, kind=ChannelKind.PM)
        before = {
            m.pk: (m.body, m.metadata, m.ts_updated_at)
            for m in Message.objects.filter(channel=channel)
        }
        for callback in callbacks:
            callback()
        after = {
            m.pk: (m.body, m.metadata, m.ts_updated_at)
            for m in Message.objects.filter(channel=channel)
        }
        self.assertEqual(before, after)

    def test_the_plan_is_swept_with_the_team(self):
        user = _user("lp_sweep")
        env = create_demo_environment(user, short="lpsweep", lang="en", version="v2")
        project = ProjectMaster.objects.get(team_id=env["team_id"], project_name="Launch plan")
        note_id = TaskNoteMaster.objects.get(project=project).note_id
        delete_demo_environment(user)
        self.assertFalse(TaskNoteMaster.objects.filter(note_id=note_id).exists())
        self.assertFalse(
            NoteVersionMaster.objects.filter(note_type=NOTE_TYPE_TASK, note_id=note_id).exists()
        )
        self.assertFalse(ProjectMaster.objects.filter(pk=project.pk).exists())
        self.assertFalse(TaskMaster.objects.filter(team_id=env["team_id"]).exists())

    def test_a_visitor_who_never_asked_for_v2_gets_none_of_it(self):
        user = _user("lp_default")
        env = create_demo_environment(user, short="lpdefault", lang="en")
        self.assertFalse(
            Message.objects.filter(
                channel__team_id=env["team_id"], metadata__has_key="via"
            ).exists()
        )
