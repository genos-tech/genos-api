"""Tests for `markdown_to_blocks` — the agent's markdown answer → BlockNote
body converter used when saving an answer into a note.
"""

from django.test import SimpleTestCase

from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.search_engine.text_extraction import extract_sections, extract_text

_ANSWER = """**What is pglogical?**

pglogical is a *logical* replication extension for PostgreSQL.

## Key Features & Capabilities

- Selective replication via replication sets
- Cross-version replication
- Row and column filtering

## Common use cases

1. Zero-downtime major-version upgrades
2. Consolidating shards into a warehouse

See the [docs](https://example.com/pglogical) for details."""


class TestMarkdownToBlocks(SimpleTestCase):
    def test_empty_is_title_only(self):
        self.assertEqual(markdown_to_blocks(""), [])
        self.assertEqual(markdown_to_blocks("   "), [])

    def test_heading_levels(self):
        blocks = markdown_to_blocks("# A\n## B\n### C\n#### D")
        self.assertEqual([b["type"] for b in blocks], ["heading"] * 4)
        self.assertEqual([b["props"]["level"] for b in blocks], [1, 2, 3, 3])

    def test_bullet_and_numbered_lists(self):
        blocks = markdown_to_blocks("- one\n- two\n\n1. first\n2. second")
        self.assertEqual(
            [b["type"] for b in blocks],
            ["bulletListItem", "bulletListItem", "numberedListItem", "numberedListItem"],
        )

    def test_inline_bold_italic(self):
        [para] = markdown_to_blocks("this is **bold** and *italic* text")
        styles = [(c["text"], c["styles"]) for c in para["content"]]
        self.assertIn(("bold", {"bold": True}), styles)
        self.assertIn(("italic", {"italic": True}), styles)

    def test_url_link_becomes_link_node_but_citation_degrades_to_prose(self):
        [para] = markdown_to_blocks("see [docs](https://x.com) and [that task](task:5)")
        types = [c["type"] for c in para["content"]]
        self.assertIn("link", types)
        link = next(c for c in para["content"] if c["type"] == "link")
        self.assertEqual(link["href"], "https://x.com")
        # The non-URL citation target keeps its prose, no dead link.
        flat = "".join(c.get("text", "") for c in para["content"] if c["type"] == "text")
        self.assertIn("that task", flat)

    def test_multi_section_answer_is_structured(self):
        blocks = markdown_to_blocks(_ANSWER)
        types = [b["type"] for b in blocks]
        self.assertGreaterEqual(types.count("heading"), 2)
        self.assertIn("bulletListItem", types)
        self.assertIn("numberedListItem", types)

    def test_chunker_reads_all_sections(self):
        # The saved body must round-trip through the reindex chunker:
        # heading-bounded sections, no lost text.
        blocks = markdown_to_blocks(_ANSWER)
        sections = extract_sections(blocks)
        headings = [h for h, _ in sections if h]
        self.assertIn("Key Features & Capabilities", headings)
        self.assertIn("Common use cases", headings)
        # No content lost — a marker from the last section survives.
        self.assertIn("Consolidating shards", extract_text(blocks))
