"""MIP-1328: chip ranking must count EVERY spelling of a citation.

`_rank_sources_by_citation` sorts source chips by whether the answer cites
them. It used to scan only `[type:id]` and `[prose](type:id)`, so a source
cited as a group `[task:1, task:2]` or a raw `(task:1)` / `（task:12）`
counted as uncited and its chip was not ranked leftmost. It now shares
`citation_resolver.extract_citation_tokens` (MIP-1279) with the resolver.
Ordering only: nothing here is model-visible.
"""

from django.test import SimpleTestCase

from origin.search_engine.agent.controller import _rank_sources_by_citation

_UUID = "8f5e4733-1a2b-4c3d-9e8f-0123456789ab"


def _src(etype, eid):
    return {"entity_type": etype, "entity_id": eid}


# Three sources in tool-emission order: task:1, task:2, task:3. Each case cites
# some of them; `expected` is the resulting entity_id order (cited first, the
# original order kept inside each bucket).
SOURCES = [_src("task", "task:1"), _src("task", "task:2"), _src("task", "task:3")]

RANK_CASES = [
    # --- forms that already worked ------------------------------------------
    ("bare", "see [task:3]", ["task:3", "task:1", "task:2"]),
    ("link", "it was [ruled out](task:3)", ["task:3", "task:1", "task:2"]),
    # --- grouped --------------------------------------------------------------
    ("group_comma", "see [task:2, task:3]", ["task:2", "task:3", "task:1"]),
    ("group_no_space", "see [task:3,task:2]", ["task:2", "task:3", "task:1"]),
    ("group_semicolon", "see [task:3; task:2]", ["task:2", "task:3", "task:1"]),
    ("group_cjk_comma", "経緯は[task:3、task:2]を参照", ["task:2", "task:3", "task:1"]),
    ("group_fullwidth_comma", "[task:3，task:2]", ["task:2", "task:3", "task:1"]),
    # --- raw ids ----------------------------------------------------------------
    ("raw_paren", "done (task:3) already", ["task:3", "task:1", "task:2"]),
    ("raw_fullwidth_paren", "完了（task:3）です", ["task:3", "task:1", "task:2"]),
    ("raw_fullwidth_brackets", "【task:3】", ["task:3", "task:1", "task:2"]),
    ("raw_group_in_paren", "(task:3, task:2)", ["task:2", "task:3", "task:1"]),
    ("raw_bare_sentence_end", "It is blocked by task:3.", ["task:3", "task:1", "task:2"]),
    ("raw_after_cjk_char", "参照task:3。", ["task:3", "task:1", "task:2"]),
    # --- case + no-cite + near misses ------------------------------------------
    ("uppercase_token_is_not_a_citation", "see [TASK:3]", ["task:1", "task:2", "task:3"]),
    ("no_citation_keeps_order", "nothing cited here", ["task:1", "task:2", "task:3"]),
    ("empty_answer_keeps_order", "", ["task:1", "task:2", "task:3"]),
    (
        "url_is_not_a_citation",
        "https://x.dev/task:3 and docs.task:3",
        ["task:1", "task:2", "task:3"],
    ),
    (
        "external_link_is_not_a_citation",
        "[MDN docs](https://mdn.dev/task)",
        ["task:1", "task:2", "task:3"],
    ),
    ("id_prefix_is_not_the_id", "see [task:30]", ["task:1", "task:2", "task:3"]),
]


_N9 = "note:personal:9"
NOTE_MY_CASES = [
    ("my_bare", "see [note:my:9]", [_N9, "task:1", "note:task:4"]),
    ("personal_bare", "see [note:personal:9]", [_N9, "task:1", "note:task:4"]),
    ("my_link", "the [plan](note:my:9)", [_N9, "task:1", "note:task:4"]),
    ("my_group", "see [note:my:9, note:task:4]", [_N9, "note:task:4", "task:1"]),
    ("my_raw_paren", "done (note:my:9)", [_N9, "task:1", "note:task:4"]),
    ("my_raw_fullwidth", "完了（note:my:9）", [_N9, "task:1", "note:task:4"]),
    ("task_note_still_works", "[note:task:4]", ["note:task:4", "task:1", _N9]),
    ("my_other_id_is_uncited", "see [note:my:90]", ["task:1", _N9, "note:task:4"]),
    ("my_id_prefix_is_not_the_id", "see [note:my:99]", ["task:1", _N9, "note:task:4"]),
]


class TestChipRankCitationForms(SimpleTestCase):
    def test_rank_cases(self):
        for name, answer, expected in RANK_CASES:
            with self.subTest(name):
                ranked = _rank_sources_by_citation(answer, [dict(s) for s in SOURCES])
                self.assertEqual([s["entity_id"] for s in ranked], expected)

    def test_none_answer_is_safe(self):
        ranked = _rank_sources_by_citation(None, [dict(s) for s in SOURCES])  # type: ignore[arg-type]
        self.assertEqual([s["entity_id"] for s in ranked], ["task:1", "task:2", "task:3"])

    def test_empty_sources(self):
        self.assertEqual(_rank_sources_by_citation("[task:1]", []), [])

    def test_mixed_types_in_one_group(self):
        sources = [
            _src("note", "note:personal:9"),
            _src("task", "task:1"),
            _src("project", "project:3"),
        ]
        ranked = _rank_sources_by_citation("see [project:3, note:personal:9]", sources)
        self.assertEqual(
            [s["entity_id"] for s in ranked], ["note:personal:9", "project:3", "task:1"]
        )

    def test_chat_source_without_type_prefix_matches_unprefixed_and_prefixed_cites(self):
        # Chat sources ship `entity_id` without the `chat:` prefix; the answer
        # may cite either spelling, bare or inside a group.
        chat = _src("chat", f"gm:{_UUID}")
        for name, answer in [
            ("prefixed", f"[chat:gm:{_UUID}]"),
            ("unprefixed", f"[gm:{_UUID}]"),
            ("grouped", f"[task:2, chat:gm:{_UUID}]"),
            ("raw", f"(gm:{_UUID})"),
        ]:
            with self.subTest(name):
                ranked = _rank_sources_by_citation(answer, [_src("task", "task:1"), chat])
                self.assertEqual(ranked[0], chat)

    def test_note_my_and_note_personal_are_the_same_citation(self):
        # MIP-1383: the frontend and the resolver accept `[note:my:9]` for a personal
        # note, whose chip carries `note:personal:9`; the ranker used to count it uncited.
        sources = [
            _src("task", "task:1"),
            _src("note", "note:personal:9"),
            _src("note", "note:task:4"),
        ]
        for name, answer, expected in NOTE_MY_CASES:
            with self.subTest(name):
                ranked = _rank_sources_by_citation(answer, [dict(s) for s in sources])
                self.assertEqual([s["entity_id"] for s in ranked], expected)

    def test_uppercase_uuid_in_answer_still_matches(self):
        chat = _src("chat", f"gm:{_UUID}")
        ranked = _rank_sources_by_citation(
            f"[chat:gm:{_UUID.upper()}]", [_src("task", "task:1"), chat]
        )
        self.assertEqual(ranked[0], chat)
