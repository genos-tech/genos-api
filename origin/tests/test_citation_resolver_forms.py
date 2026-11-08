"""MIP-1279: the citation resolver must back-fill EVERY spelling of a citation.

Models cite a source as a single `[task:1]`, a link `[prose](task:1)`, a
GROUP `[task:1, task:2]`, or a raw id `(task:1)` / `（task:12）` / bare
`task:1` in a sentence. The frontend strips all of them (citationUtils.ts);
the resolver used to match only the first two, so a source cited in the
other spellings was never back-filled and got no chip.

The extractor tests are pure (no DB). The resolver tests drive the real
`resolve_unresolved_citations` against real rows, so ACL and `seen_keys`
behaviour is pinned for the new forms too.
"""

from django.test import SimpleTestCase

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import citation_resolver as cr
from origin.tests.test_base import BaseAPITestCase

_UUID = "8f5e4733-1a2b-4c3d-9e8f-0123456789ab"
_UUID2 = "9c17aa01-2b3c-4d5e-8f60-ba9876543210"

# (case name, answer, expected tokens)
EXTRACT_CASES = [
    # --- forms that already worked: must keep working ---------------------
    ("single", "see [task:1] now", {"task:1"}),
    ("link", "the team [ruled it out](task:42) early", {"task:42"}),
    ("link_and_bare_same_id", "[why](task:7) and again [task:7]", {"task:7"}),
    ("note_personal", "[note:personal:50]", {"note:personal:50"}),
    ("todo_item", "[todo:2026-07-03:item:117]", {"todo:2026-07-03:item:117"}),
    ("chat_thread", f"[chat:gm:{_UUID}:thread:{_UUID2}]", {f"chat:gm:{_UUID}:thread:{_UUID2}"}),
    # --- grouped ------------------------------------------------------------
    ("group_comma", "[task:1, task:2]", {"task:1", "task:2"}),
    ("group_no_space", "[task:1,task:2]", {"task:1", "task:2"}),
    ("group_semicolon", "[task:1; task:2]", {"task:1", "task:2"}),
    ("group_cjk_comma", "[task:1、task:2]", {"task:1", "task:2"}),
    ("group_fullwidth_comma", "[task:1，task:2]", {"task:1", "task:2"}),
    ("group_three", "[task:1, task:2, task:3]", {"task:1", "task:2", "task:3"}),
    (
        "group_mixed_types",
        "[task:1, project:3, milestone:9]",
        {"task:1", "project:3", "milestone:9"},
    ),
    ("group_note_ids", "[note:personal:5, note:task:6]", {"note:personal:5", "note:task:6"}),
    ("group_in_cjk_sentence", "経緯は[task:12、task:13]を参照", {"task:12", "task:13"}),
    # --- raw ids ---------------------------------------------------------------
    ("raw_paren", "done (task:1) already", {"task:1"}),
    ("raw_fullwidth_paren", "完了（task:12）です", {"task:12"}),
    ("raw_fullwidth_brackets", "【task:5】と［task:6］", {"task:5", "task:6"}),
    ("raw_corner_brackets", "「task:5」", {"task:5"}),
    ("raw_group_in_paren", "(task:1, task:2)", {"task:1", "task:2"}),
    ("raw_group_fullwidth_paren", "（task:12、task:13）", {"task:12", "task:13"}),
    ("raw_bare_sentence_end", "It is blocked by task:42.", {"task:42"}),
    ("raw_bare_list", "task:1, task:2 and project:3", {"task:1", "task:2", "project:3"}),
    ("raw_after_cjk_char", "参照task:12。", {"task:12"}),
    ("raw_note", "see note:personal:50 for it", {"note:personal:50"}),
    ("raw_todo", "todo:2026-07-03:item:117 is open", {"todo:2026-07-03:item:117"}),
    ("raw_chat_prefixed", f"in chat:dm:{_UUID}.", {f"chat:dm:{_UUID}"}),
    (
        "raw_chat_unprefixed",
        f"in gm:{_UUID}:thread:{_UUID2} today",
        {f"chat:gm:{_UUID}:thread:{_UUID2}"},
    ),
    ("raw_trailing_colon", "task:42: blocked", {"task:42"}),
    # --- must NOT match -------------------------------------------------------------
    ("neg_prose_brackets", "[reminder: ship Friday]", set()),
    ("neg_word_note", "note: remember to ship", set()),
    ("neg_word_chat", "chat: hello", set()),
    ("neg_no_digit", "task:done and note:mine", set()),
    ("neg_url_path", "https://example.com/project:18 and /task:5", set()),
    ("neg_dotted_path", "docs.task:5", set()),
    ("neg_pm_clock", "see you at 5 pm:30", set()),
    ("neg_unprefixed_chat_without_uuid", "gm:12 and dm:5", set()),
    ("neg_plain", "no citations here", set()),
    ("neg_empty", "", set()),
]


class TestExtractCitationTokens(SimpleTestCase):
    def test_table(self):
        for name, answer, expected in EXTRACT_CASES:
            with self.subTest(name):
                self.assertEqual(cr.extract_citation_tokens(answer), expected)


class TestResolveGroupedAndRawCitations(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.team_id = str(self.team.team_id)
        self.user_id = str(self.user.id)
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="CitProj", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.t1 = self._task("First")
        self.t2 = self._task("Second")

    def _task(self, title):
        return TaskMaster.objects.create(
            team=self.team, project=self.project, title=title, status="open"
        )

    def _resolve(self, answer, seen_keys=None):
        return cr.resolve_unresolved_citations(
            answer=answer,
            seen_keys=seen_keys or set(),
            team_id=self.team_id,
            user_id=self.user_id,
            build_task_source=lambda tid, title, pid: {"kind": "task", "id": tid, "title": title},
            build_project_source=lambda *a: {"kind": "project", "args": a},
            build_chat_source=lambda *a: {"kind": "chat", "args": a},
            build_note_source=lambda *a: {"kind": "note", "args": a},
            build_todo_source=lambda *a: {"kind": "todo", "args": a},
            build_milestone_source=lambda *a: {"kind": "milestone", "args": a},
        )

    def _ids(self, out):
        return sorted(s["id"] for s in out)

    def test_grouped_citation_resolves_every_member(self):
        a, b = self.t1.task_id, self.t2.task_id
        for answer in (
            f"both [task:{a}, task:{b}] matter",
            f"both [task:{a},task:{b}] matter",
            f"both [task:{a}; task:{b}] matter",
            f"両方[task:{a}、task:{b}]が重要",
            f"両方[task:{a}，task:{b}]が重要",
        ):
            with self.subTest(answer):
                self.assertEqual(self._ids(self._resolve(answer)), sorted([a, b]))

    def test_raw_id_citation_resolves(self):
        a = self.t1.task_id
        for answer in (
            f"see (task:{a}) for details",
            f"詳細は（task:{a}）を参照",
            f"It is tracked in task:{a}.",
            f"参照task:{a}。",
        ):
            with self.subTest(answer):
                self.assertEqual(self._ids(self._resolve(answer)), [a])

    def test_grouped_skips_seen_member_and_keeps_the_other(self):
        a, b = self.t1.task_id, self.t2.task_id
        out = self._resolve(f"[task:{a}, task:{b}]", seen_keys={("task", f"task:{a}")})
        self.assertEqual(self._ids(out), [b])

    def test_grouped_member_the_user_cannot_see_is_dropped(self):
        hidden_project = ProjectMaster.objects.create(
            team=self.team, project_name="Hidden", owner=self.user2
        )
        ProjectMembers.objects.create(team=self.team, project=hidden_project, attendee=self.user2)
        hidden = TaskMaster.objects.create(
            team=self.team,
            project=hidden_project,
            title="Hidden",
            status="open",
            assignee=self.user2,
            reporter=self.user2,
        )
        a = self.t1.task_id
        out = self._resolve(f"[task:{a}, task:{hidden.task_id}]")
        self.assertEqual(self._ids(out), [a])

    def test_unknown_id_in_group_does_not_break_the_rest(self):
        a = self.t1.task_id
        out = self._resolve(f"[task:{a}, task:99999999]")
        self.assertEqual(self._ids(out), [a])
