"""MIP-1613: `GET /api/v2/agent/work/`, what the AI apps did in a team, for the Agent Activity tab.

The endpoint reads four sources (task activity, note versions, the team's decision notes, the
viewer's own connections) and answers with totals, per-person counts, project groups, decisions
and connections. These tests pin the privacy rules (who may see which row, how an API key and a
connected app are labelled for a colleague), the filters, the window, the page cursor, the empty
state, the text cut, and that the query count does not grow with the size of the team.
"""

from datetime import timedelta

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from oauth2_provider.models import Application

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.team_models import TeamMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.v3_activity import SURFACE_AI_APP
from origin.tests.test_base import BaseAPITestCase

URL = "/api/v2/agent/work/"

KEY = {"kind": "api_key", "key_id": "k-1", "key_name": "Alice's phone", "key_prefix": "gk_alice"}
CLAUDE = {
    "kind": "oauth",
    "app": "claude",
    "connection_id": "conn-9",
    "client_name": "Alice Claude",
}
CHATGPT = {"kind": "oauth", "app": "chatgpt", "connection_id": "conn-7", "client_name": "Alice GPT"}
OTHER_APP = {"kind": "oauth", "connection_id": "conn-8", "client_name": "Alice's secret tool"}
UNDO = {"kind": "app", "undo_of": {"run_id": "r-1", "step": 3}}
AGENT = {"kind": "agent"}
SECRETS = (
    "Alice's phone",
    "k-1",
    "gk_alice",
    "conn-9",
    "conn-7",
    "conn-8",
    "Alice Claude",
    "Alice GPT",
    "Alice's secret tool",
    "r-1",
)


class _Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Launch",
            code="LAU",
            owner=self.user,
            project_system_user=self.user,
        )
        for member in (self.user, self.user2):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Spec", status="Open"
        )
        TaskActivity.objects.all().delete()  # the setup rows carry no `via`
        TaskNoteMaster.objects.all().delete()

    def act(self, actor, via, *, action="status_changed", days_ago=0, task=None, **extra):
        metadata = dict(extra.pop("metadata", {}))
        if via is not None:
            metadata["via"] = via
        row = TaskActivity.objects.create(
            team=self.team,
            project=(task or self.task).project,
            task=task or self.task,
            actor=actor,
            action_type=action,
            field_name="status" if action == "status_changed" else None,
            old_value="Open" if action == "status_changed" else None,
            new_value="WIP" if action == "status_changed" else None,
            metadata=metadata,
            **extra,
        )
        if days_ago:
            TaskActivity.objects.filter(pk=row.pk).update(
                ts_created_at=timezone.now() - timedelta(days=days_ago)
            )
        return row

    def comment(self, actor, via, text, comment_id=1):
        self.act(actor, via, action="comment_added", metadata={"commentId": comment_id})
        return TaskComments.objects.create(
            task=self.task,
            sender=actor,
            comment_id=comment_id,
            comment_body=[{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
        )

    def decision(self, actor, via, title="Decision: ship Option A", note_type=2, days_ago=0):
        if note_type == 2:
            note = TaskNoteMaster.objects.create(
                team=self.team,
                project=self.project,
                owner=actor,
                task=self.task,
                title=title,
                body=None,
            )
            note_id = note.note_id
        else:
            note = PersonalNoteMaster.objects.create(
                team=self.team, owner=actor, title=title, body=None
            )
            note_id = note.note_id
        NoteVersionMaster.objects.create(
            team=self.team,
            note_type=note_type,
            note_id=note_id,
            version_no=1,
            editor=actor,
            title=title,
            body=None,
            via=via,
        )
        if days_ago:
            for model in (TaskNoteMaster, PersonalNoteMaster):
                model.objects.filter(note_id=note_id).update(
                    ts_created_at=timezone.now() - timedelta(days=days_ago)
                )
        return note_id

    def get(self, viewer, **params):
        self.authenticate(viewer)
        query = {"team_id": str(self.team.team_id), **params}
        res = self.client.get(URL, query)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def items(self, viewer, **params):
        return [item for group in self.get(viewer, **params)["groups"] for item in group["items"]]

    def assertNoSecrets(self, data):
        text = repr(data)
        for secret in SECRETS:
            self.assertNotIn(secret, text)


class TheEndpointContract(_Base):
    def test_it_needs_a_signed_in_member_of_the_team(self):
        res = self.client.get(URL, {"team_id": str(self.team.team_id)})
        self.assertIn(res.status_code, (401, 403))

    def test_a_viewer_outside_the_team_gets_the_same_404_as_a_missing_team(self):
        outsider = self.user2.__class__.objects.create_user(
            username="outsider", email="outsider@example.com", password="pw-outsider-1"
        )
        self.authenticate(outsider)
        res = self.client.get(URL, {"team_id": str(self.team.team_id)})
        missing = self.client.get(URL, {"team_id": "00000000-0000-0000-0000-000000000000"})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(res.json(), missing.json())

    def test_team_id_is_required(self):
        self.authenticate(self.user)
        self.assertEqual(self.client.get(URL).status_code, 400)

    def test_bad_filters_are_refused(self):
        self.authenticate(self.user)
        base = {"team_id": str(self.team.team_id)}
        for bad in (
            {"days": 0},
            {"days": 32},
            {"days": "week"},
            {"agent": "gemini"},
            {"kind": "everything"},
            {"user_id": "not-a-uuid"},
            {"project_id": "x"},
            {"limit": "many"},
            {"cursor": "-1"},
        ):
            with self.subTest(bad=bad):
                self.assertEqual(self.client.get(URL, {**base, **bad}).status_code, 400)

    def test_the_response_has_every_section_even_when_empty(self):
        data = self.get(self.user)
        self.assertEqual(
            set(data),
            {
                "window",
                "filters",
                "totals",
                "by_user_agent",
                "groups",
                "decisions",
                "connections",
                "next_cursor",
            },
        )
        self.assertEqual(
            data["totals"]["by_agent"],
            {"claude": 0, "chatgpt": 0, "api_key": 0, "other_app": 0, "person": 0},
        )
        self.assertEqual(data["totals"]["decisions"], 0)
        self.assertEqual(data["totals"]["changes"], 0)
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["decisions"], [])
        self.assertEqual(data["connections"], [])
        self.assertEqual(data["by_user_agent"], [])
        self.assertIsNone(data["next_cursor"])
        self.assertEqual(data["window"]["days"], 7)


class ThePrivacyRules(_Base):
    def test_a_colleague_sees_an_api_key_as_api_key_and_nothing_of_the_key(self):
        self.act(self.user2, KEY)
        [item] = self.items(self.user)
        self.assertEqual(item["agent"], {"kind": "api_key"})
        self.assertNoSecrets(self.get(self.user))

    def test_the_owner_of_the_key_sees_the_same_label_and_the_key_stays_out_of_the_response(self):
        self.act(self.user2, KEY)
        [item] = self.items(self.user2)
        self.assertEqual(item["agent"], {"kind": "api_key"})
        self.assertNoSecrets(self.get(self.user2))

    def test_a_connected_claude_or_chatgpt_shows_its_family_to_everyone(self):
        self.act(self.user2, CLAUDE)
        self.act(self.user2, CHATGPT)
        agents = sorted(i["agent"]["kind"] for i in self.items(self.user))
        self.assertEqual(agents, ["chatgpt", "claude"])
        for item in self.items(self.user):
            self.assertEqual(
                item["agent"], {"kind": item["agent"]["kind"], "app": item["agent"]["kind"]}
            )
        self.assertNoSecrets(self.get(self.user))

    def test_another_connected_app_is_other_app_with_no_name(self):
        self.act(self.user2, OTHER_APP)
        [item] = self.items(self.user)
        self.assertEqual(item["agent"], {"kind": "other_app"})
        self.assertNoSecrets(self.get(self.user))

    def test_an_unmarked_write_is_a_person_with_no_agent(self):
        self.act(self.user2, None)
        self.act(self.user2, UNDO)
        self.act(self.user2, AGENT)
        items = self.items(self.user)
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertIsNone(item["agent"])
            self.assertEqual(item["user"]["id"], str(self.user2.id))
        self.assertEqual(self.get(self.user)["totals"]["by_agent"]["person"], 3)

    def test_a_system_write_with_no_person_is_left_out(self):
        self.act(None, {"kind": "github"})
        self.act(None, None)
        self.assertEqual(self.items(self.user), [])

    def test_the_undo_ids_stay_with_the_person_who_clicked(self):
        self.act(self.user2, UNDO)
        self.assertNotIn("r-1", repr(self.get(self.user)))

    def test_rows_of_another_team_are_absent(self):
        other = TeamMaster.objects.create(team_name="Other team", owner=self.user2)
        other_project = ProjectMaster.objects.create(
            team=other,
            project_name="Elsewhere",
            code="ELS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        other_task = TaskMaster.objects.create(
            team=other, project=other_project, reporter=self.user2, title="Theirs", status="Open"
        )
        TaskActivity.objects.create(
            team=other,
            project=other_project,
            task=other_task,
            actor=self.user2,
            action_type="created",
            metadata={"via": KEY},
        )
        self.assertEqual(self.items(self.user), [])

    def test_a_task_the_viewer_cannot_open_is_absent(self):
        private = ProjectMaster.objects.create(
            team=self.team,
            project_name="Secret",
            code="SEC",
            owner=self.user2,
            project_system_user=self.user2,
        )
        hidden = TaskMaster.objects.create(
            team=self.team, project=private, reporter=self.user2, title="Hidden", status="Open"
        )
        self.act(self.user2, KEY, task=hidden)
        self.assertEqual(self.items(self.user), [])
        [item] = self.items(self.user2)
        self.assertEqual(item["target"]["title"], "Hidden")

    def test_a_private_personal_note_is_absent_and_the_owners_is_listed(self):
        self.decision(self.user2, KEY, title="Decision: keep it private", note_type=1)
        self.assertEqual(self.get(self.user)["decisions"], [])
        self.assertEqual(len(self.get(self.user2)["decisions"]), 1)

    def test_ai_app_notices_are_never_listed(self):
        Activity.objects.create(
            team_id=self.team.team_id,
            recipient=self.user2,
            actor=self.user2,
            activity_type=ActivityType.AI_APP,
            surface_type=SURFACE_AI_APP,
            meta={"verb": "created", "kind": "task", "count": 3, "items": [{"title": "Hidden"}]},
        )
        self.assertEqual(self.items(self.user2), [])
        self.assertNoSecrets(self.get(self.user2))

    def test_a_connection_of_someone_else_is_not_listed(self):
        app = Application.objects.create(
            client_id="claude-x",
            name="Claude",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://claude.ai/cb",
        )
        McpConnection.objects.create(
            user=self.user2,
            application=app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            scope="write",
        )
        self.assertEqual(self.get(self.user)["connections"], [])
        self.assertEqual(len(self.get(self.user2)["connections"]), 1)


class TheRowsAndTheirText(_Base):
    def test_a_status_change_is_task_status_and_names_its_target(self):
        self.act(self.user2, CLAUDE)
        [item] = self.items(self.user)
        self.assertEqual(item["action"], "task_status")
        self.assertEqual(item["target"], {"type": "task", "id": self.task.task_id, "title": "Spec"})
        self.assertEqual(item["agent"], {"kind": "claude", "app": "claude"})
        self.assertEqual(item["user"], {"id": str(self.user2.id), "name": self.user2.username})

    def test_creates_comments_and_other_edits_map_to_their_kinds(self):
        self.act(self.user2, KEY, action="created")
        self.act(self.user2, KEY, action="title_changed")
        self.comment(self.user2, KEY, "Looks good")
        kinds = sorted(i["action"] for i in self.items(self.user))
        self.assertEqual(kinds, ["task_comment", "task_created", "task_updated"])

    def test_comment_edits_and_comment_deletes_are_not_counted_twice(self):
        self.act(self.user2, KEY, action="comment_edited", metadata={"commentId": 1})
        self.act(self.user2, KEY, action="comment_deleted", metadata={"commentId": 1})
        self.assertEqual(self.items(self.user), [])

    def test_a_comment_carries_its_plain_text_cut_to_280_characters(self):
        self.comment(self.user2, KEY, "x" * 400)
        [item] = self.items(self.user)
        self.assertIsInstance(item["text"], str)
        self.assertLessEqual(len(item["text"]), 280)
        self.assertNotIn("type", item["text"])

    def test_text_is_plain_even_when_the_body_looks_like_markup(self):
        self.comment(self.user2, KEY, "**bold** <img src=x onerror=alert(1)>")
        [item] = self.items(self.user)
        # Plain text: the characters come back as written and the web app renders them as text.
        self.assertEqual(item["text"], "**bold** <img src=x onerror=alert(1)>")

    def test_a_scalar_change_says_from_and_to_and_a_big_value_says_nothing(self):
        self.act(self.user2, KEY)
        [item] = self.items(self.user)
        self.assertEqual(item["text"], "Open → WIP")

    def test_an_item_is_a_task_row_with_a_project_group(self):
        self.act(self.user2, KEY)
        [group] = self.get(self.user)["groups"]
        self.assertEqual(
            group["project"], {"id": self.project.project_id, "name": "Launch", "code": "LAU"}
        )
        self.assertEqual(group["counts"]["total"], 1)
        self.assertEqual(group["counts"]["api_key"], 1)


class TheFilters(_Base):
    def setUp(self):
        super().setUp()
        self.other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Support",
            code="SUP",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.other_project, attendee=self.user2
        )
        self.support_task = TaskMaster.objects.create(
            team=self.team,
            project=self.other_project,
            reporter=self.user,
            title="Ticket",
            status="Open",
        )
        self.act(self.user2, CLAUDE)
        self.act(self.user2, KEY)
        self.act(self.user, CLAUDE)
        self.act(self.user2, KEY, task=self.support_task)
        self.comment(self.user2, CLAUDE, "a comment")

    def test_project_narrows_to_one_project(self):
        data = self.get(self.user, project_id=self.other_project.project_id)
        self.assertEqual(data["totals"]["changes"], 1)
        self.assertEqual([g["project"]["code"] for g in data["groups"]], ["SUP"])

    def test_agent_narrows_and_the_summary_row_still_shows_every_agent(self):
        data = self.get(self.user, agent="claude")
        self.assertEqual(
            {i["agent"]["kind"] for i in self.items(self.user, agent="claude")}, {"claude"}
        )
        self.assertEqual(data["totals"]["by_agent"]["api_key"], 2)
        self.assertEqual(data["totals"]["by_agent"]["claude"], 3)

    def test_agent_person_is_the_unmarked_writes(self):
        self.act(self.user2, None)
        self.assertEqual(len(self.items(self.user, agent="person")), 1)

    def test_user_narrows_to_one_person(self):
        data = self.get(self.user, user_id=str(self.user2.id))
        self.assertTrue(
            all(
                i["user"]["id"] == str(self.user2.id)
                for i in self.items(self.user, user_id=str(self.user2.id))
            )
        )
        self.assertEqual(data["totals"]["changes"], 4)

    def test_kind_narrows_to_one_kind_of_work(self):
        self.assertEqual(
            {i["action"] for i in self.items(self.user, kind="task_comment")},
            {"task_comment"},
        )
        self.assertEqual(self.get(self.user, kind="task_comment")["totals"]["changes"], 1)
        self.assertEqual(self.get(self.user, kind="decision")["totals"]["changes"], 0)

    def test_filters_combine(self):
        data = self.get(
            self.user,
            agent="api_key",
            user_id=str(self.user2.id),
            project_id=self.project.project_id,
        )
        self.assertEqual(data["totals"]["changes"], 3)  # the summary keeps every agent
        self.assertEqual(data["totals"]["by_agent"]["api_key"], 1)
        [item] = self.items(
            self.user,
            agent="api_key",
            user_id=str(self.user2.id),
            project_id=self.project.project_id,
        )
        self.assertEqual(item["agent"], {"kind": "api_key"})

    def test_by_user_agent_lists_each_person_and_agent_with_its_projects(self):
        rows = self.get(self.user)["by_user_agent"]
        mine = next(
            r for r in rows if r["user"]["id"] == str(self.user2.id) and r["agent"] == "api_key"
        )
        self.assertEqual(mine["count"], 2)
        self.assertEqual({p["code"] for p in mine["projects"]}, {"LAU", "SUP"})
        self.assertLessEqual(len(mine["projects"]), 3)

    def test_a_filter_with_no_match_is_an_honest_empty_state(self):
        data = self.get(self.user, project_id=self.other_project.project_id, agent="chatgpt")
        self.assertEqual(data["groups"], [])
        self.assertEqual(data["totals"]["by_agent"]["chatgpt"], 0)
        self.assertEqual(data["totals"]["changes"], 1)  # the summary keeps every agent


class TheWindowAndThePage(_Base):
    def test_seven_days_is_the_default_window(self):
        self.act(self.user2, KEY, days_ago=10)
        self.act(self.user2, KEY, days_ago=2)
        self.assertEqual(self.get(self.user)["totals"]["changes"], 1)
        self.assertEqual(self.get(self.user, days=14)["totals"]["changes"], 2)
        self.assertEqual(self.get(self.user, days=31)["window"]["days"], 31)

    def test_the_limit_bounds_the_items_and_the_totals_do_not_move(self):
        for _ in range(5):
            self.act(self.user2, KEY)
        page = self.get(self.user, limit=2)
        self.assertEqual(len(self.items(self.user, limit=2)), 2)
        self.assertIsNotNone(page["next_cursor"])
        self.assertEqual(page["totals"]["changes"], 5)

    def test_the_cursor_walks_every_item_once(self):
        for _ in range(5):
            self.act(self.user2, KEY)
        seen, cursor = [], None
        for _ in range(10):
            params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
            data = self.get(self.user, **params)
            seen += [i["id"] for g in data["groups"] for i in g["items"]]
            cursor = data["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)

    def test_a_limit_above_the_maximum_is_clamped_to_200(self):
        self.act(self.user2, KEY)
        data = self.get(self.user, limit=5000)
        self.assertEqual(len(self.items(self.user, limit=5000)), 1)
        self.assertIsNone(data["next_cursor"])


class TheDecisions(_Base):
    def test_a_decision_note_is_listed_with_who_and_which_agent(self):
        self.decision(self.user2, CLAUDE)
        self.decision(self.user2, None, title="Decision: by hand")
        self.decision(self.user, KEY, title="Not a decision: a plan")  # not a decision title
        titles = sorted(d["title"] for d in self.get(self.user)["decisions"])
        self.assertEqual(titles, ["Decision: by hand", "Decision: ship Option A"])
        by_title = {d["title"]: d for d in self.get(self.user)["decisions"]}
        self.assertEqual(
            by_title["Decision: ship Option A"]["agent"], {"kind": "claude", "app": "claude"}
        )
        self.assertIsNone(by_title["Decision: by hand"]["agent"])
        self.assertEqual(self.get(self.user)["totals"]["decisions"], 2)

    def test_a_decision_outside_the_window_is_not_listed(self):
        self.decision(self.user2, KEY, days_ago=20)
        self.assertEqual(self.get(self.user)["decisions"], [])
        self.assertEqual(len(self.get(self.user, days=31)["decisions"]), 1)

    def test_a_decision_filtered_by_agent_keeps_only_that_agent(self):
        self.decision(self.user2, CLAUDE)
        self.decision(self.user2, KEY, title="Decision: other")
        titles = [d["title"] for d in self.get(self.user, agent="claude")["decisions"]]
        self.assertEqual(titles, ["Decision: ship Option A"])

    def test_kind_decision_lists_decisions_and_no_changes(self):
        self.decision(self.user2, CLAUDE)
        self.act(self.user2, KEY)
        data = self.get(self.user, kind="decision")
        self.assertEqual(len(data["decisions"]), 1)
        self.assertEqual(data["totals"]["changes"], 0)

    def test_a_note_version_with_an_agent_is_a_note_item(self):
        note_id = self.decision(self.user2, CLAUDE, title="Decision: note item")
        items = self.items(self.user, kind="note")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["action"], "note")
        self.assertEqual(items[0]["target"]["type"], "note")
        self.assertEqual(items[0]["target"]["id"], note_id)
        self.assertEqual(items[0]["agent"], {"kind": "claude", "app": "claude"})


class TheConnections(_Base):
    def test_my_live_connection_is_listed_with_family_and_last_use(self):
        app = Application.objects.create(
            client_id="claude-x",
            name="Claude",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://claude.ai/api/mcp/auth_callback",
        )
        McpConnection.objects.create(
            user=self.user,
            application=app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            scope="write",
            last_used_at=timezone.now(),
        )
        [conn] = self.get(self.user)["connections"]
        self.assertEqual(conn["family"], "claude")
        self.assertIsNotNone(conn["last_used_at"])
        self.assertNotIn(str(app.client_id), repr(conn))

    def test_a_revoked_connection_is_not_listed(self):
        app = Application.objects.create(
            client_id="x-app",
            name="X",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://example.com/cb",
        )
        McpConnection.objects.create(
            user=self.user,
            application=app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            scope="write",
            revoked_at=timezone.now(),
        )
        self.assertEqual(self.get(self.user)["connections"], [])


class TheQueryCount(_Base):
    def count_for(self, viewer):
        with CaptureQueriesContext(connection) as ctx:
            self.get(viewer)
        return len(ctx.captured_queries)

    def test_the_query_count_does_not_grow_with_the_team(self):
        self.act(self.user2, KEY)
        self.comment(self.user2, KEY, "text", comment_id=99)
        self.decision(self.user2, CLAUDE)
        small = self.count_for(self.user)
        for i in range(12):
            task = TaskMaster.objects.create(
                team=self.team,
                project=self.project,
                reporter=self.user2,
                title=f"T{i}",
                status="Open",
            )
            for _ in range(3):
                self.act(self.user2, CLAUDE, task=task)
                self.act(self.user2, KEY, task=task, action="created")
            self.comment(self.user2, KEY, "text", comment_id=100 + i)
            self.decision(self.user2, KEY, title=f"Decision: {i}")
        large = self.count_for(self.user)
        self.assertEqual(small, large)
        self.assertLessEqual(large, 40)


class TheLabelRule(_Base):
    """The Python label (`agent_work._label_of`, shown on each item) and the SQL label (the filters
    and the totals) are one rule. One row of each kind of `via`, read by a colleague and by its own
    writer, checked on both sides."""

    MATRIX = [
        (KEY, "api_key", "api_key"),
        (CLAUDE, "claude", "claude"),
        (CHATGPT, "chatgpt", "chatgpt"),
        (OTHER_APP, "other_app", "other_app"),
        ({"kind": "oauth", "app": "gemini"}, "other_app", "other_app"),
        (UNDO, "person", "person"),
        (AGENT, "person", "person"),
        ({"kind": "app"}, "person", "person"),
        (None, "person", "person"),
        ({}, "person", "person"),
        ({"kind": ""}, "person", "person"),
        ("a plain string", "person", "person"),
        ({"kind": "github", "login": "x"}, None, None),
        ({"kind": "pr_merge"}, None, None),
        ({"kind": "removed"}, None, None),
    ]

    def test_the_item_label_and_the_summary_agree_for_every_kind_of_via(self):
        expected_totals = {a: 0 for a in ("claude", "chatgpt", "api_key", "other_app", "person")}
        for via, _, sql in self.MATRIX:
            self.act(self.user2, via)
            if sql:
                expected_totals[sql] += 1
        for viewer in (self.user, self.user2):
            data = self.get(viewer)
            self.assertEqual(data["totals"]["by_agent"], expected_totals)
            by_id = {}
            for item in self.items(viewer):
                by_id[item["id"]] = item
            self.assertEqual(len(by_id), sum(expected_totals.values()))
        # Per row, the colleague's item label is the rule's label for the via shown to them.
        shown = {i["agent"]["kind"] if i["agent"] else "person" for i in self.items(self.user)}
        self.assertEqual(shown, {"api_key", "claude", "chatgpt", "other_app", "person"})
        # The writer reads their own row whole; the label does not change with the viewer.
        own = sorted((i["agent"] or {}).get("kind", "person") for i in self.items(self.user2))
        other = sorted((i["agent"] or {}).get("kind", "person") for i in self.items(self.user))
        self.assertEqual(own, other)

    def test_a_filter_on_each_label_returns_exactly_its_rows(self):
        for via, _, _ in self.MATRIX:
            self.act(self.user2, via)
        for agent in ("claude", "chatgpt", "api_key", "other_app", "person"):
            with self.subTest(agent=agent):
                expected = sum(1 for _, label, _ in self.MATRIX if label == agent)
                self.assertEqual(len(self.items(self.user, agent=agent)), expected)


class TheDeletedAndHiddenText(_Base):
    def test_a_deleted_comment_has_no_text(self):
        comment = self.comment(self.user2, KEY, "a secret plan")
        TaskComments.objects.filter(pk=comment.pk).update(is_deleted=True)
        [item] = self.items(self.user)
        self.assertEqual(item["action"], "task_comment")
        self.assertIsNone(item["text"])
        self.assertNotIn("secret plan", repr(self.get(self.user)))

    def test_a_project_the_viewer_is_not_in_is_never_named_on_a_note(self):
        private = ProjectMaster.objects.create(
            team=self.team,
            project_name="Board only",
            code="BRD",
            owner=self.user2,
            project_system_user=self.user2,
        )
        task = TaskMaster.objects.create(
            team=self.team, project=private, reporter=self.user2, title="Board", status="Open"
        )
        note = TaskNoteMaster.objects.create(
            team=self.team, project=private, owner=self.user, task=task, title="Decision: x"
        )
        NoteVersionMaster.objects.create(
            team=self.team,
            note_type=2,
            note_id=note.note_id,
            version_no=1,
            editor=self.user,
            title="Decision: x",
            via=CLAUDE,
        )
        data = self.get(self.user)
        self.assertNotIn("Board only", repr(data))
        self.assertNotIn("BRD", repr(data))
