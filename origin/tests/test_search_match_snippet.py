"""Spotlight shows the part of a result that matched (MIP-836).

The indexed `snippet_text` is the first 280 characters of the chunk. The
keyword query already highlights `search_text` whole, so the row's text is
windowed from that response: no reindex, and stemmed, half-typed and
Japanese matches count because the analyzer marked them.
"""

from django.test import SimpleTestCase

from origin.search_engine import search as S

PRE, POST = S._HIGHLIGHT_PRE, S._HIGHLIGHT_POST
FILLER = "The quick brown fox keeps walking past the old stone bridge. " * 12  # ~700 chars


def _mark(text, *words):
    for w in words:
        text = text.replace(w, f"{PRE}{w}{POST}")
    return text


def _snip(text, *words, field="search_text"):
    return S._match_snippet({field: [_mark(text, *words)]})


class TestMatchSnippet(SimpleTestCase):
    def test_a_match_at_the_start_keeps_the_indexed_snippet(self):
        self.assertIsNone(_snip("parent " + FILLER, "parent"))

    def test_a_match_in_the_middle_is_windowed(self):
        text = FILLER + "the missing comments are listed here. " + FILLER
        out = _snip(text, "comments")
        self.assertIn("comments", out)
        self.assertTrue(out.startswith("… ") and out.endswith(" …"))
        self.assertLessEqual(len(out), S._MATCH_SNIPPET_CHARS + 4)
        self.assertNotIn(PRE, out)
        self.assertNotIn(POST, out)

    def test_a_match_at_the_end_is_windowed_and_not_trailed_off(self):
        text = FILLER + "and finally the sentence we remember."
        out = _snip(text, "sentence")
        self.assertTrue(out.startswith("… "))
        self.assertFalse(out.endswith("…"))
        self.assertIn("sentence we remember.", out)
        self.assertGreater(len(out), 150)

    def test_the_cut_lands_on_word_boundaries(self):
        out = _snip(FILLER + "needle " + FILLER, "needle")
        words = set(FILLER.split()) | {"needle"}
        for w in out.replace("…", "").split():
            self.assertIn(w, words)

    def test_the_window_holds_the_most_matched_words(self):
        text = "alpha " + FILLER + FILLER + "alpha beta " + FILLER
        out = _snip(text, "alpha", "beta")
        self.assertIn("alpha beta", out)

    def test_a_short_chunk_is_already_whole(self):
        self.assertIsNone(_snip("a short comment about parents", "parents"))

    def test_a_vector_only_hit_has_no_highlight(self):
        self.assertIsNone(S._match_snippet(None))
        self.assertIsNone(S._match_snippet({}))
        self.assertIsNone(S._match_snippet({"title": [f"{PRE}x{POST}"]}))

    def test_only_a_body_field_with_marks_counts(self):
        self.assertIsNone(S._match_snippet({"search_text": [FILLER * 2]}))

    def test_the_stemmed_field_can_be_the_one_that_matched(self):
        # "comm" → "comments": the `.en` copy marks the stemmed form.
        out = _snip(FILLER + "all the comments here. " + FILLER, "comments", field="search_text.en")
        self.assertIn("comments", out)

    def test_japanese_without_spaces(self):
        body = "あいうえお" * 80 + "設定画面" + "かきくけこ" * 80
        out = _snip(body, "設定画面", field="search_text.ja")
        self.assertIn("設定画面", out)
        self.assertLessEqual(len(out), S._MATCH_SNIPPET_CHARS + 4)

    def test_whitespace_is_collapsed_like_the_indexed_snippet(self):
        out = _snip(FILLER + "line one\n\n  line   two needle " + FILLER, "needle")
        self.assertIn("line one line two needle", out)


class TestMatchSnippetReachesTheRow(SimpleTestCase):
    def _kw_hit(self, cid, highlight, entity="task:1", chunk_type="task_comment"):
        return {
            "_id": cid,
            "_source": {
                "chunk_id": cid,
                "entity_type": "task",
                "entity_id": entity,
                "chunk_type": chunk_type,
                "snippet_text": "top of the body",
            },
            "highlight": highlight,
            "_lexical_tier": S.TIER_ALL_WORDS,
        }

    def _fused(self, hits):
        return S._rrf_fuse(hits, [])

    def test_a_keyword_hit_carries_its_passage_and_a_vector_hit_does_not(self):
        marked = {"search_text": [_mark(FILLER + "needle " + FILLER, "needle")]}
        kw = [self._kw_hit("k", marked)]
        vec = [
            {"_id": "v", "_source": {"chunk_id": "v", "entity_type": "task", "entity_id": "task:2"}}
        ]
        fused = {c["chunk_id"]: c for c in S._rrf_fuse(kw, vec)}
        self.assertIn("needle", fused["k"]["match_snippet"])
        self.assertNotIn("match_snippet", fused["v"])

    def test_the_row_swaps_in_the_passage_and_drops_the_private_key(self):
        marked = {"search_text": [_mark(FILLER + "needle " + FILLER, "needle")]}
        (row,) = S._group_by_entity(self._fused([self._kw_hit("k", marked)]))
        self.assertEqual(row["snippet"], "top of the body")  # untouched until the final pass
        S._apply_match_snippets([row])
        self.assertIn("needle", row["snippet"])
        self.assertNotIn("_match_snippet", row)

    def test_no_passage_keeps_the_indexed_snippet(self):
        (row,) = S._group_by_entity(self._fused([self._kw_hit("k", None)]))
        S._apply_match_snippets([row])
        self.assertEqual(row["snippet"], "top of the body")

    def test_the_agent_shape_is_untouched(self):
        marked = {"search_text": [_mark(FILLER + "needle " + FILLER, "needle")]}
        (row,) = S._group_by_entity(self._fused([self._kw_hit("k", marked)]), for_agent=True)
        self.assertNotIn("_match_snippet", row)
        S._apply_match_snippets([row])
        self.assertEqual(row["snippet"], "top of the body")

    def test_the_better_matching_chunk_supplies_the_passage(self):
        title = self._kw_hit("title", None, chunk_type="task_title_content")
        title["_lexical_tier"] = S.TIER_SOME_WORDS
        marked = {"search_text": [_mark(FILLER + "needle " + FILLER, "needle")]}
        comment = self._kw_hit("comment", marked)
        comment["_lexical_tier"] = S.TIER_PHRASE
        (row,) = S._group_by_entity(self._fused([title, comment]))
        S._apply_match_snippets([row])
        self.assertIn("needle", row["snippet"])
