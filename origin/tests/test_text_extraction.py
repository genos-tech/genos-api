"""Regression tests for BlockNote plain-text extraction (search_engine).

`origin.search_engine.text_extraction` turns a BlockNote-style JSON body
into the plain string that every chunker feeds to OpenSearch as
`search_text`. These are pure (no DB / network) tests of that transform.

Focus: **code blocks**. BlockNote declares its code block with
`content: "inline"` (see `@blocknote/core` `blocks/Code/block.ts`), so a
code block stores its source exactly like a paragraph — an inline
`content` array of `{"type": "text", "text": ...}` nodes:

    {"type": "codeBlock", "props": {"language": "python"},
     "content": [{"type": "text", "text": "print(1)", "styles": {}}],
     "children": []}

Because `_walk_block` walks `content` for *every* block type (it only
special-cases `heading` for section splitting), code-block source already
flows into `search_text` and is searchable — verified end-to-end against
live OpenSearch data. Nothing special is needed to index it.

The extraction is therefore correct *by structure*, not by an explicit
code-block branch. These tests pin that guarantee so a future refactor of
`_walk_block` (e.g. adding block-type-specific handling) can't silently
drop code-block contents from the index.
"""

from django.test import SimpleTestCase, TestCase

from origin.search_engine.text_extraction import extract_sections, extract_text


def _code_block(text: str, language: str = "javascript") -> dict:
    """A code block shaped exactly like the rows BlockNote persists."""
    return {
        "id": "cb-1",
        "type": "codeBlock",
        "props": {"language": language},
        "content": [{"text": text, "type": "text", "styles": {}}],
        "children": [],
    }


def _paragraph(text: str) -> dict:
    return {"type": "paragraph", "content": [{"type": "text", "text": text}]}


def _heading(text: str) -> dict:
    return {"type": "heading", "content": [{"type": "text", "text": text}]}


class TestCodeBlockExtraction(TestCase):
    def test_extract_text_includes_code_block_source(self):
        body = [
            _paragraph("Intro line"),
            _code_block("const x = 1;"),
        ]
        out = extract_text(body)
        self.assertIn("Intro line", out)
        self.assertIn("const x = 1;", out)

    def test_extract_text_preserves_multiline_code(self):
        # A multi-line code block is a single text node with embedded
        # newlines (BlockNote inserts "\n" on Enter inside a code block).
        body = [_code_block("def add(a, b):\n    return a + b", language="python")]
        out = extract_text(body)
        self.assertIn("def add(a, b):", out)
        self.assertIn("return a + b", out)

    def test_code_only_body_is_not_dropped(self):
        # A body whose only block is a code block must still yield text;
        # otherwise a code-only message/note would index nothing.
        body = [_code_block("SELECT * FROM users;", language="sql")]
        self.assertEqual(extract_text(body), "SELECT * FROM users;")

    def test_code_block_nested_in_list_item_children(self):
        # `_walk_block` recurses into `children`, so a code block nested
        # under a list item is still captured.
        body = [
            {
                "type": "bulletListItem",
                "content": [{"type": "text", "text": "Run:"}],
                "children": [_code_block("ls -la", language="bash")],
            }
        ]
        out = extract_text(body)
        self.assertIn("Run:", out)
        self.assertIn("ls -la", out)

    def test_extract_sections_keeps_code_under_its_heading(self):
        # Section splitting must route a code block into the body of the
        # heading it falls under (a code block is not itself a heading).
        body = [
            _heading("Example"),
            _code_block("print('hi')", language="python"),
        ]
        sections = extract_sections(body)
        self.assertEqual(len(sections), 1)
        heading, section_body = sections[0]
        self.assertEqual(heading, "Example")
        self.assertIn("print('hi')", section_body)

    def test_extract_sections_code_only_section(self):
        # No headings, code block only -> one heading-less section that
        # still carries the code (mirrors `extract_text`).
        body = [_code_block("echo hello", language="bash")]
        sections = extract_sections(body)
        self.assertEqual(sections, [("", "echo hello")])


# Math and diagram blocks (MIP-1023). The source of a block flows into the search
# text by structure (a plain-text run in `content`, like a code block); inline
# math keeps its TeX in a plain string, which needs its own branch.
class TestMathExtraction(SimpleTestCase):
    @staticmethod
    def _body():
        return [
            {
                "type": "heading",
                "props": {"level": 2},
                "content": [{"type": "text", "text": "Energy", "styles": {}}],
            },
            {
                "type": "paragraph",
                "props": {},
                "content": [
                    {"type": "text", "text": "Einstein: ", "styles": {}},
                    {"type": "math", "props": {}, "content": "E=mc^2"},
                ],
            },
            {
                "type": "mathBlock",
                "props": {},
                "content": [{"type": "text", "text": "a^2+b^2=c^2", "styles": {}}],
            },
            {
                "type": "diagram",
                "props": {},
                "content": [{"type": "text", "text": "graph LR; Start-->Done", "styles": {}}],
            },
        ]

    def test_extract_text_indexes_inline_math_and_both_block_sources(self):
        text = extract_text(self._body())
        for expected in ("Einstein:", "E=mc^2", "a^2+b^2=c^2", "graph LR; Start-->Done"):
            self.assertIn(expected, text)

    def test_extract_sections_keeps_them_under_their_heading(self):
        sections = extract_sections(self._body())
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0][0], "Energy")
        self.assertIn("E=mc^2", sections[0][1])
        self.assertIn("Start-->Done", sections[0][1])

    def test_an_empty_inline_math_node_adds_nothing(self):
        body = [
            {
                "type": "paragraph",
                "props": {},
                "content": [{"type": "math", "props": {}, "content": " "}],
            }
        ]
        self.assertEqual(extract_text(body), "")
