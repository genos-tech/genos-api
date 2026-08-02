"""Tests for `blocknote_render.blocks_to_markdown`.

The point of the module is that a task description written in the editor
survives being handed to a reader that has to ACT on it. The flattener
(`extract_text`) was built for the search index, where structure is
noise; these tests pin the cases where structure is the whole meaning —
a checked box vs an unchecked one, a code block vs prose that happens to
mention code, a heading vs a sentence.

Round-trip tests go through `markdown_to_blocks` (the inverse) rather
than comparing strings, so they assert the *structure* is preserved
rather than one particular rendering of it.
"""

from django.test import SimpleTestCase

from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown


def _text(value, styles=None):
    return {"type": "text", "text": value, "styles": styles or {}}


def _block(btype, text="", *, props=None, children=None, content=None):
    return {
        "type": btype,
        "props": props or {},
        "content": content if content is not None else ([_text(text)] if text else []),
        "children": children or [],
    }


class BlockRenderingTests(SimpleTestCase):
    def test_heading_level_becomes_hash_count(self):
        body = [
            _block("heading", "Goal", props={"level": 1}),
            _block("heading", "Acceptance criteria", props={"level": 2}),
        ]
        self.assertEqual(blocks_to_markdown(body), "# Goal\n\n## Acceptance criteria")

    def test_heading_level_is_clamped_and_survives_garbage(self):
        self.assertEqual(
            blocks_to_markdown([_block("heading", "X", props={"level": 99})]), "###### X"
        )
        self.assertEqual(blocks_to_markdown([_block("heading", "X", props={"level": "no"})]), "# X")
        self.assertEqual(blocks_to_markdown([_block("heading", "X", props={})]), "# X")

    def test_checklist_state_is_visible(self):
        """The case the flattener cannot express: which items are done."""
        body = [
            _block("checkListItem", "returns 404", props={"checked": True}),
            _block("checkListItem", "covered by a test", props={"checked": False}),
            # BlockNote has stored `checked` as the string "true" too.
            _block("checkListItem", "legacy shape", props={"checked": "true"}),
        ]
        self.assertEqual(
            blocks_to_markdown(body),
            "- [x] returns 404\n- [ ] covered by a test\n- [x] legacy shape",
        )

    def test_code_block_is_fenced_with_its_language(self):
        body = [
            _block(
                "codeBlock",
                props={"language": "python"},
                content=[_text("def f():\n    return 1")],
            )
        ]
        self.assertEqual(
            blocks_to_markdown(body),
            "```python\ndef f():\n    return 1\n```",
        )

    def test_markdown_inside_a_code_block_stays_literal(self):
        """Inline markers must not be applied to source — `**x**` in code
        is two asterisks, not bold."""
        body = [_block("codeBlock", props={"language": ""}, content=[_text("a = '**x**'")])]
        self.assertEqual(blocks_to_markdown(body), "```\na = '**x**'\n```")

    def test_numbered_list_numbers_ascend_and_reset(self):
        body = [
            _block("numberedListItem", "first"),
            _block("numberedListItem", "second"),
            _block("paragraph", "interrupting"),
            _block("numberedListItem", "restarted"),
        ]
        self.assertEqual(
            blocks_to_markdown(body),
            "1. first\n2. second\n\ninterrupting\n\n1. restarted",
        )

    def test_a_list_run_is_one_tight_block(self):
        """A blank line between items would end the list in most parsers."""
        body = [_block("bulletListItem", "a"), _block("bulletListItem", "b")]
        self.assertEqual(blocks_to_markdown(body), "- a\n- b")

    def test_nested_children_are_indented_inside_the_same_list(self):
        body = [
            _block(
                "bulletListItem",
                "parent",
                children=[_block("bulletListItem", "child")],
            ),
            _block("bulletListItem", "sibling"),
        ]
        self.assertEqual(blocks_to_markdown(body), "- parent\n  - child\n- sibling")

    def test_inline_styles_and_links(self):
        body = [
            _block(
                "paragraph",
                content=[
                    _text("see "),
                    {
                        "type": "link",
                        "href": "https://example.com",
                        "content": [_text("the docs")],
                    },
                    _text(" — "),
                    _text("bold", {"bold": True}),
                    _text(" and "),
                    _text("italic", {"italic": True}),
                    _text(" and "),
                    _text("code", {"code": True}),
                ],
            )
        ]
        self.assertEqual(
            blocks_to_markdown(body),
            "see [the docs](https://example.com) — **bold** and *italic* and `code`",
        )

    def test_code_style_is_not_double_wrapped(self):
        """`**`x`**` renders wrong — code wins and is applied alone."""
        body = [_block("paragraph", content=[_text("x", {"code": True, "bold": True})])]
        self.assertEqual(blocks_to_markdown(body), "`x`")

    def test_mentions_and_custom_emoji_match_the_flattener(self):
        body = [
            _block(
                "paragraph",
                content=[
                    {"type": "mention", "props": {"userName": "alice"}},
                    _text(" "),
                    {"type": "customEmoji", "props": {"name": "party"}},
                ],
            )
        ]
        self.assertEqual(blocks_to_markdown(body), "@alice :party:")

    def test_quote_and_media(self):
        self.assertEqual(blocks_to_markdown([_block("quote", "hm")]), "> hm")
        self.assertEqual(
            blocks_to_markdown(
                [_block("image", props={"url": "https://x/y.png", "caption": "a chart"})]
            ),
            "![a chart](https://x/y.png)",
        )
        self.assertEqual(
            blocks_to_markdown([_block("file", props={"url": "https://x/y.pdf", "name": "spec"})]),
            "[spec](https://x/y.pdf)",
        )

    def test_table_renders_as_pipes_with_a_header_separator(self):
        body = [
            {
                "type": "table",
                "props": {},
                "content": {
                    "type": "tableContent",
                    "rows": [
                        {"cells": [[_text("field")], [_text("type")]]},
                        {"cells": [[_text("id")], [_text("int")]]},
                    ],
                },
                "children": [],
            }
        ]
        self.assertEqual(
            blocks_to_markdown(body),
            "| field | type |\n| --- | --- |\n| id | int |",
        )


class DegradationTests(SimpleTestCase):
    """`extract_text`'s never-raise contract, inherited."""

    def test_unknown_block_type_keeps_its_text(self):
        body = [_block("someFutureBlock", "still readable")]
        self.assertEqual(blocks_to_markdown(body), "still readable")

    def test_empty_and_malformed_bodies_return_a_string(self):
        for body in (None, [], {}, "", 42, {"nope": 1}, [None, 3, "x"]):
            self.assertIsInstance(blocks_to_markdown(body), str)

    def test_a_plain_string_body_passes_through(self):
        self.assertEqual(blocks_to_markdown("just text"), "just text")

    def test_wrapper_shapes_are_unwrapped(self):
        blocks = [_block("paragraph", "inside")]
        self.assertEqual(blocks_to_markdown({"blocks": blocks}), "inside")


class RoundTripTests(SimpleTestCase):
    """Structure survives markdown -> blocks -> markdown -> blocks.

    Compares block *types*, not strings: the renderer is free to choose
    `-` over `*` as long as the meaning is stable.
    """

    def _types(self, blocks):
        return [b.get("type") for b in blocks]

    def test_a_task_spec_round_trips(self):
        source = (
            "# Goal\n"
            "\n"
            "Ship the thing.\n"
            "\n"
            "## Steps\n"
            "\n"
            "- read the code\n"
            "- write the test\n"
            "\n"
            "1. first\n"
            "2. second\n"
        )
        once = markdown_to_blocks(source)
        twice = markdown_to_blocks(blocks_to_markdown(once))
        self.assertEqual(self._types(once), self._types(twice))
        self.assertEqual(
            self._types(twice),
            [
                "heading",
                "paragraph",
                "heading",
                "bulletListItem",
                "bulletListItem",
                "numberedListItem",
                "numberedListItem",
            ],
        )

    def test_inline_formatting_round_trips(self):
        once = markdown_to_blocks("a **bold** and *italic* and [link](https://x.com)")
        rendered = blocks_to_markdown(once)
        self.assertIn("**bold**", rendered)
        self.assertIn("*italic*", rendered)
        self.assertIn("[link](https://x.com)", rendered)
