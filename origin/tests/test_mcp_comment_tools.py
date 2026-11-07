"""Over MCP, a comment can be fixed or taken back, and a rewrite walked back (MIP-880).

There is no approval card on this surface, so the key's scope is the only
authorization. What the endpoint adds to the tools' own rules: the delete needs
the key's destructive opt-in and nothing else does; a read key sees none of the
three; a task can be named by its display id; and the three ids a caller needs
(`comment_id`, `activity_id`) really do come from published reads.
"""

from __future__ import annotations

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.task.task_models import TaskComments
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools

NEW = {"update_comment", "delete_comment", "restore_task_body"}


class McpCommentBase(McpBase):
    def listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def run_tool(self, name, arguments):
        result = self._result(self._call(name, arguments))
        return result, result["content"][0]["text"]

    def comment(self, text="first draft"):
        result, _ = self.run_tool("add_comment", {"task_id": "WRD-7", "body_text": text})
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]["comment_id"]

    def row(self, comment_id):
        return TaskComments.objects.get(task=self.task, comment_id=comment_id)


class TestWhoSeesWhat(McpCommentBase):
    def test_a_write_key_sees_the_edit_and_the_restore_but_not_the_delete(self):
        self._key(scope=SCOPE_WRITE)
        names = set(self.listed())
        self.assertIn("update_comment", names)
        self.assertIn("restore_task_body", names)
        self.assertNotIn("delete_comment", names)

    def test_of_the_three_only_the_delete_needs_the_destructive_opt_in(self):
        """A key without the opt-in can still fix a comment and restore a body."""
        self._key(scope=SCOPE_WRITE)
        self.assertEqual(NEW & set(self.listed()), {"update_comment", "restore_task_body"})
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        self.assertEqual(NEW & set(self.listed()), NEW)

    def test_a_read_key_sees_none_of_them(self):
        self._key(scope=SCOPE_READ)
        self.assertFalse(NEW & set(self.listed()))

    def test_the_delete_alone_is_marked_destructive(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        listed = self.listed()
        self.assertIs(listed["delete_comment"]["annotations"]["destructiveHint"], True)
        self.assertIs(listed["update_comment"]["annotations"]["destructiveHint"], False)
        self.assertIs(listed["restore_task_body"]["annotations"]["destructiveHint"], False)

    def test_the_catalog_says_who_the_comment_belongs_to(self):
        for name in ("update_comment", "delete_comment"):
            self.assertIn("get_task", tools.BY_NAME[name].description)
            self.assertIn("own", tools.BY_NAME[name].description)
        self.assertIn("get_task_history", tools.BY_NAME["restore_task_body"].description)


class TestTheCallsWork(McpCommentBase):
    def test_a_comment_is_edited_by_the_tasks_display_id(self):
        self._key(scope=SCOPE_WRITE)
        cid = self.comment("first draft")

        result, _ = self.run_tool(
            "update_comment", {"task_id": "WRD-7", "comment_id": cid, "body_text": "second draft"}
        )

        self.assertFalse(result.get("isError"), result)
        self.assertEqual(result["structuredContent"]["display_id"], "WRD-7")
        body = self.row(cid).comment_body
        self.assertIn("second draft", str(body))

    def test_the_comment_id_comes_from_get_task(self):
        self._key(scope=SCOPE_WRITE)
        cid = self.comment("first draft")
        task, _ = self.run_tool("get_task", {"task_id": "WRD-7"})
        listed = task["structuredContent"]["comments"]
        self.assertIn(cid, [c["comment_id"] for c in listed])
        self.assertTrue(all("sender_id" in c for c in listed))

    def test_the_delete_is_refused_without_the_opt_in(self):
        self._key(scope=SCOPE_WRITE)
        cid = self.comment()

        result, text = self.run_tool("delete_comment", {"task_id": "WRD-7", "comment_id": cid})

        self.assertTrue(result.get("isError"), result)
        self.assertIn("destructive", text.lower())
        self.assertFalse(self.row(cid).is_deleted)

    def test_the_delete_works_with_the_opt_in(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        cid = self.comment()

        result, _ = self.run_tool("delete_comment", {"task_id": "WRD-7", "comment_id": cid})

        self.assertFalse(result.get("isError"), result)
        self.assertTrue(self.row(cid).is_deleted)

    def test_a_read_key_cannot_call_them_even_from_a_cached_list(self):
        self._key(scope=SCOPE_WRITE)
        cid = self.comment()
        self._key(scope=SCOPE_READ)
        for name, args in (
            ("update_comment", {"task_id": "WRD-7", "comment_id": cid, "body_text": "x"}),
            ("restore_task_body", {"task_id": "WRD-7", "activity_id": 1}),
        ):
            result, _ = self.run_tool(name, args)
            self.assertTrue(result.get("isError"), name)

    def test_a_rewrite_is_walked_back_end_to_end(self):
        """The loop this exists for: rewrite, see it in history, put it back."""
        self._key(scope=SCOPE_WRITE)
        self.task.content = [
            {"type": "paragraph", "content": [{"type": "text", "text": "The careful spec"}]}
        ]
        self.task.save(update_fields=["content"])

        rewrite, _ = self.run_tool(
            "update_task", {"task_id": "WRD-7", "content_text": "Oops, a fragment"}
        )
        self.assertFalse(rewrite.get("isError"), rewrite)
        self.assertIn("fragment", str(self.task.__class__.objects.get(pk=self.task.pk).content))

        history, _ = self.run_tool("get_task_history", {"task_id": "WRD-7"})
        restorable = [
            e
            for e in history["structuredContent"]["history"]
            if e["action"] == "description_edited" and e.get("restorable")
        ]
        self.assertTrue(restorable, history["structuredContent"]["history"])

        restored, _ = self.run_tool(
            "restore_task_body",
            {"task_id": "WRD-7", "activity_id": restorable[0]["activity_id"]},
        )

        self.assertFalse(restored.get("isError"), restored)
        content = str(self.task.__class__.objects.get(pk=self.task.pk).content)
        self.assertIn("careful spec", content)
        self.assertNotIn("fragment", content)
