"""`update_task` partial body edits and the stale-write guard (MIP-824).

`content_text` rewrites the whole description, so one corrected sentence meant
resending the body, and an edit made between the read and the write was
overwritten without a word. `fetch_task` now returns `body_hash`;
`update_task` takes `base_body_hash` (refused before any write when stale) and
one of `body_append` / `body_prepend` / `body_replace`.
"""

from __future__ import annotations

from unittest import mock

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.fetch_task import FETCH_TASK
from origin.search_engine.agent.tools.task_body_edit import body_hash
from origin.search_engine.agent.tools.template_prompts import PROMPTS
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase
from .test_mcp_endpoint import McpBase

PROMPT = "Why does this matter? What problem are we solving?"


def para(text, block_id, **styles):
    return {
        "id": block_id,
        "type": "paragraph",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": [{"type": "text", "text": text, "styles": styles}],
        "children": [],
    }


def blank(block_id="blank"):
    return {**para("", block_id)}


def prompt_block(block_id="prompt"):
    return para(PROMPT, block_id, italic=True, textColor="gray")


class Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Implementation",
            status="Open",
            priority="Normal",
            content=[para("First paragraph.", "a"), para("Second paragraph.", "b"), blank()],
        )
        patcher = mock.patch("origin.search_engine.agent.tools.update_task.push_task_body")
        self.push = patcher.start()
        self.addCleanup(patcher.stop)

    def _update(self, task=None, **args):
        task = task or self.task
        with self.captureOnCommitCallbacks(execute=True):
            return UPDATE_TASK.run({"task_id": task.task_id, **args}, self.ctx)

    def _fetch(self, task=None):
        return FETCH_TASK.run({"task_id": (task or self.task).task_id}, self.ctx)

    def _texts(self, task=None):
        task = task or self.task
        task.refresh_from_db()
        return [
            "".join(r.get("text", "") for r in b.get("content") or []) for b in task.content or []
        ]


class BodyHashTests(Base):
    def test_the_fixture_prompt_is_a_real_template_prompt(self):
        self.assertIn(PROMPT, PROMPTS)

    def test_fetch_task_returns_a_stable_hash(self):
        first = self._fetch()["body_hash"]
        self.assertEqual(first, self._fetch()["body_hash"])
        self.assertEqual(first, body_hash(self.task.content))

    def test_the_hash_moves_with_the_body(self):
        before = self._fetch()["body_hash"]
        self._update(body_append="More.")
        self.assertNotEqual(before, self._fetch()["body_hash"])

    def test_a_status_edit_does_not_move_it(self):
        # The reason it is a hash and not `ts_updated_at`, which moves on
        # every status / priority edit and would refuse a good write.
        before = self._fetch()["body_hash"]
        self._update(status="WIP", priority="High")
        self.assertEqual(before, self._fetch()["body_hash"])

    def test_it_is_taken_over_the_body_the_caller_was_shown(self):
        # An untouched template prompt is hidden from the reader (MIP-825).
        # Hashing the stored body would differ from the one hashed here, and
        # `base_body_hash` could never match on such a task.
        with_prompt = [prompt_block(), *self.task.content]
        self.assertEqual(body_hash(with_prompt), body_hash(self.task.content))
        self.task.content = with_prompt
        self.task.save(update_fields=["content"])
        shown = self._fetch()["body_hash"]
        self.assertEqual(shown, body_hash(self.task.content))
        result = self._update(base_body_hash=shown, body_append="Added.")
        self.assertEqual(result["changed_fields"], ["content"])

    def test_an_empty_body_has_a_hash_and_none_equals_empty(self):
        self.task.content = None
        self.task.save(update_fields=["content"])
        self.assertEqual(self._fetch()["body_hash"], body_hash([]))


class StaleWriteTests(Base):
    def test_a_stale_hash_is_refused_and_nothing_is_written(self):
        stale = self._fetch()["body_hash"]
        # Someone else edits in between.
        self.task.content = [para("Edited by someone else.", "z"), blank()]
        self.task.save(update_fields=["content"])
        before = list(self.task.content)

        with self.assertRaisesRegex(ToolError, "changed since you read it"):
            self._update(base_body_hash=stale, content_text="mine", status="WIP", title="New")

        self.task.refresh_from_db()
        self.assertEqual(self.task.content, before)
        # Refused as a whole: the status and title sent alongside did not land.
        self.assertEqual(self.task.status, "Open")
        self.assertEqual(self.task.title, "Implementation")
        self.push.assert_not_called()

    def test_a_current_hash_is_accepted_and_the_result_carries_the_new_one(self):
        result = self._update(base_body_hash=self._fetch()["body_hash"], content_text="Rewritten.")
        self.assertEqual(result["changed_fields"], ["content"])
        self.assertEqual(result["body_hash"], self._fetch()["body_hash"])

    def test_chained_edits_use_the_hash_from_the_previous_result(self):
        first = self._update(base_body_hash=self._fetch()["body_hash"], body_append="One.")
        second = self._update(base_body_hash=first["body_hash"], body_append="Two.")
        self.assertEqual(second["changed_fields"], ["content"])
        self.assertEqual(self._texts()[-3:], ["One.", "Two.", ""])

    def test_the_second_of_two_readers_is_refused(self):
        seen_by_both = self._fetch()["body_hash"]
        self._update(
            base_body_hash=seen_by_both, body_replace={"old_text": "First", "new_text": "1st"}
        )
        with self.assertRaisesRegex(ToolError, "stale"):
            self._update(base_body_hash=seen_by_both, body_append="late")

    def test_a_malformed_hash_is_refused(self):
        for bad in ("", "  ", 12):
            with self.subTest(bad=bad), self.assertRaisesRegex(ToolError, "base_body_hash"):
                self._update(base_body_hash=bad, body_append="x")

    def test_without_a_hash_nothing_is_checked(self):
        self.assertEqual(self._update(content_text="blind write")["changed_fields"], ["content"])


class ExclusivityTests(Base):
    def test_only_one_body_mode_per_call(self):
        combos = [
            {"content_text": "a", "body_append": "b"},
            {"body_append": "a", "body_prepend": "b"},
            {"body_prepend": "a", "body_replace": {"old_text": "First", "new_text": "x"}},
        ]
        before = list(self.task.content)
        for combo in combos:
            with (
                self.subTest(combo=sorted(combo)),
                self.assertRaisesRegex(ToolError, "only one of"),
            ):
                self._update(status="WIP", **combo)
        self.task.refresh_from_db()
        self.assertEqual(self.task.content, before)
        self.assertEqual(self.task.status, "Open")


class AppendPrependTests(Base):
    def test_append_keeps_existing_blocks_and_their_ids_and_the_trailing_blank(self):
        self._update(body_append="## Notes\n\nAdded later.")
        self.task.refresh_from_db()
        self.assertEqual(
            self._texts(), ["First paragraph.", "Second paragraph.", "Notes", "Added later.", ""]
        )
        # The original blocks are untouched, ids included.
        self.assertEqual([b["id"] for b in self.task.content[:2]], ["a", "b"])
        self.assertEqual(self.task.content[2]["type"], "heading")
        # The editor's blank last line is still last, and is still the old one.
        self.assertEqual(self.task.content[-1]["id"], "blank")

    def test_prepend_goes_first_and_keeps_the_rest(self):
        self._update(body_prepend="Status: **ready**")
        self.task.refresh_from_db()
        self.assertEqual(
            self._texts(), ["Status: ready", "First paragraph.", "Second paragraph.", ""]
        )
        self.assertEqual([b["id"] for b in self.task.content[1:]], ["a", "b", "blank"])

    def test_append_to_a_body_without_a_trailing_blank(self):
        self.task.content = [para("Only.", "a")]
        self.task.save(update_fields=["content"])
        self._update(body_append="Next.")
        self.assertEqual(self._texts(), ["Only.", "Next."])

    def test_append_and_prepend_to_an_empty_body_write_a_normal_body(self):
        self.task.content = None
        self.task.save(update_fields=["content"])
        self._update(body_append="Start.")
        self.assertEqual(self._texts(), ["Start.", ""])
        self.task.content = []
        self.task.save(update_fields=["content"])
        self._update(body_prepend="Head.")
        self.assertEqual(self._texts(), ["Head.", ""])

    def test_template_prompts_stay_in_the_stored_body(self):
        # Splicing never rewrites what is already there, hidden prompts too.
        self.task.content = [prompt_block(), *self.task.content]
        self.task.save(update_fields=["content"])
        self._update(body_append="Filled in.")
        self.assertEqual(self._texts()[0], PROMPT)

    def test_an_empty_fragment_is_refused(self):
        for mode in ("body_append", "body_prepend"):
            for fragment in ("", "  \n "):
                with (
                    self.subTest(mode=mode, fragment=fragment),
                    self.assertRaisesRegex(ToolError, "non-empty"),
                ):
                    self._update(**{mode: fragment})

    def test_a_chip_in_the_fragment_is_resolved_like_content_text(self):
        self._update(body_append=f"cc [@Me](user:{self.user.id})")
        self.task.refresh_from_db()
        runs = self.task.content[-2]["content"]
        self.assertTrue(any(r.get("type") == "mention" for r in runs), runs)

    def test_the_editor_doc_is_pushed_with_the_whole_new_body(self):
        self._update(body_append="Pushed.")
        self.task.refresh_from_db()
        self.push.assert_called_once_with(self.task.task_id, self.task.content)


class ReplaceTests(Base):
    def test_one_match_is_replaced_and_the_rest_of_the_body_survives(self):
        result = self._update(body_replace={"old_text": "Second", "new_text": "2nd"})
        self.assertEqual(result["changed_fields"], ["content"])
        self.assertEqual(self._texts(), ["First paragraph.", "2nd paragraph.", ""])

    def test_zero_matches_are_refused_with_the_count(self):
        with self.assertRaisesRegex(ToolError, "matched 0 times"):
            self._update(body_replace={"old_text": "absent", "new_text": "x"})
        self.assertEqual(self._texts(), ["First paragraph.", "Second paragraph.", ""])

    def test_several_matches_are_refused_with_the_count(self):
        with self.assertRaisesRegex(ToolError, "matched 2 times"):
            self._update(body_replace={"old_text": "paragraph", "new_text": "x"})
        self.assertEqual(self._texts(), ["First paragraph.", "Second paragraph.", ""])
        self.push.assert_not_called()

    def test_new_text_can_be_empty_to_delete_the_match(self):
        self._update(body_replace={"old_text": " paragraph.\n\nSecond", "new_text": ""})
        self.assertEqual(self._texts(), ["First paragraph.", ""])

    def test_replacing_a_match_with_itself_changes_nothing(self):
        result = self._update(body_replace={"old_text": "First", "new_text": "First"})
        self.assertEqual(result["changed_fields"], [])
        self.push.assert_not_called()

    def test_the_text_to_find_is_what_content_markdown_shows(self):
        # Copy-from-the-read: a styled run is found by its markdown, and
        # matching is not done on raw blocks (one phrase is several runs).
        self.task.content = [
            {
                **para("", "a"),
                "content": [
                    {"type": "text", "text": "Ship ", "styles": {}},
                    {"type": "text", "text": "on Friday", "styles": {"bold": True}},
                    {"type": "text", "text": ".", "styles": {}},
                ],
            },
            blank(),
        ]
        self.task.save(update_fields=["content"])
        shown = self._fetch()["content_markdown"]
        self.assertIn("**on Friday**", shown)
        self._update(body_replace={"old_text": "**on Friday**", "new_text": "**on Monday**"})
        self.task.refresh_from_db()
        runs = self.task.content[0]["content"]
        self.assertIn(
            ("on Monday", True), [(r["text"], r["styles"].get("bold") is True) for r in runs]
        )

    def test_a_hidden_template_prompt_cannot_be_matched(self):
        self.task.content = [prompt_block(), *self.task.content]
        self.task.save(update_fields=["content"])
        with self.assertRaisesRegex(ToolError, "matched 0 times"):
            self._update(body_replace={"old_text": PROMPT, "new_text": "x"})

    def test_a_replace_on_a_task_with_a_prompt_works_on_what_was_shown(self):
        self.task.content = [prompt_block(), *self.task.content]
        self.task.save(update_fields=["content"])
        shown_hash = self._fetch()["body_hash"]
        self._update(
            base_body_hash=shown_hash, body_replace={"old_text": "First", "new_text": "Opening"}
        )
        # Written back from the shown body, as `content_text` would: the
        # untouched prompt is not carried over.
        self.assertEqual(self._texts(), ["Opening paragraph.", "Second paragraph.", ""])

    def test_a_malformed_patch_is_refused(self):
        bad = [
            "just a string",
            {"old_text": "", "new_text": "x"},
            {"old_text": "First"},
            {"old_text": 3, "new_text": "x"},
            {"old_text": "First", "new_text": None},
        ]
        for patch in bad:
            with self.subTest(patch=patch), self.assertRaises(ToolError):
                self._update(body_replace=patch)
        self.assertEqual(self._texts(), ["First paragraph.", "Second paragraph.", ""])


class BackingTaskTests(Base):
    """A milestone's body lives on `MilestoneMaster.description`; its backing
    task row is a mirror `sync_backing_task` re-derives on every write."""

    def setUp(self):
        super().setUp()
        self.milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="v1.0",
            status="Open",
            priority="Normal",
        )
        self.backing = ensure_backing_task(self.milestone)
        self._update(self.backing, content_text="# Goal\n\nShip it.\n\nOwner: Ken")

    def test_append_reaches_the_milestone_and_its_backing_row(self):
        self._update(self.backing, body_append="Extra line.")
        self.milestone.refresh_from_db()
        self.backing.refresh_from_db()
        self.assertEqual(self.milestone.description, self.backing.content)
        self.assertIn("Extra line.", self._texts(self.backing))

    def test_replace_reaches_the_milestone_and_its_backing_row(self):
        self._update(self.backing, body_replace={"old_text": "Ken", "new_text": "Ada"})
        self.milestone.refresh_from_db()
        self.backing.refresh_from_db()
        self.assertEqual(self.milestone.description, self.backing.content)
        self.assertIn("Owner: Ada", self._texts(self.backing))

    def test_the_returned_hash_matches_what_a_later_read_sees(self):
        # The write goes through `sync_backing_task`, which re-saves the
        # backing row from the milestone. The hash handed back must be the
        # one `fetch_task` computes from that stored row, or chained edits
        # would be refused on the second call.
        result = self._update(self.backing, body_append="Extra line.")
        self.assertEqual(result["body_hash"], self._fetch(self.backing)["body_hash"])
        again = self._update(
            self.backing, base_body_hash=result["body_hash"], body_append="And another."
        )
        self.assertEqual(again["changed_fields"], ["content"])

    def test_a_stale_hash_is_refused_for_a_backing_task(self):
        stale = self._fetch(self.backing)["body_hash"]
        self._update(self.backing, body_append="Someone else got here first.")
        self.milestone.refresh_from_db()
        before = self.milestone.description
        with self.assertRaisesRegex(ToolError, "stale"):
            self._update(self.backing, base_body_hash=stale, body_append="mine")
        self.milestone.refresh_from_db()
        self.assertEqual(self.milestone.description, before)


class InAppAndMcpTests(McpBase):
    def test_the_in_app_agent_gets_the_hash_too(self):
        out = FETCH_TASK.run(
            {"task_id": self.task.task_id},
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id)),
        )
        self.assertEqual(out["body_hash"], body_hash(self.task.content))

    def test_the_arguments_are_published_and_a_nested_patch_is_accepted(self):
        from origin.views.public.mcp import tools

        props = tools.input_schema(tools.BY_NAME["update_task"])["properties"]
        for name in ("body_append", "body_prepend", "body_replace", "base_body_hash"):
            self.assertIn(name, props)
        self.assertEqual(props["body_replace"]["type"], "object")
        self.assertEqual(sorted(props["body_replace"]["required"]), ["new_text", "old_text"])

    def test_read_edit_over_mcp_round_trip(self):
        self.task.content = [para("Return 404 for a foreign team.", "a"), blank()]
        self.task.save(update_fields=["content"])
        self._key()
        read = self._result(self._call("get_task", {"task_id": "WRD-7"}))["structuredContent"]
        self.assertIn("body_hash", read)
        with mock.patch("origin.search_engine.agent.tools.update_task.push_task_body"):
            res = self._call(
                "update_task",
                {
                    "task_id": "WRD-7",
                    "base_body_hash": read["body_hash"],
                    "body_replace": {"old_text": "404", "new_text": "403"},
                },
            )
        out = self._result(res)["structuredContent"]
        self.assertEqual(out["changed_fields"], ["content"])
        self.task.refresh_from_db()
        self.assertEqual(
            self.task.content[0]["content"][0]["text"], "Return 403 for a foreign team."
        )
        # The same hash again is now stale, and the refusal is a tool error
        # the model can read, with the body untouched.
        res = self._call(
            "update_task",
            {"task_id": "WRD-7", "base_body_hash": read["body_hash"], "body_append": "late"},
        )
        self.assertTrue(self._result(res)["isError"])
        self.task.refresh_from_db()
        self.assertEqual(len(self.task.content), 2)

    def test_the_hash_is_a_documented_field(self):
        from origin.views.public.mcp import tools

        self.assertIn("body_hash", tools.BY_NAME["get_task"].description)
        self.assertIn("base_body_hash", tools.BY_NAME["update_task"].description)
