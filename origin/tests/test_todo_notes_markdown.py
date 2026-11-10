"""A to-do's notes take markdown from the agent and MCP, and read back as it (MIP-1334).

The notes are a BlockNote document, edited in the app by a BlockNote editor on the
shared schema, but `create_todo_item` and `update_todo_item` wrapped `notes_text` one
plain paragraph per line, so `[spec](https://...)` and `- item` were stored as the
literal characters. Now:

* `notes_text` is markdown, parsed by the converter task bodies use;
* each line of running text stays its own line, as plain-text notes always did;
* `@`/`#` chips render (and, as everywhere on to-dos, notify nobody);
* the two list tools return `notes_markdown`, so an agent reads before it replaces,
  and what it writes back keeps the links and lists a person typed.
"""

from __future__ import annotations

from origin.models.chat.todo_models import ToDoItem
from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.search_engine.agent.tools.create_todo_item import CREATE_TODO_ITEM
from origin.search_engine.agent.tools.list_today_todos import LIST_TODAY_TODOS
from origin.search_engine.agent.tools.list_uncompleted_todos import LIST_UNCOMPLETED_TODOS
from origin.search_engine.agent.tools.update_todo_item import UPDATE_TODO_ITEM
from origin.search_engine.text_extraction import extract_text
from origin.tests.test_agent_tool_todo_writes import TodoToolBase
from origin.tests.test_mcp_todo_writes import McpTodoBase

LINKED = "Read [the spec](https://example.com/spec) and **sign** it\n\n- first\n- second"


def _inline(blocks):
    """Every inline node in a block list, depth first."""
    out = []
    for block in blocks or []:
        for node in block.get("content") or []:
            if isinstance(node, dict):
                out.append(node)
                out.extend(n for n in node.get("content") or [] if isinstance(n, dict))
        out.extend(_inline(block.get("children")))
    return out


def _types(blocks):
    return [b["type"] for b in blocks or [] if b.get("content") or b["type"] != "paragraph"]


class CreateTakesMarkdown(TodoToolBase):
    def _create(self, notes_text):
        out = CREATE_TODO_ITEM.run({"title": "Contract", "notes_text": notes_text}, self.ctx)
        return ToDoItem.objects.get(item_id=out["item_id"])

    def test_a_link_is_a_link_not_its_brackets(self):
        item = self._create(LINKED)
        links = [n for n in _inline(item.notes) if n.get("type") == "link"]
        self.assertEqual([link["href"] for link in links], ["https://example.com/spec"])
        self.assertNotIn("[the spec]", extract_text(item.notes))
        self.assertNotIn("**", extract_text(item.notes))
        bold = [n for n in _inline(item.notes) if n.get("styles", {}).get("bold")]
        self.assertEqual([n["text"] for n in bold], ["sign"])

    def test_lists_and_checklists_are_blocks(self):
        item = self._create("- one\n- two\n\n- [ ] open\n- [x] done\n\n1. first")
        self.assertEqual(
            _types(item.notes),
            [
                "bulletListItem",
                "bulletListItem",
                "checkListItem",
                "checkListItem",
                "numberedListItem",
            ],
        )
        checked = [b["props"].get("checked") for b in item.notes if b["type"] == "checkListItem"]
        self.assertEqual(checked, [False, True])

    def test_each_line_of_plain_text_stays_its_own_line(self):
        # The old notes were one paragraph per line; strict markdown would join them.
        item = self._create("Call Sam\nThen email Carol")
        texts = [extract_text([b]).strip() for b in item.notes if b.get("content")]
        self.assertEqual(texts, ["Call Sam", "Then email Carol"])

    def test_a_member_link_is_a_mention_chip(self):
        item = self._create(f"Ask [@me](user:{self.user.id}) first")
        mentions = [n for n in _inline(item.notes) if n.get("type") == "mention"]
        self.assertEqual(len(mentions), 1)
        self.assertNotIn("user:", extract_text(item.notes))

    def test_blocks_carry_ids_and_the_editor_trailing_line(self):
        item = self._create("Hello")
        self.assertTrue(all(b.get("id") for b in item.notes))
        self.assertEqual(item.notes[-1]["type"], "paragraph")
        self.assertEqual(item.notes[-1]["content"], [])

    def test_no_notes_text_means_no_notes(self):
        self.assertIsNone(self._create("").notes)
        self.assertIsNone(self._create("   \n  ").notes)

    def test_the_search_text_is_the_words(self):
        item = self._create(LINKED)
        text = extract_text(item.notes)
        for word in ("the spec", "sign", "first", "second"):
            self.assertIn(word, text)


class UpdateTakesMarkdown(TodoToolBase):
    def test_replaces_the_notes_with_parsed_markdown(self):
        item = self.todo(
            notes=[{"type": "paragraph", "content": [{"type": "text", "text": "old"}]}]
        )
        UPDATE_TODO_ITEM.run({"item_id": item.item_id, "notes_text": LINKED}, self.ctx)
        item.refresh_from_db()
        self.assertIn("link", [n.get("type") for n in _inline(item.notes)])
        self.assertNotIn("old", extract_text(item.notes))

    def test_an_empty_string_clears_them(self):
        item = self.todo(notes=markdown_to_blocks("keep me"))
        UPDATE_TODO_ITEM.run({"item_id": item.item_id, "notes_text": ""}, self.ctx)
        item.refresh_from_db()
        self.assertIsNone(item.notes)

    def test_leaving_notes_text_out_leaves_them(self):
        item = self.todo(notes=markdown_to_blocks("keep me"))
        before = item.notes
        UPDATE_TODO_ITEM.run({"item_id": item.item_id, "title": "Renamed"}, self.ctx)
        item.refresh_from_db()
        self.assertEqual(item.notes, before)


class ReadsReturnMarkdown(TodoToolBase):
    def _today_row(self, item):
        rows = LIST_TODAY_TODOS.run({}, self.ctx)["items"]
        return next(r for r in rows if r["item_id"] == item.item_id)

    def _open_row(self, item):
        rows = LIST_UNCOMPLETED_TODOS.run({}, self.ctx)["items"]
        return next(r for r in rows if r["item_id"] == item.item_id)

    def test_both_list_tools_give_the_notes_as_markdown(self):
        item = self.todo(notes=markdown_to_blocks(LINKED))
        for row in (self._today_row(item), self._open_row(item)):
            self.assertIn("[the spec](https://example.com/spec)", row["notes_markdown"])
            self.assertIn("**sign**", row["notes_markdown"])
            self.assertIn("- first", row["notes_markdown"])

    def test_a_to_do_without_notes_has_no_notes_field(self):
        for notes in (None, [], [{"type": "paragraph", "content": []}]):
            item = self.todo(title=f"bare {notes!r}", notes=notes)
            self.assertNotIn("notes_markdown", self._today_row(item))
            self.assertNotIn("notes_markdown", self._open_row(item))

    def test_what_is_read_writes_back_unchanged(self):
        # An agent that reads the notes and sends them back with an edit keeps
        # the person's links and lists: the round trip is lossless.
        created = CREATE_TODO_ITEM.run({"title": "Contract", "notes_text": LINKED}, self.ctx)
        item = ToDoItem.objects.get(item_id=created["item_id"])
        first = self._today_row(item)["notes_markdown"]

        UPDATE_TODO_ITEM.run({"item_id": item.item_id, "notes_text": first}, self.ctx)
        item.refresh_from_db()
        self.assertEqual(self._today_row(item)["notes_markdown"], first)
        self.assertEqual([n.get("type") for n in _inline(item.notes)].count("link"), 1, item.notes)

    def test_lines_round_trip_as_lines(self):
        created = CREATE_TODO_ITEM.run(
            {"title": "Calls", "notes_text": "Call Sam\nThen email Carol"}, self.ctx
        )
        item = ToDoItem.objects.get(item_id=created["item_id"])
        md = self._today_row(item)["notes_markdown"]
        UPDATE_TODO_ITEM.run({"item_id": item.item_id, "notes_text": md}, self.ctx)
        item.refresh_from_db()
        texts = [extract_text([b]).strip() for b in item.notes if b.get("content")]
        self.assertEqual(texts, ["Call Sam", "Then email Carol"])


class OverMcp(McpTodoBase):
    def test_the_write_tools_say_markdown_and_not_plain_text(self):
        self._key(scope=SCOPE_WRITE)
        listed = self._listed()
        for name in ("create_todo_item", "update_todo_item"):
            notes = listed[name]["inputSchema"]["properties"]["notes_text"]["description"]
            self.assertIn("Markdown", notes, name)
            self.assertNotIn("plain text", notes.lower(), name)
            self.assertIn("notifies no one", notes, name)
        self.assertIn(
            "notes_markdown",
            listed["update_todo_item"]["inputSchema"]["properties"]["notes_text"]["description"],
        )

    def test_create_over_mcp_stores_a_link(self):
        self._key(scope=SCOPE_WRITE)
        self._text("create_todo_item", {"title": "Spec", "notes_text": LINKED})
        item = ToDoItem.objects.get(title="Spec")
        self.assertIn("link", [n.get("type") for n in _inline(item.notes)])

    def test_the_reads_over_mcp_return_notes_markdown(self):
        self.item.notes = markdown_to_blocks(LINKED)
        self.item.save(update_fields=["notes"])
        self._key(scope=SCOPE_READ)
        for name in ("list_today_todos", "list_uncompleted_todos"):
            text, is_error = self._text(name, {})
            self.assertFalse(is_error, text)
            self.assertIn("notes_markdown", text, name)
            self.assertIn("https://example.com/spec", text, name)
