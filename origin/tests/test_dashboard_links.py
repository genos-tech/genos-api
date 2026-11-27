"""Links on a dashboard page (MIP-2417): the server builds them from ids it has checked.

What is pinned, each by a test that fails when its guard is removed:

  * a path is made only from a database row, and only when EVERY reader of the page may open it;
  * the renderer writes only the app's own paths, whatever the data holds, and escapes the words;
  * a skill's page links only what its run READ from Genos's tools and its owner may open; a made
    up id, a URL or an `href` in the answer gives no link;
  * only a page the server drew is served with the link script (and the stricter policy), and only
    the delivery of a dashboard can mark one: any other save of the page takes the mark away;
  * the chat notice, the Inbox rows and the AI cache fingerprint are what they were before links.
"""

from __future__ import annotations

import json
import uuid
from unittest import mock

from django.test import SimpleTestCase

from origin.models.common.schedule_models import STATUS_OK
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.html_note_models import NoteHtmlRevision
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services import dashboard_links, dashboard_page, html_note_pages, html_notes
from origin.services.collab_push import PushOutcome
from origin.services.dashboard_links import Ref
from origin.services.schedule_jobs import ai_dashboard, skill_dashboard
from origin.tests.test_html_note_pages import EXPECTED_CSP, PageBase
from origin.tests.test_schedule_dashboard_skill import DashBase
from origin.tests.test_schedule_job_ai_dashboard import PUSH
from origin.tests.test_schedule_jobs import JobBase
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL


def task_path(task):
    return f"/workspace/tasks/project/{task.project_id}/task/{task.task_id}"


class PathsTests(JobBase):
    def paths(self, *refs, readers=None):
        return dashboard_links.paths_for(self.ctx, refs, readers or {str(self.user.id)})

    def milestone(self, project=None):
        project = project or self.project
        backing = self.make_task("Milestone backing", project=project, is_milestone=True)
        return MilestoneMaster.objects.create(
            team=self.team, project=project, task=backing, title="M1", reporter=self.user
        )

    def test_a_task_a_milestone_and_a_project_the_reader_may_open(self):
        task, milestone = self.make_task("One"), self.milestone()
        refs = (
            Ref("task", task.task_id),
            Ref("milestone", milestone.milestone_id),
            Ref("project", self.project.project_id),
        )
        self.assertEqual(
            self.paths(*refs),
            {
                refs[0]: task_path(task),
                refs[1]: (
                    f"/workspace/tasks/project/{self.project.project_id}/milestone/"
                    f"{milestone.milestone_id}"
                ),
                refs[2]: f"/workspace/tasks/project/{self.project.project_id}",
            },
        )

    def test_one_reader_who_may_not_open_it_takes_the_link_away_from_the_page(self):
        task, milestone = self.make_task("One", assignee=self.user, reporter=self.user), None
        milestone = self.milestone()
        stranger = str(uuid.uuid4())
        self.assertEqual(
            self.paths(
                Ref("task", task.task_id),
                Ref("milestone", milestone.milestone_id),
                Ref("project", self.project.project_id),
                readers={str(self.user.id), stranger},
            ),
            {},
        )

    def test_ids_that_name_nothing_and_a_deleted_task_get_no_link(self):
        gone = self.make_task("Gone", is_deleted=True)
        self.assertEqual(
            self.paths(
                Ref("task", 999999),
                Ref("task", gone.task_id),
                Ref("milestone", 999999),
                Ref("project", 999999),
                Ref("note", 999999, "personal"),
            ),
            {},
        )

    def test_no_reader_no_link(self):
        task = self.make_task("One")
        self.assertEqual(
            dashboard_links.paths_for(self.ctx, [Ref("task", task.task_id)], set()), {}
        )

    def personal_note(self, folder=None, **extra):
        return PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="n",
            body=[],
            folder_id=folder.folder_id if folder else None,
            **extra,
        )

    def folder(self, scope):
        return PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name=f"f {scope}", scope=scope
        )

    def test_my_note_a_team_folder_note_and_a_note_under_one(self):
        mine = self.personal_note()
        filed = self.personal_note(self.folder(PersonalNoteFolder.SCOPE_TEAM))
        child = self.personal_note(parent_note_id=filed.note_id)
        with mock.patch("origin.services.dashboard_links.may_read_note", return_value=True):
            got = self.paths(*(Ref("note", n.note_id, "personal") for n in (mine, filed, child)))
        self.assertEqual(
            sorted(got.values()),
            sorted(
                [
                    f"/workspace/notes/my/{mine.note_id}",
                    f"/workspace/notes/team/{filed.note_id}",
                    f"/workspace/notes/team/{child.note_id}",
                ]
            ),
        )

    def test_a_task_note_and_a_chat_note(self):
        task = self.make_task("Has a note")
        task_note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=self.project, task=task, title="tn"
        )
        channel = uuid.uuid4()
        chat_note = ChatNoteMaster(note_id=7, chat_type=2, channel_id=channel, owner=self.user)
        with mock.patch("origin.services.dashboard_links.may_read_note", return_value=True):
            got = self.paths(Ref("note", task_note.note_id, "task"))
            with mock.patch.object(ChatNoteMaster.objects, "filter") as found:
                found.return_value.first.return_value = chat_note
                got |= self.paths(Ref("note", 7, "chat"))
        self.assertEqual(
            sorted(got.values()),
            [
                f"/workspace/notes/chat/gm/{channel}/thread/0/note/7",
                f"/workspace/notes/task/project/{self.project.project_id}/task/{task.task_id}"
                f"/note/{task_note.note_id}",
            ],
        )

    def test_a_note_the_reader_may_not_read_gets_no_link(self):
        note = self.personal_note()
        self.assertEqual(
            self.paths(Ref("note", note.note_id, "personal"), readers={str(self.user2.id)}), {}
        )

    def test_a_skills_body_note_gets_no_link(self):
        note = self.personal_note(self.folder(PersonalNoteFolder.SCOPE_SKILL))
        self.assertEqual(self.paths(Ref("note", note.note_id, "personal")), {})


class RefTests(SimpleTestCase):
    def test_only_a_kind_a_positive_id_and_a_notes_family_are_read(self):
        clean = dashboard_links.clean_ref
        self.assertEqual(clean({"kind": "task", "id": 5, "href": "https://x"}), Ref("task", 5))
        self.assertEqual(clean({"kind": "task", "id": "5"}), Ref("task", 5))
        self.assertEqual(
            clean({"kind": "note", "id": 5, "note_type": "team"}), Ref("note", 5, "personal")
        )
        self.assertEqual(clean({"kind": "note", "id": 5, "note_type": 2}), Ref("note", 5, "task"))
        for bad in (
            None,
            "task:5",
            {"kind": "task"},
            {"kind": "task", "id": -1},
            {"kind": "task", "id": 0},
            {"kind": "task", "id": True},
            {"kind": "task", "id": 1.5},
            {"kind": "task", "id": "1/../2"},
            {"kind": "task", "id": 2**70},
            {"kind": "chat", "id": 1},
            {"kind": "note", "id": 5},
        ):
            self.assertIsNone(clean(bad), repr(bad))

    def test_what_a_run_read_comes_from_the_results_of_genos_tools_only(self):
        seen: set[Ref] = set()
        dashboard_links.refs_in_result(
            "list_tasks",
            {
                "tasks": [
                    {
                        "task_id": 1,
                        "parent_task_id": 2,
                        "milestone_id": 3,
                        "project_id": 4,
                        "custom_fields": {"task_id": 66},
                        "title": "task_id 77",
                    }
                ],
                "matches": [{"note_id": 9, "note_type": 1}, {"note_id": 10}],
            },
            seen,
        )
        for name in ("fetch_pr", "list_calendar_events", "search_web"):
            dashboard_links.refs_in_result(name, {"milestone_id": 88, "project_id": 89}, seen)
        self.assertEqual(
            seen,
            {
                Ref("task", 1),
                Ref("task", 2),
                Ref("milestone", 3),
                Ref("project", 4),
                Ref("note", 9, "personal"),
            },
        )

    def test_the_renderers_paths(self):
        for good in (
            "/workspace/tasks/project/1",
            "/workspace/tasks/project/1/task/2",
            "/workspace/tasks/project/1/milestone/2",
            "/workspace/notes/my/1",
            "/workspace/notes/task/project/1/task/2/note/3",
            f"/workspace/notes/chat/dm/{uuid.uuid4()}/thread/0/note/3",
        ):
            self.assertTrue(dashboard_links.is_app_path(good), good)
        for bad in (
            "https://evil.example/workspace/tasks/project/1",
            "//evil.example",
            "javascript:alert(1)",
            "/workspace/../admin",
            "/workspace/tasks/project/1/../2",
            "/workspace/tasks/project/1?next=//evil",
            "/workspace/tasks/project/1#x",
            "/workspace/tasks/project/1\n",
            "/workspace/settings",
            "/workspace/tasks/project/%31",
            None,
        ):
            self.assertFalse(dashboard_links.is_app_path(bad), repr(bad))


class RenderTests(SimpleTestCase):
    def page(self, **extra):
        dash = {
            "title": "T",
            "kind": "skill",
            "subtitle": "s",
            "stats": [{"label": "tasks", "value": "1", "href": "javascript:alert(1)"}],
            "lists": [
                {
                    "heading": "h",
                    "rows": [
                        "an old plain row",
                        {"text": "<b>mine</b>", "href": "/workspace/tasks/project/1/task/2"},
                        {"text": "outside", "href": "https://evil.example"},
                        {"text": "broken", "href": '/workspace/notes/my/1"><script>x</script>'},
                    ],
                }
            ],
        }
        dash.update(extra)
        return dashboard_page.render(dash)[0]

    def test_only_the_apps_paths_become_links_and_every_word_is_escaped(self):
        html = self.page()
        self.assertEqual(html.count("<a "), 1)
        self.assertIn(
            '<a href="/workspace/tasks/project/1/task/2">&lt;b&gt;mine&lt;/b&gt;</a>', html
        )
        self.assertIn('title="an old plain row">an old plain row</div>', html)
        self.assertNotIn("evil.example", html)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("<script", html)

    def test_the_title_and_a_stat_may_be_links(self):
        html = self.page(
            subject={"href": "/workspace/tasks/project/1"},
            stats=[{"label": "open", "value": "2", "href": "/workspace/tasks/project/1"}],
        )
        self.assertIn('<h1><a href="/workspace/tasks/project/1">T</a></h1>', html)
        self.assertIn(
            '<li><a href="/workspace/tasks/project/1"><b>2</b><span>open</span></a></li>', html
        )
        self.assertEqual(html_notes.scan_html(html).warnings, [])


class BuiltInTests(JobBase):
    def test_a_project_dashboard_links_its_project_and_every_task_it_names(self):
        done = self.make_task("Done one", status="Closed")
        self.activity(done, self.user, "status_changed", "WIP", "Closed", at=self.ago(hours=3))
        report = self.run_job("ai_dashboard", kind="project", project_id=self.project.project_id)
        dash = report["dashboard"]
        self.assertEqual(
            dash["subject"]["href"], f"/workspace/tasks/project/{self.project.project_id}"
        )
        rows = [r for block in dash["lists"] for r in block["rows"]]
        self.assertTrue(rows)
        self.assertEqual([r["href"] for r in rows], [task_path(done)])
        # The Inbox and the chat read words, never the page's data.
        self.assertIn(f"{done.display_id} · Done one", self.texts(report))

    def test_a_milestone_dashboard_links_the_milestone_and_its_tasks(self):
        backing = self.make_task("M backing", is_milestone=True)
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, task=backing, title="M1", reporter=self.user
        )
        ready = self.make_task("Ready one", milestone=milestone)
        report = self.run_job("ai_dashboard", kind="milestone", milestone_id=milestone.milestone_id)
        dash = report["dashboard"]
        self.assertTrue(dash["subject"]["href"].endswith(f"/milestone/{milestone.milestone_id}"))
        hrefs = [r.get("href") for block in dash["lists"] for r in block["rows"]]
        self.assertIn(task_path(ready), hrefs)
        self.assertNotIn(None, hrefs)

    def test_my_progress_links_what_was_closed(self):
        done = self.make_task("Mine done", status="Closed")
        self.activity(done, self.user, "status_changed", "WIP", "Closed", at=self.ago(hours=3))
        dash = self.run_job("ai_dashboard", kind="mine", period="week")["dashboard"]
        self.assertIn(task_path(done), [r.get("href") for r in dash["lists"][0]["rows"]])

    def test_the_summary_line_and_the_fingerprint_are_what_they_were_before_links(self):
        linked = {
            "title": "Project dashboard: P",
            "kind": "project",
            "subtitle": "Last 30 days, as of 2026-11-27",
            "stats": [
                {"label": "tasks", "value": "3"},
                {"label": "closed", "value": "33%"},
                {"label": "overdue", "value": "0"},
            ],
            "series": [],
            "lists": [
                {
                    "heading": "Recently closed",
                    "rows": [{"text": "P-1 · A", "ref": {"kind": "task", "id": 1}, "href": "/x"}],
                }
            ],
            "subject": {"ref": {"kind": "project", "id": 1}},
        }
        plain = {**linked, "lists": [{"heading": "Recently closed", "rows": ["P-1 · A"]}]}
        del plain["subject"]
        report = ai_dashboard.report_of(linked)
        self.assertEqual(
            report["summary"],
            "Last 30 days, as of 2026-11-27: 3 tasks, 33% closed, 0 overdue.",
        )
        self.assertEqual(report["sections"], ai_dashboard.report_of(plain)["sections"])
        self.assertEqual(
            ai_dashboard.data_fingerprint(linked), ai_dashboard.data_fingerprint(plain)
        )


class SkillPageTests(DashBase):
    def run_page(self, answer, results=()):
        """One run of a skill dashboard whose model was shown `results` and answered `answer`."""
        scripted = self.agent

        def run_agent(query, ctx, emit, **kwargs):
            for name, result in results:
                kwargs["trace_hook"](name, {}, result)
            scripted.answer = answer if isinstance(answer, str) else json.dumps(answer)
            return scripted(query, ctx, emit, **kwargs)

        sched = self.dash(self.skill())
        with mock.patch("origin.search_engine.agent.controller.run_agent", run_agent):
            status, error = self.execute(sched)
        self.assertEqual((status, error), (STATUS_OK, ""))
        note = self.notes().get()
        return html_notes.latest_revision(NOTE_TYPE_PERSONAL, note.note_id)

    def task(self, owner_reads=True):
        project = self.project if owner_reads else self.hidden_project()
        return TaskMaster.objects.create(
            team=self.team, project=project, title="t", status="Open", project_task_number=1
        )

    def hidden_project(self):
        from origin.models.project.prj_models import ProjectMaster  # noqa: PLC0415

        return ProjectMaster.objects.create(
            team=self.team, project_name="Hidden", owner=self.user2, project_system_user=self.user2
        )

    def setUp(self):
        super().setUp()
        from origin.models.project.prj_models import ProjectMaster, ProjectMembers  # noqa: PLC0415

        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Mine", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def answer(self, *rows, **extra):
        return {"title": "P", "lists": [{"heading": "h", "rows": list(rows)}], **extra}

    def test_a_row_about_a_task_the_run_read_is_a_link(self):
        task = self.task()
        revision = self.run_page(
            self.answer(
                {"text": "Read it", "ref": {"kind": "task", "id": task.task_id}},
                subject=None,
                ref={"kind": "project", "id": self.project.project_id},
            ),
            results=[
                ("fetch_task", {"task_id": task.task_id, "project_id": self.project.project_id})
            ],
        )
        self.assertIn(f'<a href="{task_path(task)}">Read it</a>', revision.html)
        self.assertIn(
            f'<h1><a href="/workspace/tasks/project/{self.project.project_id}">P</a></h1>',
            revision.html,
        )
        self.assertTrue(revision.server_rendered)

    def test_a_task_the_run_did_not_read_is_words_only_even_when_the_owner_may_open_it(self):
        task = self.task()
        revision = self.run_page(
            self.answer({"text": "Guessed", "ref": {"kind": "task", "id": task.task_id}})
        )
        self.assertIn('title="Guessed">Guessed</div>', revision.html)
        self.assertNotIn("<a ", revision.html)

    def test_a_task_the_owner_may_not_open_is_words_only_even_when_a_result_named_its_id(self):
        task = self.task(owner_reads=False)
        revision = self.run_page(
            self.answer({"text": "Hidden", "ref": {"kind": "task", "id": task.task_id}}),
            results=[("list_tasks", {"tasks": [{"task_id": task.task_id}]})],
        )
        self.assertNotIn("<a ", revision.html)

    def test_a_url_a_path_or_markup_in_the_answer_is_never_a_link(self):
        task = self.task()
        revision = self.run_page(
            self.answer(
                {"text": "See https://evil.example/x", "href": task_path(task)},
                {"text": '<a href="https://evil.example">tap</a>', "url": "https://evil.example"},
                {"text": "ref as words", "ref": "https://evil.example"},
                {"text": "ref with a path", "ref": {"kind": "task", "id": task_path(task)}},
                "[tap](https://evil.example)",
            ),
            results=[("fetch_task", {"task_id": task.task_id})],
        )
        self.assertNotIn("<a ", revision.html)
        self.assertNotIn('href="', revision.html)
        self.assertIn("&lt;a href=&quot;https://evil.example&quot;&gt;", revision.html)

    def test_parsing_keeps_a_wellformed_ref_and_nothing_else(self):
        dash = skill_dashboard.parse_dashboard(
            json.dumps(
                {
                    "stats": [
                        {"label": "a", "value": "1", "href": "x", "ref": {"kind": "task", "id": 3}}
                    ],
                    "lists": [{"heading": "h", "rows": [{"text": "t", "href": "x"}, {"ref": 1}]}],
                    "subject": {"href": "https://evil.example"},
                    "href": "https://evil.example",
                }
            ),
            default_title="d",
        )
        self.assertEqual(
            dash["stats"], [{"label": "a", "value": "1", "ref": {"kind": "task", "id": 3}}]
        )
        self.assertEqual(dash["lists"], [{"heading": "h", "rows": ["t"]}])
        self.assertNotIn("subject", dash)
        self.assertNotIn("href", dash)

    def test_the_model_is_told_how_to_point_at_a_thing_and_never_to_write_an_address(self):
        self.run_page(self.answer("x"))
        told = self.system_extra()
        self.assertIn('"ref"', told)
        self.assertIn("Never write a URL", told)


class ServedPageTests(PageBase):
    def mark(self, value=True):
        NoteHtmlRevision.objects.filter(
            note_type=NOTE_TYPE_PERSONAL, note_id=self.personal.note_id
        ).update(server_rendered=value)

    def served(self):
        grant = self.grant_for()
        res = self.page(grant["path"])
        self.assertEqual(res.status_code, 200)
        return grant, res

    def test_an_ordinary_page_is_served_as_before_and_takes_no_links(self):
        grant, res = self.served()
        self.assertFalse(grant["links"])
        self.assertEqual(res["Content-Security-Policy"], EXPECTED_CSP)
        self.assertNotIn(html_note_pages.OPEN_KEY, res.content.decode())

    def test_a_server_drawn_page_runs_only_the_script_with_this_loads_nonce(self):
        self.mark()
        grant, res = self.served()
        self.assertTrue(grant["links"])
        token = grant["path"].removeprefix("/h/")
        script_nonce = html_note_pages.script_nonce_for(token)
        self.assertNotEqual(script_nonce, grant["nonce"])
        policy = res["Content-Security-Policy"]
        self.assertEqual(
            policy,
            EXPECTED_CSP.replace(
                "script-src 'unsafe-inline'", f"script-src 'nonce-{script_nonce}'"
            ),
        )
        self.assertIn("sandbox allow-scripts;", policy)
        body = res.content.decode()
        self.assertEqual(body.count(f'<script nonce="{script_nonce}">'), 1)
        # The page's own script (this test page has one) carries no nonce, so the policy stops it.
        self.assertNotIn("nonce=", body[body.index("</script>") :])
        self.assertIn(html_note_pages.OPEN_KEY, body)
        self.assertIn("if(!e.isTrusted)return", body)
        self.assertIn('h.indexOf("/workspace/")!==0', body)

    def test_any_other_save_of_the_page_takes_the_mark_away(self):
        self.mark()
        prepared = html_notes.prepare("<title>t</title><a href='/workspace/'>x</a>", summary="s")
        with mock.patch(PUSH, return_value=PushOutcome.SKIPPED):
            html_notes.replace_page(
                note_type=NOTE_TYPE_PERSONAL,
                note=self.personal,
                user=self.user,
                prepared=prepared,
                keep_title=True,
            )
        grant, res = self.served()
        self.assertFalse(grant["links"])
        self.assertEqual(res["Content-Security-Policy"], EXPECTED_CSP)

    def test_a_caller_cannot_ask_for_the_mark_through_the_save_arguments(self):
        from origin.search_engine.agent.tools.base import ToolContext  # noqa: PLC0415

        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        with (
            mock.patch("origin.search_engine.quota.get_effective_tier", return_value="core"),
            mock.patch(PUSH, return_value=PushOutcome.SKIPPED),
        ):
            saved = html_notes.save_page(
                {
                    "html": "<title>t</title><p>x</p>",
                    "summary": "s",
                    "note_type": "personal",
                    "server_rendered": True,
                },
                ctx,
            )
        revision = html_notes.latest_revision(NOTE_TYPE_PERSONAL, saved["note_id"])
        self.assertFalse(revision.server_rendered)
