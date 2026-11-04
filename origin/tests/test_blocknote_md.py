"""Tests for `markdown_to_blocks` — the agent's markdown answer → BlockNote
body converter used when saving an answer into a note.
"""

import time

from django.test import SimpleTestCase

from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.text_extraction import extract_sections, extract_text


def _fake_resolver(token):
    """Stands in for entity_links.resolve_note_entity_link: knows one
    task and one project, misses everything else."""
    return {
        "task:7": ("/workspace/tasks/project/3/task/7", "WRD-7"),
        "project:3": ("/workspace/tasks/project/3", "Website Redesign"),
    }.get(token)


def _text(text, styles=None):
    return {"type": "text", "text": text, "styles": styles or {}}


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

    def _runs(self, line):
        [para] = markdown_to_blocks(line)
        return [(c["text"], sorted(c["styles"])) for c in para["content"]]

    def test_emphasis_nests(self):
        """MIP-705: a regex alternation can't nest, so `*it*` inside bold
        came back as literal asterisks."""
        self.assertEqual(
            self._runs("**bold *it* more**"),
            [("bold ", ["bold"]), ("it", ["bold", "italic"]), (" more", ["bold"])],
        )
        self.assertEqual(self._runs("***x***"), [("x", ["bold", "italic"])])
        self.assertEqual(self._runs("~~**x**~~"), [("x", ["bold", "strike"])])
        self.assertEqual(self._runs("**a *b***"), [("a ", ["bold"]), ("b", ["bold", "italic"])])
        self.assertEqual(
            self._runs("*a ~~**x** y~~*"),
            [("a ", ["italic"]), ("x", ["bold", "italic", "strike"]), (" y", ["italic", "strike"])],
        )
        # The `**` in ` ~~**c` only opens: closing on the `~` before it
        # would steal the enclosing `*`.
        self.assertEqual(
            self._runs("***a** b ~~**c** d~~*"),
            [
                ("a", ["bold", "italic"]),
                (" b ", ["italic"]),
                ("c", ["bold", "italic", "strike"]),
                (" d", ["italic", "strike"]),
            ],
        )

    def test_emphasis_edge_cases_stay_put(self):
        self.assertEqual(self._runs("snake_case_name"), [("snake_case_name", [])])
        self.assertEqual(self._runs("**unclosed and *open"), [("**unclosed and *open", [])])
        self.assertEqual(self._runs("a ** b"), [("a ** b", [])])
        # The closer sits between punctuation and a letter, which plain
        # CommonMark refuses — Japanese bodies are written this way.
        self.assertEqual(self._runs("**注意:**本文"), [("注意:", ["bold"]), ("本文", [])])
        # Rule of 3: the inner `**` can't pair with a single `*`.
        self.assertEqual(self._runs("*a**b*"), [("a**b", ["italic"])])
        # Pairs never cross: the `_` inside the italic can't reach out.
        self.assertEqual(self._runs("**a _b* c_"), [("*", []), ("a _b", ["italic"]), (" c_", [])])

    def test_a_line_of_markers_is_linear_not_recursive(self):
        # (A line of nothing but `*` is a divider, hence the leading `x `.)
        lines = (
            "x " + "*" * 50_000,
            "*a " * 20_000,
            "**" * 20_000 + "x",
            "x " + "_" * 50_000,
            # Closers with no opener of their kind: each would rescan
            # every `_` below it without CommonMark's openers_bottom —
            # 0.03s with it, ~18s without.
            "_a " * 20_000 + "a* " * 20_000,
        )
        started = time.monotonic()
        for line in lines:
            text = "".join(t for t, _ in self._runs(line))
            self.assertTrue(text == line.strip(), line[:20])  # no 60k-char diff
        self.assertLess(time.monotonic() - started, 3)

    def test_url_link_becomes_link_node_but_citation_degrades_to_prose(self):
        [para] = markdown_to_blocks("see [docs](https://x.com) and [that task](task:5)")
        types = [c["type"] for c in para["content"]]
        self.assertIn("link", types)
        link = next(c for c in para["content"] if c["type"] == "link")
        self.assertEqual(link["href"], "https://x.com")
        # The non-URL citation target keeps its prose, no dead link.
        flat = "".join(c.get("text", "") for c in para["content"] if c["type"] == "text")
        self.assertIn("that task", flat)

    def test_citation_link_resolves_to_in_app_href_with_resolver(self):
        [para] = markdown_to_blocks(
            "see [the Safari task](task:7) in [Website Redesign](project:3)",
            entity_link_resolver=_fake_resolver,
        )
        links = [c for c in para["content"] if c["type"] == "link"]
        self.assertEqual(
            [(ln["href"], ln["content"][0]["text"]) for ln in links],
            [
                # The model's prose stays the label; href is the app route.
                ("/workspace/tasks/project/3/task/7", "the Safari task"),
                ("/workspace/tasks/project/3", "Website Redesign"),
            ],
        )

    def test_bare_token_resolves_with_entity_label(self):
        [para] = markdown_to_blocks(
            "tracked in [task:7] this sprint", entity_link_resolver=_fake_resolver
        )
        link = next(c for c in para["content"] if c["type"] == "link")
        self.assertEqual(link["href"], "/workspace/tasks/project/3/task/7")
        # Bare tokens have no prose — the resolver's label is used.
        self.assertEqual(link["content"][0]["text"], "WRD-7")
        flat = "".join(c.get("text", "") for c in para["content"] if c["type"] == "text")
        self.assertIn("this sprint", flat)

    def test_unresolvable_citation_degrades_even_with_resolver(self):
        [para] = markdown_to_blocks(
            "see [gone](task:999) and [note:personal:1]", entity_link_resolver=_fake_resolver
        )
        self.assertEqual([c["type"] for c in para["content"] if c["type"] == "link"], [])
        flat = "".join(c.get("text", "") for c in para["content"] if c["type"] == "text")
        self.assertIn("gone", flat)  # prose kept for the inline form
        self.assertIn("[note:personal:1]", flat)  # bare token left literal

    def test_multi_section_answer_is_structured(self):
        blocks = markdown_to_blocks(_ANSWER)
        types = [b["type"] for b in blocks]
        self.assertGreaterEqual(types.count("heading"), 2)
        self.assertIn("bulletListItem", types)
        self.assertIn("numberedListItem", types)

    def test_inline_code_strike_and_literal_code_span(self):
        [para] = markdown_to_blocks("run `make test`, ~~old~~, and `**not bold** [a](https://b.c)`")
        styles = [(c.get("text"), c.get("styles")) for c in para["content"]]
        self.assertIn(("make test", {"code": True}), styles)
        self.assertIn(("old", {"strike": True}), styles)
        # Code-span text is literal: no bold, no link node out of it.
        self.assertIn(("**not bold** [a](https://b.c)", {"code": True}), styles)
        self.assertNotIn("link", [c["type"] for c in para["content"]])

    def test_emphasis_wrapping_a_code_span_styles_it(self):
        """`- **`x` decision:**` is how models label a list item; the
        markers must pair across the code span, not leak as `**`."""
        [item] = markdown_to_blocks("- **`isAutoLinked` decision:** keep it")
        self.assertEqual(
            [(c["text"], c["styles"]) for c in item["content"]],
            [
                ("isAutoLinked", {"code": True, "bold": True}),
                (" decision:", {"bold": True}),
                (" keep it", {}),
            ],
        )
        for md, style in (("*`a` b*", "italic"), ("~~`a` b~~", "strike"), ("__`a` b__", "bold")):
            [para] = markdown_to_blocks(md)
            self.assertEqual(
                [(c["text"], c["styles"]) for c in para["content"]],
                [("a", {"code": True, style: True}), (" b", {style: True})],
                md,
            )

    def test_emphasis_wrapping_a_link_styles_its_label(self):
        [para] = markdown_to_blocks("**see [docs](https://x.com) now** then")
        self.assertEqual(
            [(c["type"], c.get("text"), c.get("styles")) for c in para["content"]],
            [
                ("text", "see ", {"bold": True}),
                ("link", None, None),
                ("text", " now", {"bold": True}),
                ("text", " then", {}),
            ],
        )
        self.assertEqual(para["content"][1]["content"], [_text("docs", {"bold": True})])

    def test_emphasis_wrapping_resolved_and_unresolved_citations(self):
        [para] = markdown_to_blocks(
            "**[task:7] and [gone](task:999) and [the site](project:3)**",
            entity_link_resolver=_fake_resolver,
        )
        content = para["content"]
        self.assertEqual(
            [(c["type"], c.get("text"), c.get("styles")) for c in content],
            [
                ("link", None, None),
                # The unresolved citation's prose joins the bold run.
                ("text", " and gone and ", {"bold": True}),
                ("link", None, None),
            ],
        )
        self.assertEqual(content[0]["content"], [_text("WRD-7", {"bold": True})])
        self.assertEqual(content[2]["content"], [_text("the site", {"bold": True})])

    def test_emphasis_inside_a_link_label(self):
        [para] = markdown_to_blocks("[**bold** label](https://x.com)")
        self.assertEqual(
            para["content"][0]["content"],
            [_text("bold", {"bold": True}), _text(" label")],
        )

    def test_atoms_leave_unpaired_markers_and_placeholder_char_literal(self):
        """An unclosed `**` stays literal, and a literal U+FFFC (the
        atom placeholder) in the text is not mistaken for an atom."""
        [para] = markdown_to_blocks("**open `x` here and ￼ [a](https://x.com)")
        self.assertEqual(
            [(c["type"], c.get("text"), c.get("styles")) for c in para["content"]],
            [
                ("text", "**open ", {}),
                ("text", "x", {"code": True}),
                ("text", " here and ￼ ", {}),
                ("link", None, None),
            ],
        )

    def test_fenced_code_block_is_literal_with_canonical_language(self):
        blocks = markdown_to_blocks(
            "```py\n# not a heading\n- not a bullet\n```\n\n```\nplain\n```"
        )
        self.assertEqual([b["type"] for b in blocks], ["codeBlock", "codeBlock"])
        # Aliases resolve to the editor's language ids, like its ``` shortcut.
        self.assertEqual([b["props"]["language"] for b in blocks], ["python", "text"])
        self.assertEqual(
            blocks[0]["content"],
            [{"type": "text", "text": "# not a heading\n- not a bullet", "styles": {}}],
        )

    def test_pipe_table_becomes_table_block(self):
        [table] = markdown_to_blocks(
            "| Want | Tool |\n| :--- | ---: |\n| Milestone | `create_task_plan` ✅ |\n| x \\| y |"
        )
        self.assertEqual(table["type"], "table")
        content = table["content"]
        self.assertEqual((content["type"], content["headerRows"]), ("tableContent", 1))
        rows = [[cell["content"] for cell in row["cells"]] for row in content["rows"]]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(len(r) == 2 for r in rows))  # ragged row padded
        self.assertEqual(
            rows[1][1][0], {"type": "text", "text": "create_task_plan", "styles": {"code": True}}
        )
        self.assertEqual(rows[2][0][0]["text"], "x | y")  # escaped pipe is a literal
        self.assertEqual(rows[2][1], [])

    def test_table_right_under_a_paragraph_line_still_parses(self):
        # Models often skip the blank line between an intro and the table.
        blocks = markdown_to_blocks("Comparison:\n| a | b |\n| --- | --- |\n| 1 | 2 |")
        self.assertEqual([b["type"] for b in blocks], ["paragraph", "table"])

    def test_prose_with_a_pipe_is_not_a_table(self):
        [para] = markdown_to_blocks("a | b, with no separator row")
        self.assertEqual(para["type"], "paragraph")

    def test_quote_checklist_divider_and_nesting(self):
        blocks = markdown_to_blocks(
            "> one\n> two\n\n- [ ] todo\n- [x] done\n- parent\n  - child\n    1. grandchild\n\n---"
        )
        self.assertEqual(
            [b["type"] for b in blocks],
            ["quote", "checkListItem", "checkListItem", "bulletListItem", "divider"],
        )
        self.assertEqual(blocks[0]["content"][0]["text"], "one two")
        self.assertEqual([b["props"]["checked"] for b in blocks[1:3]], [False, True])
        [child] = blocks[3]["children"]
        self.assertEqual(child["type"], "bulletListItem")
        self.assertEqual(child["children"][0]["type"], "numberedListItem")

    def test_rich_body_round_trips_through_the_renderer(self):
        # content_markdown is rendered from the stored blocks, so a body
        # re-saved from it must not drift — including a code span that
        # itself holds a backtick.
        md = (
            "## Plan\n\nUse `` `tick` `` and **bold**.\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n"
            "```python\nx = 1\n```\n\n> note\n\n- [x] done\n  - nested\n\n---\n\nend"
        )
        once = blocks_to_markdown(markdown_to_blocks(md))
        self.assertIn("`` `tick` ``", once)
        self.assertEqual(blocks_to_markdown(markdown_to_blocks(once)), once)
        self.assertEqual(
            [b["type"] for b in markdown_to_blocks(once)],
            [b["type"] for b in markdown_to_blocks(md)],
        )

    def test_emphasis_around_atoms_survives_a_read_modify_write(self):
        """An agent edits a body via get_task's content_markdown, so
        emphasis on code and links must render back to markers the
        parser pairs again — never to literal `**`."""
        md = (
            "- **`isAutoLinked` decision:** keep\n"
            "- **see [docs](https://x.com) now** and *`a` b* and ~~`c`~~"
        )
        blocks = markdown_to_blocks(md)
        once = blocks_to_markdown(blocks)
        self.assertEqual(markdown_to_blocks(once), blocks)
        texts = [c.get("text", "") for b in blocks for c in b["content"]]
        self.assertFalse([t for t in texts if "*" in t or "~" in t], texts)

    def test_chunker_indexes_table_cells(self):
        blocks = markdown_to_blocks(
            "## Tools\n\n| Want | Tool |\n| --- | --- |\n| Milestone | create_task_plan |"
        )
        self.assertIn("create_task_plan", extract_text(blocks))
        self.assertEqual(
            extract_sections(blocks), [("Tools", "Want Tool Milestone create_task_plan")]
        )

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


# ---------------------------------------------------------------------------
# Math and diagram blocks (MIP-1023). The shapes are the ones a real
# BlockNote 0.57 editor holds for `@blocknote/math-block` and
# `@blocknote/diagram-block`, recorded from the editor itself for the
# genos-collab tests: `mathBlock` and `diagram` have no props and their source
# is ONE plain text run; inline `math` has no props and its source is a plain
# STRING in `content`.
# ---------------------------------------------------------------------------


def _math(source):
    return {"type": "math", "props": {}, "content": source}


def _source_block(block_type, source):
    return {
        "type": block_type,
        "props": {},
        "content": [_text(source)] if source else [],
        "children": [],
    }


_DIAGRAM_SOURCE = "graph TD\n  A[Start] --> B{Ok?}\n  B -->|yes| C[Done]"


class TestMathAndDiagramBlocks(SimpleTestCase):
    def test_math_fence_is_a_math_block(self):
        self.assertEqual(
            markdown_to_blocks("```math\nx^2 + y^2 = z^2\n```"),
            [_source_block("mathBlock", "x^2 + y^2 = z^2")],
        )

    def test_mermaid_fence_is_a_diagram(self):
        self.assertEqual(
            markdown_to_blocks(f"```mermaid\n{_DIAGRAM_SOURCE}\n```"),
            [_source_block("diagram", _DIAGRAM_SOURCE)],
        )

    def test_fence_tag_and_marker_variants(self):
        self.assertEqual(
            markdown_to_blocks("```Mermaid\ngraph LR; A-->B\n```")[0]["type"], "diagram"
        )
        self.assertEqual(markdown_to_blocks("~~~MATH\nx\n~~~")[0]["type"], "mathBlock")
        self.assertEqual(markdown_to_blocks("````math\nx\n````")[0]["type"], "mathBlock")

    def test_empty_fences_make_empty_blocks(self):
        self.assertEqual(markdown_to_blocks("```math\n```"), [_source_block("mathBlock", "")])
        self.assertEqual(markdown_to_blocks("```mermaid\n\n```"), [_source_block("diagram", "")])

    def test_other_fences_stay_code(self):
        # `latex` / `tex` are code (a LaTeX document), only `math` is a formula.
        for tag, language in (
            ("python", "python"),
            ("latex", "latex"),
            ("tex", "latex"),
            ("js", "javascript"),
        ):
            block = markdown_to_blocks(f"```{tag}\nx\n```")[0]
            self.assertEqual(
                (block["type"], block["props"]["language"]), ("codeBlock", language), tag
            )

    def test_dollar_display_math_forms(self):
        for markdown, source in (
            ("$$\nx^2\n$$", "x^2"),
            ("$$ x^2 $$", "x^2"),
            ("$$x^2$$", "x^2"),
            ("$$a = b \\\\\n  c = d\n$$", "a = b \\\\\nc = d"),
            ("$$ a = b\nc = d $$", "a = b\nc = d"),
        ):
            self.assertEqual(
                markdown_to_blocks(markdown), [_source_block("mathBlock", source)], markdown
            )

    def test_unclosed_display_math_is_a_paragraph(self):
        blocks = markdown_to_blocks("$$\nx^2")
        self.assertEqual([b["type"] for b in blocks], ["paragraph"])

    def test_display_math_and_fences_end_a_paragraph(self):
        blocks = markdown_to_blocks(
            "intro\n$$\nx\n$$\nmiddle\n```mermaid\ngraph LR; A-->B\n```\noutro"
        )
        self.assertEqual(
            [b["type"] for b in blocks],
            ["paragraph", "mathBlock", "paragraph", "diagram", "paragraph"],
        )

    def test_a_fence_inside_a_math_source_does_not_end_it_early(self):
        blocks = markdown_to_blocks("````mermaid\ngraph LR\n```\nstill source\n````")
        self.assertEqual(blocks, [_source_block("diagram", "graph LR\n```\nstill source")])


class TestInlineMath(SimpleTestCase):
    @staticmethod
    def content(markdown):
        return markdown_to_blocks(markdown)[0]["content"]

    def test_inline_math_between_text(self):
        self.assertEqual(
            self.content("Energy $E=mc^2$ in joules"),
            [_text("Energy "), _math("E=mc^2"), _text(" in joules")],
        )

    def test_two_formulas_in_one_line(self):
        self.assertEqual(self.content("$a$ and $b_1$"), [_math("a"), _text(" and "), _math("b_1")])

    def test_dollar_amounts_stay_text(self):
        for text in (
            "costs $5 and $10",
            "between $5-$10",
            "pay $5.",
            "$5",
            "US$100 or $200 total",
            "a $ b $ c",
            "price: $5, tax: $0.50",
            # Shell and template variables: nothing here is a formula.
            "echo $HOME/$USER",
            "$a$b",
            "x$y$ z",
            "<?= $a ?>/<?= $b ?>",
        ):
            self.assertEqual(self.content(text), [_text(text)], text)

    def test_a_formula_touching_cjk_text_is_still_a_formula(self):
        # CJK text has no spaces around a formula; only ASCII letters and
        # digits keep `$` from opening or closing one.
        self.assertEqual(
            self.content("エネルギーは$E=mc^2$です"),
            [_text("エネルギーは"), _math("E=mc^2"), _text("です")],
        )
        self.assertEqual(self.content("($x$)"), [_text("("), _math("x"), _text(")")])

    def test_escaped_dollar_is_literal(self):
        self.assertEqual(self.content("\\$5 and \\$6"), [_text("\\$5 and \\$6")])

    def test_code_span_wins_over_math(self):
        self.assertEqual(self.content("`$x$`"), [_text("$x$", {"code": True})])

    def test_emphasis_markers_inside_math_are_literal(self):
        self.assertEqual(self.content("$a_1*b_2$"), [_math("a_1*b_2")])

    def test_math_inside_emphasis_keeps_the_emphasis_on_the_text_only(self):
        self.assertEqual(
            self.content("**bold $x$ end**"),
            [_text("bold ", {"bold": True}), _math("x"), _text(" end", {"bold": True})],
        )

    def test_a_link_keeps_a_dollar_in_its_address(self):
        node = self.content("[a](https://example.com/$x$)")[0]
        self.assertEqual((node["type"], node["href"]), ("link", "https://example.com/$x$"))

    def test_double_dollars_inside_a_sentence_stay_text(self):
        self.assertEqual(self.content("a $$x$$ b"), [_text("a $$x$$ b")])

    def test_math_in_heading_list_item_and_table_cell(self):
        heading, bullet, table = markdown_to_blocks(
            "# Area $A$\n\n- uses $B$\n\n| h |\n| - |\n| $C$ |"
        )
        self.assertEqual(heading["content"], [_text("Area "), _math("A")])
        self.assertEqual(bullet["content"], [_text("uses "), _math("B")])
        cell = table["content"]["rows"][1]["cells"][0]
        self.assertIn(_math("C"), cell["content"] if isinstance(cell, dict) else cell)


class TestMathRoundTrip(SimpleTestCase):
    """A body read as markdown and saved again must keep its math and
    diagrams: this is what an agent's `update_note` does."""

    @staticmethod
    def roundtrip(blocks):
        return markdown_to_blocks(blocks_to_markdown(blocks))

    def test_blocks_survive(self):
        for block in (
            _source_block("mathBlock", "x^2 + y^2 = z^2"),
            _source_block("mathBlock", "a = b\\\\\nc = d"),
            _source_block("mathBlock", ""),
            _source_block("diagram", _DIAGRAM_SOURCE),
            _source_block("diagram", ""),
            _source_block("diagram", "graph LR\n```\nsource with a fence\n```"),
        ):
            self.assertEqual(self.roundtrip([block]), [block])

    def test_inline_math_survives(self):
        paragraph = {
            "type": "paragraph",
            "props": {
                "textColor": "default",
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [_text("Energy "), _math("E=mc^2"), _text(" in joules")],
            "children": [],
        }
        self.assertEqual(self.roundtrip([paragraph]), [paragraph])

    def test_an_edit_next_to_a_formula_and_a_diagram_keeps_both(self):
        # What the agent does: read the body, change a sentence, save.
        body = [
            {**_source_block("mathBlock", "E=mc^2")},
            {"type": "paragraph", "props": {}, "content": [_text("old sentence")], "children": []},
            {**_source_block("diagram", _DIAGRAM_SOURCE)},
        ]
        markdown = blocks_to_markdown(body).replace("old sentence", "new sentence")
        saved = markdown_to_blocks(markdown)
        self.assertEqual([b["type"] for b in saved], ["mathBlock", "paragraph", "diagram"])
        self.assertEqual(saved[0]["content"], [_text("E=mc^2")])
        self.assertEqual(saved[2]["content"], [_text(_DIAGRAM_SOURCE)])
