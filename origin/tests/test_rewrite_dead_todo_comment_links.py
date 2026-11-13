"""`rewrite_dead_todo_comment_links`: old to-dos made from a task comment link the task page (MIP-1443).

Before MIP-1436 the "From task comment" link in a to-do's notes was
`<origin>/workspace/chat/pm/<project id>/thread/<task id>/comment/<comment id>`, which opens "No
conversation selected" since v3 (a PM chat URL carries the channel UUID). The command rewrites it to
`<origin>/workspace/tasks/project/<project id>/task/<task id>/comment/<comment id>`.

Pinned here: both the link's target and its visible text change and nothing else does; a link that
is already right (UUID form, task-page form, other chats) is not touched; the default run only
reports; `--apply` rewrites, moves `ts_updated_at`, and a second run finds nothing; rows whose notes
are not a document are skipped; the report never prints a to-do's title or notes.
"""

import copy
import uuid
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from origin.management.commands.rewrite_dead_todo_comment_links import rewrite
from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.tests.test_base import BaseAPITestCase

ORIGIN = "https://app.genos.example"
DEAD = f"{ORIGIN}/workspace/chat/pm/455/thread/5660/comment/24"
LIVE = f"{ORIGIN}/workspace/tasks/project/455/task/5660/comment/24"


def notes(href, text=None, extra=None):
    """The document `appendTodoFromMessage` writes."""
    blocks = [
        {
            "type": "paragraph",
            "props": {
                "textColor": "default",
                "textAlignment": "left",
                "backgroundColor": "default",
            },
            "content": [
                {"text": "From task comment: ", "type": "text", "styles": {"italic": True}},
                {
                    "href": href,
                    "type": "link",
                    "content": [
                        {"text": text if text is not None else href, "type": "text", "styles": {}}
                    ],
                },
            ],
            "children": [],
        }
    ]
    return blocks + (extra or [])


class RewriteTests(BaseAPITestCase):
    def _item(self, notes_value, title="Secret errand"):
        group, _ = ToDoGroup.objects.get_or_create(
            team=self.team, user=self.user, local_date=timezone.now().date()
        )
        return ToDoItem.objects.create(group=group, title=title, notes=notes_value)

    def _run(self, *args):
        out = StringIO()
        call_command("rewrite_dead_todo_comment_links", *args, stdout=out)
        return out.getvalue()

    # ---- the pure rewrite ---------------------------------------------------

    def test_the_target_and_the_visible_text_change_and_nothing_else_does(self):
        before = notes(DEAD)
        snapshot = copy.deepcopy(before)
        after, count = rewrite(before)
        self.assertEqual(count, 2)  # href + visible text
        self.assertEqual(after, notes(LIVE))
        self.assertEqual(before, snapshot, "the input is not mutated")

    def test_the_origin_is_kept_whatever_it_is(self):
        after, _ = rewrite(notes("http://localhost:3000/workspace/chat/pm/7/thread/8/comment/9"))
        self.assertEqual(
            after, notes("http://localhost:3000/workspace/tasks/project/7/task/8/comment/9")
        )

    def test_custom_link_text_is_left_alone(self):
        after, count = rewrite(notes(DEAD, text="the comment"))
        self.assertEqual(count, 1)
        self.assertEqual(after, notes(LIVE, text="the comment"))

    def test_a_link_that_is_already_right_is_not_touched(self):
        uuid_link = f"{ORIGIN}/workspace/chat/pm/{uuid.uuid4()}/thread/5660/comment/24"
        for href in (
            uuid_link,
            LIVE,
            f"{ORIGIN}/workspace/chat/gm/12/thread/5/message/9",
            f"{ORIGIN}/workspace/chat/pm/455/thread/5660",  # a task thread link, not a comment
            f"{ORIGIN}/workspace/chat/pm/455/thread/5660/comment/abc",
        ):
            with self.subTest(href=href):
                doc = notes(href)
                after, count = rewrite(doc)
                self.assertEqual(count, 0)
                self.assertIs(after, doc)

    def test_two_links_in_one_document_are_both_rewritten(self):
        other = "/workspace/chat/pm/9/thread/10/comment/11"
        doc = notes(DEAD, extra=notes(f"{ORIGIN}{other}"))
        after, count = rewrite(doc)
        self.assertEqual(count, 4)
        self.assertEqual(
            after,
            notes(LIVE, extra=notes(f"{ORIGIN}/workspace/tasks/project/9/task/10/comment/11")),
        )

    # ---- the command --------------------------------------------------------

    def test_the_default_run_reports_and_changes_nothing(self):
        item = self._item(notes(DEAD))
        out = self._run()
        self.assertIn("To-dos with a dead task-comment link: 1 (2 link(s))", out)
        self.assertIn(str(item.item_id), out)
        self.assertIn("Report only", out)
        item.refresh_from_db()
        self.assertEqual(item.notes, notes(DEAD))

    def test_apply_rewrites_moves_ts_updated_at_and_a_second_run_finds_nothing(self):
        item = self._item(notes(DEAD))
        ToDoItem.objects.filter(pk=item.pk).update(ts_updated_at=timezone.now() - timedelta(days=9))
        before = ToDoItem.objects.get(pk=item.pk).ts_updated_at
        out = self._run("--apply")
        self.assertIn("Rewritten: 1", out)
        item.refresh_from_db()
        self.assertEqual(item.notes, notes(LIVE))
        self.assertGreater(item.ts_updated_at, before + timedelta(days=8))
        again = self._run("--apply")
        self.assertIn("with a dead task-comment link: 0", again)
        self.assertIn("Rewritten: 0", again)

    def test_a_to_do_with_a_correct_link_or_no_notes_is_left_alone(self):
        good = self._item(notes(f"{ORIGIN}/workspace/chat/pm/{uuid.uuid4()}/thread/5/comment/6"))
        plain = self._item(None)
        empty = self._item([])
        snapshots = {i.pk: copy.deepcopy(i.notes) for i in (good, plain, empty)}
        self._item(notes(DEAD))
        self._run("--apply")
        for item in (good, plain, empty):
            item.refresh_from_db()
            self.assertEqual(item.notes, snapshots[item.pk])

    def test_rows_that_are_not_a_document_are_skipped_not_crashed_on(self):
        text_notes = self._item(f"see {DEAD}")  # a JSON string, not a document
        elsewhere = self._item([{"type": "paragraph", "props": {"note": DEAD}, "content": []}])
        out = self._run("--apply")
        self.assertIn("with a dead task-comment link: 0", out)
        self.assertIn(
            "Skipped (the link is not in a link's href or text, or notes are not a document): 2",
            out,
        )
        text_notes.refresh_from_db()
        elsewhere.refresh_from_db()
        self.assertEqual(text_notes.notes, f"see {DEAD}")
        self.assertEqual(elsewhere.notes[0]["props"]["note"], DEAD)

    def test_it_covers_every_owner(self):
        mine = self._item(notes(DEAD))
        group, _ = ToDoGroup.objects.get_or_create(
            team=self.team, user=self.user2, local_date=timezone.now().date()
        )
        theirs = ToDoItem.objects.create(group=group, title="theirs", notes=notes(DEAD))
        self._run("--apply")
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.notes, notes(LIVE))
        self.assertEqual(theirs.notes, notes(LIVE))

    def test_the_report_never_prints_a_title_or_notes(self):
        self._item(
            notes(DEAD, text="my private text"), title="Secret errand for the surprise party"
        )
        for args in ((), ("--apply",)):
            out = self._run(*args)
            self.assertNotIn("Secret errand", out)
            self.assertNotIn("private text", out)
            self.assertNotIn("/workspace/", out)
