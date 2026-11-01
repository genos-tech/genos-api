"""Template prompts nobody filled in are not content to a reader (MIP-825).

KDS-333's body was mostly "Why does this matter? What problem are we
solving?" and "First condition that must be true when this is done." in
italic gray: the template's instructions, saved because the author never
wrote in those sections. `fetch_task` (the agent tool and MCP `get_task`)
now reads the body without them. Stored data is untouched.
"""

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.fetch_task import FETCH_TASK
from origin.search_engine.agent.tools.template_prompts import without_untouched_prompts

from .test_base import BaseAPITestCase

PARA = {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"}
HEAD = {**PARA, "level": 3}
GRAY = {"italic": True, "textColor": "gray"}


def _h(text):
    return {
        "type": "heading",
        "props": HEAD,
        "content": [{"type": "text", "text": text, "styles": {}}],
        "children": [],
    }


def _p(text, styles=None):
    return {
        "type": "paragraph",
        "props": PARA,
        "content": [{"type": "text", "text": text, "styles": styles or {}}] if text else [],
        "children": [],
    }


def _b(text, styles=None):
    return {**_p(text, styles), "type": "bulletListItem"}


def _prompt(text):
    return _p(text, GRAY)


def _bullet_prompt(text):
    return _b(text, GRAY)


def _template():
    """The default template as the editor stores it."""
    return [
        _h("🧾 Summary"),
        _prompt("One or two lines on what this task delivers."),
        _p(""),
        _h("🪜 Motivation"),
        _prompt("Why does this matter? What problem are we solving?"),
        _p(""),
        _h("✅ Acceptance criteria"),
        _bullet_prompt("First condition that must be true when this is done."),
        _bullet_prompt("Second condition…"),
        _bullet_prompt("Third condition…"),
        _p(""),
        _h("🎯 Notes & links"),
        _prompt("Anything else worth pinning here."),
        _p(""),
    ]


def _texts(blocks):
    return [
        "".join(r.get("text", "") for r in b.get("content", [])) for b in blocks if b.get("content")
    ]


class WithoutUntouchedPromptsTests(BaseAPITestCase):
    def test_an_untouched_template_has_nothing_left(self):
        self.assertEqual(_texts(without_untouched_prompts(_template())), [])
        self.assertFalse(
            [b for b in without_untouched_prompts(_template()) if b["type"] == "heading"]
        )

    def test_a_filled_section_keeps_its_heading_and_the_rest_goes(self):
        body = _template()
        body[4] = _p("Users lose their draft.")
        out = without_untouched_prompts(body)
        self.assertEqual(_texts(out), ["🪜 Motivation", "Users lose their draft."])

    def test_an_edited_bullet_stays_and_its_untouched_siblings_go(self):
        body = _template()
        body[7] = _b("Saving keeps the draft.")
        out = without_untouched_prompts(body)
        self.assertEqual(_texts(out), ["✅ Acceptance criteria", "Saving keeps the draft."])

    def test_a_line_that_repeats_a_prompt_without_the_styling_is_kept(self):
        body = _template()
        body[12] = _p("Anything else worth pinning here.")  # plain, not italic gray
        out = without_untouched_prompts(body)
        self.assertEqual(_texts(out), ["🎯 Notes & links", "Anything else worth pinning here."])

    def test_a_gray_italic_line_that_is_not_a_known_prompt_is_kept(self):
        body = [_h("Mine"), _prompt("My own gray note")]
        self.assertEqual(without_untouched_prompts(body), body)

    def test_content_before_the_first_heading_is_kept(self):
        body = [_p("Context first.")] + _template()
        self.assertEqual(_texts(without_untouched_prompts(body)), ["Context first."])

    def test_a_body_with_no_prompts_comes_back_as_it_was(self):
        body = [_h("Notes"), _p("Hello")]
        self.assertIs(without_untouched_prompts(body), body)

    def test_odd_shapes_are_returned_unchanged(self):
        for odd in (None, "", "plain text", {}, {"nope": 1}, [], [None, 3]):
            self.assertEqual(without_untouched_prompts(odd), odd)

    def test_the_input_is_not_mutated(self):
        body = _template()
        snapshot = [dict(b) for b in body]
        without_untouched_prompts(body)
        self.assertEqual(body, snapshot)


class FetchTaskReadsWithoutPromptsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Kraken",
            code="KDS",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _task(self, content):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Defect",
            content=content,
        )

    def test_the_boilerplate_is_gone_from_text_and_markdown(self):
        body = _template()
        body[1] = _p("The export drops the last row.")
        task = self._task(body)

        out = FETCH_TASK.run({"task_id": task.task_id}, self.ctx)

        for key in ("content_text", "content_markdown"):
            self.assertIn("The export drops the last row.", out[key])
            self.assertNotIn("Why does this matter", out[key])
            self.assertNotIn("First condition", out[key])
            self.assertNotIn("Anything else worth pinning", out[key])

    def test_a_task_with_only_prompts_reads_as_empty(self):
        task = self._task(_template())
        out = FETCH_TASK.run({"task_id": task.task_id}, self.ctx)
        for key in ("content_text", "content_markdown"):
            self.assertNotIn("Why does this matter", out[key])
            self.assertNotIn("Motivation", out[key])

    def test_the_stored_body_is_not_rewritten(self):
        task = self._task(_template())
        FETCH_TASK.run({"task_id": task.task_id}, self.ctx)
        task.refresh_from_db()
        self.assertEqual(task.content, _template())
