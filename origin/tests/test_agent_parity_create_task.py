"""`create_task` takes what the app's create form sets (MIP-1561, flag `AGENT_PARITY_TOOLS`).

The form sets a starting status, a reporter, links and a body template; the tool left every task
Open, reported by the caller, with no links and an empty body. With the flag on it takes `status`,
`reporter_id`, `links` and `template`, and a task sent with no body gets the project's default
template, as one made in the app does.

The core tests drive the same task through the app's own POST (`/api/v2/task/`) and through the tool
and compare where each ends up: the stored columns and the history rows the signals write. The rest
pin each guard. A refused call creates nothing, even when it carried other valid fields.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import override_settings

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTaskTemplate
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.blocknote_md import task_body_blocks
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.plan_task_bodies import BUILT_IN_HEADINGS

from .test_base import BaseAPITestCase

User = get_user_model()
PARITY = {"AGENT_PARITY_TOOLS": True}


def _text(text, **styles):
    return {"type": "text", "text": text, "styles": styles}


def _heading(text):
    return {"type": "heading", "props": {"level": 3}, "content": [_text(text)], "children": []}


def _para(*runs):
    return {"type": "paragraph", "content": list(runs), "children": []}


def _bullet(text):
    return {"type": "bulletListItem", "content": [_text(text)], "children": []}


# A member-authored template: a heading, the grey italic hint of the built-ins, more sections.
CUSTOM = [
    _heading("Context"),
    _para(_text("Why now?", italic=True, textColor="gray")),
    _heading("Done when"),
    _bullet("tests pass"),
]


def _activity(task, action_type):
    """What a history row says, minus who/when/how it arrived (`via` differs by design)."""
    return [
        (field, old, new, {k: v for k, v in (meta or {}).items() if k != "via"})
        for field, old, new, meta in TaskActivity.objects.filter(task=task, action_type=action_type)
        .order_by("activity_id")
        .values_list("field_name", "old_value", "new_value", "metadata")
    ]


class ParityCreateBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, PARITY)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No test reaches GitHub: the webhook registration is the app's, mocked at its seam.
        hook = mock.patch("origin.search_engine.agent.tools.task_links.ensure_webhooks_for_links")
        self.hook = hook.start()
        self.addCleanup(hook.stop)

        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code="WEB",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

        # A guest holds a project row and no team row (the shared-project model).
        self.guest = User.objects.create_user(
            username="guest", email="guest@example.com", password="x"
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.guest)
        # A stranger: another team entirely.
        self.stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="x"
        )
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=other_team, attendee=self.stranger)

    def create(self, **extra):
        args = {"title": "T", "project_id": self.project.project_id, **extra}
        return CREATE_TASK.run(args, self.ctx)

    def made(self, out):
        return TaskMaster.objects.get(task_id=out["task_id"])

    def ui_post(self, **over):
        payload = {
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": None,
            "reporter": str(self.user.id),
            "title": "T",
            "priority": None,
            "effort_level": None,
            "status": "Open",
            "content": None,
            "due_date": None,
            "links": None,
            "tags": [],
            "is_init_task": False,
            **over,
        }
        self.authenticate()
        res = self.client.post("/api/v2/task/", payload, format="json")
        self.assertEqual(res.status_code, 201, getattr(res, "data", None))
        return TaskMaster.objects.get(task_id=res.data["task"]["task_id"])

    def template(self, body=None, name="Design doc", project=None):
        return ProjectTaskTemplate.objects.create(
            team=self.team,
            project=project or self.project,
            template_name=name,
            body=CUSTOM if body is None else body,
        )

    def set_default(self, value):
        self.project.default_task_template = value
        self.project.save(update_fields=["default_task_template"])

    def assert_nothing_created(self, **extra):
        before = TaskMaster.objects.count()
        with self.assertRaises(ToolError) as caught:
            self.create(**extra)
        self.assertEqual(TaskMaster.objects.count(), before)
        return str(caught.exception)

    def body(self, out):
        return blocks_to_markdown(self.made(out).content)


class StatusAtCreateTests(ParityCreateBase):
    def test_each_status_matches_the_uis_post(self):
        for status in ("WIP", "Blocked", "Pending", "Closed"):
            with self.subTest(status=status):
                by_tool = self.made(self.create(title=f"twin {status}", status=status))
                by_ui = self.ui_post(title=f"twin {status}", status=status)

                self.assertEqual(by_tool.status, status)
                self.assertEqual(by_tool.status, by_ui.status)
                self.assertEqual(
                    _activity(by_tool, TaskActivityActionType.CREATED),
                    _activity(by_ui, TaskActivityActionType.CREATED),
                )
                self.assertEqual(
                    _activity(by_tool, TaskActivityActionType.STATUS),
                    _activity(by_ui, TaskActivityActionType.STATUS),
                )

    def test_the_result_and_summary_say_the_status_it_started_in(self):
        out = self.create(status="WIP")

        self.assertEqual(out["status"], "WIP")
        self.assertIn("status WIP", out["__summary__"])

    def test_no_status_is_open_as_before(self):
        out = self.create()

        self.assertEqual(out["status"], "Open")
        self.assertNotIn("status", out["__summary__"].split(":", 1)[1])
        self.assertEqual(self.made(out).status, "Open")

    def test_deleted_is_refused_and_nothing_is_created(self):
        message = self.assert_nothing_created(status="Deleted", priority="High")

        self.assertIn("update_task", message)

    def test_a_status_the_app_does_not_have_is_refused(self):
        message = self.assert_nothing_created(status="Done")

        self.assertIn("Open", message)
        self.assertIn("'Done'", message)


class ReporterAtCreateTests(ParityCreateBase):
    def test_no_reporter_is_the_caller_as_before(self):
        self.assertEqual(self.made(self.create()).reporter_id, self.user.id)

    def test_a_teammate_is_the_reporter_as_the_uis_post_stores_it(self):
        by_tool = self.made(self.create(title="twin", reporter_id=str(self.user2.id)))
        by_ui = self.ui_post(title="twin", reporter=str(self.user2.id))

        self.assertEqual(by_tool.reporter_id, self.user2.id)
        self.assertEqual(by_tool.reporter_id, by_ui.reporter_id)
        self.assertEqual(
            _activity(by_tool, TaskActivityActionType.CREATED),
            _activity(by_ui, TaskActivityActionType.CREATED),
        )

    def test_the_result_names_the_reporter_when_it_is_not_the_caller(self):
        self.assertEqual(
            self.create(reporter_id=str(self.user2.id))["reporter_id"], str(self.user2.id)
        )

    def test_a_guest_on_the_project_may_be_the_reporter(self):
        out = self.create(reporter_id=str(self.guest.id))

        self.assertEqual(self.made(out).reporter_id, self.guest.id)

    def test_someone_in_another_team_is_refused_in_the_words_of_a_missing_user(self):
        # The reporter is a read grant (`can_access_task` lets them open the task).
        outsider = self.assert_nothing_created(reporter_id=str(self.stranger.id))
        missing = self.assert_nothing_created(reporter_id="00000000-0000-0000-0000-000000000000")

        self.assertIn("not an active member of this team", outsider)
        self.assertEqual(
            outsider.replace(str(self.stranger.id), "X"),
            missing.replace("00000000-0000-0000-0000-000000000000", "X"),
        )

    def test_a_deleted_account_is_refused(self):
        self.user2.is_deleted = True
        self.user2.save(update_fields=["is_deleted"])

        message = self.assert_nothing_created(reporter_id=str(self.user2.id))

        self.assertIn("deleted", message)

    def test_a_refused_reporter_creates_nothing_even_with_valid_fields(self):
        self.assert_nothing_created(
            reporter_id=str(self.stranger.id), status="WIP", links=[{"url": "https://a.example"}]
        )


class LinksAtCreateTests(ParityCreateBase):
    LINKS = [
        {"url": "https://example.com/spec", "title": "Spec"},
        {"url": "https://github.com/genos-tech/genos-api/pull/9"},
    ]

    def test_links_are_stored_as_the_uis_post_stores_them(self):
        by_tool = self.made(self.create(title="tool", links=self.LINKS))
        by_ui = self.ui_post(
            title="ui",
            links=[
                {
                    "id": "link-1",
                    "url": "https://example.com/spec",
                    "title": "Spec",
                    "isGitHub": False,
                },
                {
                    "id": "link-2",
                    "url": "https://github.com/genos-tech/genos-api/pull/9",
                    "title": "https://github.com/genos-tech/genos-api/pull/9",
                    "isGitHub": True,
                },
            ],
        )

        def shape(task):
            return [{k: v for k, v in link.items() if k != "id"} for link in task.links]

        self.assertEqual(shape(by_tool), shape(by_ui))
        self.assertTrue(all(link["id"].startswith("link-") for link in by_tool.links))
        self.assertEqual(len({link["id"] for link in by_tool.links}), 2)

    def test_the_result_lists_the_links_as_stored(self):
        out = self.create(links=self.LINKS)

        self.assertEqual(
            out["links"],
            [
                {"title": "Spec", "url": "https://example.com/spec"},
                {
                    "title": "https://github.com/genos-tech/genos-api/pull/9",
                    "url": "https://github.com/genos-tech/genos-api/pull/9",
                },
            ],
        )

    def test_no_links_stays_null_and_empty_is_an_empty_list(self):
        self.assertIsNone(self.made(self.create()).links)
        self.assertEqual(self.made(self.create(links=[])).links, [])

    def test_a_bare_url_string_is_a_link(self):
        task = self.made(self.create(links=["https://example.com/a"]))

        self.assertEqual(task.links[0]["url"], "https://example.com/a")

    def test_a_repeated_url_is_stored_once(self):
        task = self.made(
            self.create(links=["https://example.com/a", {"url": "https://example.com/a"}])
        )

        self.assertEqual(len(task.links), 1)

    def test_only_web_links_are_accepted_and_nothing_is_created(self):
        for bad in (
            "javascript:alert(1)",
            "  JaVa\tScRiPt:alert(1)",
            "data:text/html,<script>1</script>",
            "ftp://example.com/x",
            "example.com/no-scheme",
            "/relative/path",
            "https://",
            "https://exa mple.com",
            "",
        ):
            with self.subTest(url=bad):
                self.assert_nothing_created(links=[{"url": bad}], priority="High")

    def test_a_malformed_list_is_refused(self):
        for bad in ("https://example.com", {"url": "https://example.com"}, [3], [{"title": "x"}]):
            with self.subTest(links=bad):
                self.assert_nothing_created(links=bad)

    def test_a_key_that_is_not_a_link_field_is_refused_not_dropped(self):
        message = self.assert_nothing_created(links=[{"url": "https://example.com", "label": "x"}])

        self.assertIn("`label`", message)

    def test_the_caps_hold(self):
        self.assert_nothing_created(links=[f"https://example.com/{i}" for i in range(51)])
        self.assert_nothing_created(links=["https://example.com/" + "a" * 2000])
        self.assert_nothing_created(links=[{"url": "https://example.com", "title": "t" * 301}])

    def test_a_pr_link_asks_for_the_repos_webhook_as_the_task_save_does(self):
        self.create(links=self.LINKS)

        self.hook.assert_called_once()
        user, links = self.hook.call_args.args
        self.assertEqual(user.id, self.user.id)
        self.assertEqual([link["url"] for link in links], [link["url"] for link in self.LINKS])

    def test_a_webhook_that_fails_does_not_fail_the_task(self):
        self.hook.side_effect = RuntimeError("github is down")

        out = self.create(links=self.LINKS)

        self.assertTrue(TaskMaster.objects.filter(task_id=out["task_id"]).exists())

    def test_no_links_no_webhook(self):
        self.create()

        self.hook.assert_not_called()


class TemplateAtCreateTests(ParityCreateBase):
    def test_a_built_in_by_id_or_label_gives_its_headings_with_the_title_under_the_first(self):
        for name in ("bug", "BUG", "Bug report", " bug "):
            with self.subTest(name=name):
                out = self.create(title="Login fails", template=name)

                headings = BUILT_IN_HEADINGS["bug"]
                self.assertEqual(
                    self.body(out),
                    "\n\n".join(
                        [f"### {headings[0]}", "Login fails", *(f"### {h}" for h in headings[1:])]
                    ),
                )

    def test_the_four_built_ins_each_start_with_their_own_first_heading(self):
        for name, headings in BUILT_IN_HEADINGS.items():
            with self.subTest(template=name):
                body = self.body(self.create(title="X", template=name))

                self.assertTrue(body.startswith(f"### {headings[0]}\n\nX"))
                self.assertEqual(body.count("### "), len(headings))

    def test_the_stored_body_is_blocks_the_editor_renders(self):
        task = self.made(self.create(title="Login fails", template="bug"))

        markdown = blocks_to_markdown(task.content)
        self.assertEqual(blocks_to_markdown(task_body_blocks(markdown)), markdown)
        self.assertEqual(task.content[0]["type"], "heading")

    def test_a_template_of_the_project_by_name(self):
        self.template()

        out = self.create(title="Ship it", template="design DOC")

        self.assertEqual(self.body(out), "### Context\n\nShip it\n\n### Done when\n\n- tests pass")

    def test_the_projects_own_template_wins_over_a_built_in_of_the_same_name(self):
        self.template(
            body=[_heading("Mine"), _para(_text("x", italic=True, textColor="gray"))], name="bug"
        )

        out = self.create(title="T", template="bug")

        self.assertTrue(self.body(out).startswith("### Mine"))

    def test_an_unknown_name_is_refused_with_the_names_there_are(self):
        self.template(name="Design doc")
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)
        self.template(name="Secret plan", project=other)

        message = self.assert_nothing_created(template="Nope")

        self.assertIn("default, bug, spike, milestone", message)
        self.assertIn("'Design doc'", message)
        self.assertNotIn("Secret plan", message)

    def test_another_projects_template_is_not_found_by_name(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        self.template(name="Secret plan", project=other)

        self.assertIn("not found", self.assert_nothing_created(template="Secret plan"))

    def test_two_templates_of_one_name_are_refused(self):
        self.template(name="Dup")
        self.template(name="dup")

        self.assertIn("2 templates", self.assert_nothing_created(template="DUP"))

    def test_an_empty_template_is_refused(self):
        self.template(body=[], name="Empty")

        self.assertIn("no content", self.assert_nothing_created(template="Empty"))

    def test_a_template_and_a_body_together_are_refused(self):
        message = self.assert_nothing_created(template="bug", content_text="Steps: ...")

        self.assertIn("not both", message)

    def test_a_blank_template_means_none(self):
        out = self.create(template="")

        self.assertEqual(self.body(out), "### Summary\n\nT")

    def test_a_template_that_is_not_text_is_refused(self):
        self.assertIn("`template`", self.assert_nothing_created(template=["bug"]))

    def test_an_oversize_template_falls_back_to_the_one_line_body(self):
        self.template(
            body=[_heading("Big"), *(_para(_text("x" * 1000)) for _ in range(20))], name="Huge"
        )

        with override_settings(AGENT_MAX_BODY_BYTES=2000):
            out = self.create(title="Fits", template="Huge")

        self.assertEqual(self.body(out), "### Summary\n\nFits")


class DefaultBodyTests(ParityCreateBase):
    def test_no_body_and_no_default_is_the_one_line_summary(self):
        self.assertEqual(
            self.body(self.create(title="Fix the footer")), "### Summary\n\nFix the footer"
        )

    def test_the_projects_default_template_fills_the_body(self):
        template = self.template()
        self.set_default(f"custom:{template.pk}")

        out = self.create(title="Ship it")

        self.assertEqual(self.body(out), "### Context\n\nShip it\n\n### Done when\n\n- tests pass")

    def test_a_built_in_the_project_made_its_default_applies(self):
        self.set_default("bug")

        out = self.create(title="Crash on save")

        self.assertTrue(
            self.body(out).startswith(f"### {BUILT_IN_HEADINGS['bug'][0]}\n\nCrash on save")
        )

    def test_a_default_of_another_project_reads_as_none(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        foreign = self.template(project=other, name="Foreign")
        self.set_default(f"custom:{foreign.pk}")

        self.assertEqual(self.body(self.create(title="T")), "### Summary\n\nT")

    def test_the_named_template_beats_the_default(self):
        template = self.template()
        self.set_default(f"custom:{template.pk}")

        out = self.create(title="X", template="spike")

        self.assertTrue(self.body(out).startswith(f"### {BUILT_IN_HEADINGS['spike'][0]}"))

    def test_a_body_the_caller_wrote_is_never_touched(self):
        template = self.template()
        self.set_default(f"custom:{template.pk}")

        out = self.create(title="T", content_text="Just this.")

        self.assertEqual(self.body(out), "Just this.")

    def test_a_blank_body_counts_as_no_body(self):
        self.assertEqual(
            self.body(self.create(title="T", content_text="  \n ")), "### Summary\n\nT"
        )

    def test_under_a_parent_the_body_is_still_filled(self):
        parent = self.made(self.create(title="Parent"))

        out = self.create(title="Child", parent_task_id=str(parent.task_id))

        self.assertEqual(self.body(out), "### Summary\n\nChild")


class CardTests(ParityCreateBase):
    def card(self, args, ctx=None):
        return controller._friendly_arguments(args, "create_task", ctx=ctx or self.ctx)

    def test_the_card_shows_the_body_the_task_is_created_with(self):
        template = self.template()
        self.set_default(f"custom:{template.pk}")
        args = {"title": "Ship it", "project_id": self.project.project_id}

        shown = self.card(args)["content_text"]
        out = CREATE_TASK.run(dict(args), self.ctx)

        self.assertEqual(shown, self.body(out))

    def test_the_card_shows_the_named_template(self):
        args = {"title": "Login fails", "project_id": self.project.project_id, "template": "bug"}

        shown = self.card(args)["content_text"]
        out = CREATE_TASK.run(dict(args), self.ctx)

        self.assertEqual(shown, self.body(out))
        self.assertEqual(self.card(args)["template"], "bug")

    def test_a_body_the_model_wrote_is_shown_as_it_is(self):
        args = {"title": "T", "project_id": self.project.project_id, "content_text": "mine"}

        self.assertEqual(self.card(args)["content_text"], "mine")

    def test_an_unknown_template_leaves_the_card_without_a_body_and_the_tool_refuses(self):
        args = {"title": "T", "project_id": self.project.project_id, "template": "Nope"}

        self.assertNotIn("content_text", self.card(args))
        with self.assertRaises(ToolError):
            CREATE_TASK.run(dict(args), self.ctx)

    def test_a_project_the_caller_cannot_create_in_shows_no_template_body(self):
        template = self.template()
        self.set_default(f"custom:{template.pk}")
        args = {"title": "T", "project_id": self.project.project_id}

        for user in (self.user2, self.stranger):
            with self.subTest(user=user.username):
                ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))
                self.assertNotIn("content_text", self.card(args, ctx))

    def test_the_reporter_is_labelled_only_for_someone_the_caller_may_name(self):
        shown = self.card(
            {"title": "T", "project_id": self.project.project_id, "reporter_id": str(self.user2.id)}
        )
        hidden = self.card(
            {
                "title": "T",
                "project_id": self.project.project_id,
                "reporter_id": str(self.stranger.id),
            }
        )

        self.assertEqual(shown["reporter_id"], "otheruser")
        self.assertEqual(hidden["reporter_id"], str(self.stranger.id))


class UndoTests(ParityCreateBase):
    def test_a_task_made_with_the_new_fields_is_still_fully_undoable(self):
        from origin.search_engine.agent import undo  # noqa: PLC0415

        ctx = ToolContext(
            team_id=str(self.team.team_id), user_id=str(self.user.id), capture_undo=True
        )

        out = CREATE_TASK.run(
            {
                "title": "T",
                "project_id": self.project.project_id,
                "status": "WIP",
                "reporter_id": str(self.user2.id),
                "links": ["https://example.com"],
                "template": "bug",
            },
            ctx,
        )

        self.assertIn(undo.UNDO_KEY, out)
        self.assertEqual(undo.UNDO_SUPPORT["create_task"], undo.FULL)
        self.assertEqual(
            undo.proposal("create_task", {"status": "WIP", "links": [{"url": "https://x.io"}]}),
            undo.Proposal(True, None),
        )
