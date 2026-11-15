"""`set_item_importance` offers the "On it" mark of a report row, behind `AGENT_PARITY_TOOLS` (MIP-1588).

A member taps "On it" on a task row of their own report and the task leaves their later reports for 7 days, or until its status,
priority, due date or overdue state changes. Private, and nothing on the task changes. The connected-app tool offered only the three
older values, so "I'm on it, stop reminding me" went to `not_important`, a stronger mark (never a top item or a suggestion until
cleared). Now, while the flag is on:

  * `on_it` is a fourth value of the tool (the schema is a COPY with the value laid over it, `tool_flag_params`) and the catalog text
    says what it is for (`tool_flag_text.MCP_REPLACEMENTS`);
  * the result names the mark it replaced, so an overwrite is visible;
  * the row is the very row the app's own "On it" action writes, held out of reports by the same rule.

With the flag off the tool is what it was, byte for byte: three values, `on_it` refused with the same words, no `previous`.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.core import mail
from django.utils import timezone

from origin.models.common.member_importance_models import (
    ALL_MARKS,
    MARK_ON_IT,
    MARKS,
    MemberImportanceMark,
)
from origin.search_engine.agent import tool_flag_params
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.parity import PARITY_FLAG
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY
from origin.services import importance, report_memory
from origin.tests.test_importance import Base
from origin.views.public.mcp.secretary.set_item_importance import SET_ITEM_IMPORTANCE

THREE = ["not_important", "priority_is_right", "clear"]

# The tool as a connected client read it before MIP-1588, held as literals: with the flag off nothing here may move by a byte.
BASE_MARK_DESCRIPTION = (
    "not_important: never offer it as a top item or suggestion. "
    "priority_is_right: stop asking whether to change its priority. "
    "clear: remove the mark."
)
BASE_DESCRIPTION = (
    "Correct what matters to the member on ONE task. `mark` 'not_important': never "
    "offer it as a top item or a suggestion again (it sticks until they clear it). "
    "'priority_is_right': stop asking whether to change its priority, its ranking is "
    "unchanged. 'clear': remove the mark. Use it when they say 'this isn't important', "
    "'stop showing me this' or 'leave its priority alone'. It changes nothing on the "
    "task itself. Name the task by its display id (PRJ-123), its Genos URL, or its "
    "numeric id. It refuses while the member has recording switched off. See the "
    "marks with `get_my_importance`. Requires a write-scoped API key."
)


def flag(on):
    return mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: on})


class MarkBase(Base):
    """A connected app with a write key (`Base`) and a member who can open `self.task`; each test says which way the flag is."""

    def setUp(self):
        super().setUp()
        self.task = self.make_task("Answer me")
        self.other_task = self.make_task("Another")

    def listed_tool(self, flag_on):
        with flag(flag_on):
            tools = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        return tools["set_item_importance"]

    def set(self, mark, *, task=None, flag_on=True):
        """The tool's own answer, parsed."""
        with flag(flag_on):
            return self.ok("set_item_importance", task_id=(task or self.task).task_id, mark=mark)

    def said(self, mark, *, task=None, flag_on=True):
        """The words the tool leads its answer with (the first line of the MCP text), after making the change."""
        with flag(flag_on):
            text, is_error = self.call(
                "set_item_importance", task_id=(task or self.task).task_id, mark=mark
            )
        self.assertFalse(is_error, text)
        return text.split("\n", 1)[0]

    def refuses(self, mark, *, task_id=None, flag_on=True):
        with flag(flag_on):
            return self.refused(
                "set_item_importance", task_id=task_id or self.task.task_id, mark=mark
            )

    def row(self, task=None):
        return MemberImportanceMark.objects.filter(
            user=self.user, task_id=(task or self.task).task_id
        ).first()

    def signature(self, task):
        task.refresh_from_db()
        return report_memory.signature_of_task(task, timezone.localdate(timezone.now()))


class WithTheFlagOffTheToolIsWhatItWasTests(MarkBase):
    def test_the_published_schema_and_text_are_the_old_bytes(self):
        tool = self.listed_tool(False)
        mark = tool["inputSchema"]["properties"]["mark"]
        self.assertEqual(mark["enum"], THREE)
        self.assertEqual(mark["description"], BASE_MARK_DESCRIPTION)
        self.assertEqual(tool["description"], BASE_DESCRIPTION)
        self.assertEqual(set(tool["inputSchema"]["required"]), {"task_id", "mark"})
        self.assertEqual(set(tool["inputSchema"]["properties"]), {"task_id", "mark"})

    def test_on_it_is_refused_with_the_words_it_always_had_and_stores_nothing(self):
        text = self.refuses("on_it", flag_on=False)
        self.assertIn("`mark` must be one of: not_important, priority_is_right, clear.", text)
        self.assertNotIn("on_it", text)
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_a_mark_answers_without_the_mark_it_replaced_and_in_the_old_words(self):
        self.set("priority_is_right", flag_on=False)
        out = self.set("not_important", flag_on=False)
        self.assertNotIn("previous", out)
        self.assertEqual(out["mark"], "not_important")
        self.assertEqual(
            self.said("priority_is_right", flag_on=False),
            f"{self.task.display_id} marked as having the right priority.",
        )
        self.assertEqual(self.said("clear", flag_on=False), f"{self.task.display_id} cleared.")

    def test_the_old_marks_store_the_source_mcp_as_before(self):
        self.set("not_important", flag_on=False)
        self.assertEqual(self.row().source, "mcp")

    def test_the_declared_in_app_tools_do_not_include_it_either_way(self):
        # MCP-only: the in-app agent never offers it, so no grouped paid run measures it.
        for on in (False, True):
            with flag(on):
                names = {d.name for d in _build_tool_declarations()}
            self.assertNotIn("set_item_importance", names)
        self.assertTrue(SET_ITEM_IMPORTANCE.mcp_only)
        self.assertNotIn("set_item_importance", REGISTRY)


class OnItWhileTheFlagIsOnTests(MarkBase):
    def test_the_schema_is_a_copy_with_one_more_value_and_the_registry_is_not_touched(self):
        before = copy.deepcopy(MCP_ONLY_REGISTRY["set_item_importance"].parameters_schema)
        tool = self.listed_tool(True)
        mark = tool["inputSchema"]["properties"]["mark"]
        self.assertEqual(mark["enum"], [*THREE, "on_it"])
        self.assertEqual(mark["description"], tool_flag_params.ON_IT_MARK_DESCRIPTION)
        self.assertIn("on_it: the member is on it", mark["description"])
        self.assertEqual(MCP_ONLY_REGISTRY["set_item_importance"].parameters_schema, before)
        self.assertEqual(before["properties"]["mark"]["enum"], THREE)
        self.assertEqual(set(tool["inputSchema"]["required"]), {"task_id", "mark"})
        self.assertEqual(set(tool["inputSchema"]["properties"]), {"task_id", "mark"})

    def test_the_catalog_text_says_what_it_is_for_and_keeps_every_other_sentence(self):
        on = self.listed_tool(True)["description"]
        off = self.listed_tool(False)["description"]
        self.assertIn("'on_it': they are on it", on)
        self.assertIn("'I'm on it' or 'stop reminding me about this one'", on)
        self.assertIn("weaker than 'not_important'", on)
        self.assertEqual(
            on,
            off.replace(
                tool_flag_params.ON_IT_CLEAR_SENTENCE, tool_flag_params.ON_IT_CLEAR_SENTENCE_ON, 1
            ),
        )

    def test_the_mark_is_the_one_the_app_sets_with_a_signature_and_the_source_mcp(self):
        out = self.set("on_it")
        self.assertEqual(
            (out["task_id"], out["mark"], out["previous"]), (self.task.task_id, "on_it", None)
        )
        row = self.row()
        self.assertEqual((row.mark, row.source), ("on_it", "mcp"))
        self.assertEqual(row.sig, self.signature(self.task))

    def test_every_transition_names_what_it_replaced_and_leaves_one_row(self):
        steps = [
            ("on_it", None, "on_it"),
            ("not_important", "on_it", "not_important"),
            ("on_it", "not_important", "on_it"),
            ("priority_is_right", "on_it", "priority_is_right"),
            ("on_it", "priority_is_right", "on_it"),
            ("clear", "on_it", None),
            ("clear", None, None),
        ]
        for mark, previous, now in steps:
            with self.subTest(mark=mark, previous=previous):
                out = self.set(mark)
                self.assertEqual((out["previous"], out["mark"]), (previous, now))
                held = list(MemberImportanceMark.objects.filter(user=self.user))
                self.assertEqual([r.mark for r in held], [now] if now else [])

    def test_the_summary_says_what_was_replaced(self):
        name = self.task.display_id
        self.assertEqual(
            self.said("on_it"),
            f'{name} marked "on it". It is left out of reports for 7 days, or until it changes.',
        )
        self.assertEqual(
            self.said("on_it"),
            f'{name} marked "on it" (it already was). It is left out of reports for 7 days, or until it changes.',
        )
        self.assertEqual(
            self.said("not_important"), f'{name} marked not important (it was marked "on it").'
        )
        self.assertEqual(
            self.said("on_it"),
            f'{name} marked "on it" (it was marked not important). It is left out of reports for 7 days, or until it changes.',
        )
        self.assertEqual(
            self.said("priority_is_right"),
            f'{name} marked as having the right priority (it was marked "on it").',
        )
        self.assertEqual(
            self.said("clear"), f"{name} cleared (it was marked as having the right priority)."
        )
        self.assertEqual(self.said("clear"), f"{name} cleared (there was no mark).")

    def test_a_task_the_member_cannot_open_reads_like_one_that_does_not_exist(self):
        hidden = self.make_task(
            "Hidden", project=self.hidden_project(), assignee=self.user2, reporter=self.user2
        )
        missing = self.refuses("on_it", task_id=999999)
        theirs = self.refuses("on_it", task_id=hidden.task_id)
        self.assertEqual(missing.replace("999999", "N"), theirs.replace(str(hidden.task_id), "N"))
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_recording_off_refuses_it_and_keeps_nothing(self):
        self.switch(False)
        self.assertIn("not recording", self.refuses("on_it"))
        self.assertEqual(MemberImportanceMark.objects.count(), 0)

    def test_the_marks_cap_still_holds_for_it(self):
        with mock.patch.object(importance, "MAX_MARKS", 0):
            self.assertIn("marked 0 tasks already", self.refuses("on_it"))

    def test_an_unknown_mark_names_the_four_values_now(self):
        self.assertIn(
            "`mark` must be one of: not_important, priority_is_right, on_it, clear.",
            self.refuses("very"),
        )

    def test_get_my_importance_lists_what_it_set_with_the_same_word(self):
        self.set("on_it")
        with flag(True):
            listing = self.ok("get_my_importance")
        self.assertEqual(
            [(m["task_id"], m["mark"]) for m in listing["marked"]], [(self.task.task_id, "on_it")]
        )

    def test_the_constants_agree_with_the_model(self):
        self.assertEqual(tool_flag_params.ON_IT_MARK, MARK_ON_IT)
        self.assertEqual(ALL_MARKS, (*MARKS, MARK_ON_IT))


def table_counts():
    """Rows in every table of the project, so a write anywhere shows."""
    return {
        model._meta.label: model.objects.count()
        for model in apps.get_models()
        if model._meta.app_label in {"origin", "search_engine"}
        and not model._meta.proxy
        and model._meta.label
        != "origin.ApiKey"  # the test's own re-authentication, not a channel of the call
    }


def changed(before, after):
    return {name: after[name] - before[name] for name in after if after[name] != before[name]}


class TheTwinOfTheAppsOnItActionTests(MarkBase):
    """The app's own action is `PUT /api/v2/user/memory/marks/<task>/` with `source: report`. The tool writes the same row."""

    def rest(self, method, task, **body):
        self.client.credentials()
        self.client.force_authenticate(user=self.user)
        try:
            return getattr(self.client, method)(
                f"/api/v2/user/memory/marks/{task.task_id}/?team_id={self.team.team_id}",
                body or None,
                format="json",
            )
        finally:
            self.client.force_authenticate(user=None)
            self._key(scope="write")

    def snapshot(self, task):
        row = MemberImportanceMark.objects.get(user=self.user, task_id=task.task_id)
        return {
            "mark": row.mark,
            "sig": row.sig,
            "team": row.team_id,
            "source": row.source,
            "ts": row.ts,
        }

    def both_rows(self):
        """`(the app's row, the tool's row)` for the SAME task, one after the other."""
        with flag(True):
            res = self.rest("put", self.task, mark="on_it", source="report")
            self.assertEqual(res.status_code, 200, res.data)
            from_the_app = self.snapshot(self.task)
            app_prefs = importance.load(self.ctx)
            self.assertEqual(self.rest("delete", self.task).status_code, 200)
            self.set("on_it")
            from_the_tool = self.snapshot(self.task)
            tool_prefs = importance.load(self.ctx)
        return from_the_app, from_the_tool, app_prefs, tool_prefs

    def test_the_same_row_in_every_field_but_where_the_answer_came_from(self):
        from_the_app, from_the_tool, _, _ = self.both_rows()
        self.assertEqual((from_the_app["source"], from_the_tool["source"]), ("report", "mcp"))
        for field in ("mark", "sig", "team"):
            self.assertEqual(from_the_app[field], from_the_tool[field], field)
        self.assertEqual(from_the_tool["mark"], "on_it")
        self.assertIsNotNone(from_the_tool["sig"])
        self.assertLess(from_the_tool["ts"] - from_the_app["ts"], timedelta(minutes=1))

    def test_both_rows_leave_the_task_out_of_a_report_by_the_same_rule(self):
        _, _, app_prefs, tool_prefs = self.both_rows()
        sig = self.signature(self.task)
        for name, prefs in (("the app", app_prefs), ("the tool", tool_prefs)):
            info = prefs.info[self.task.task_id]
            self.assertEqual(prefs.marks[self.task.task_id], "on_it", name)
            for after, holds in (
                (timedelta(0), True),
                (timedelta(days=6, hours=23), True),
                (timedelta(days=7, minutes=1), False),
            ):
                with self.subTest(name=name, after=str(after)):
                    self.assertEqual(
                        report_memory._answer_holds(info, "on_it", sig, info[1] + after), holds
                    )
        self.assertEqual(report_memory.ON_IT_DAYS, 7)
        # ...and until the task changes: a new status is a new signature, for both.
        type(self.task).objects.filter(pk=self.task.pk).update(status="Blocked")
        changed_sig = self.signature(self.task)
        self.assertNotEqual(changed_sig, sig)
        for prefs in (app_prefs, tool_prefs):
            info = prefs.info[self.task.task_id]
            self.assertFalse(report_memory._answer_holds(info, "on_it", changed_sig, info[1]))

    def test_the_apps_door_writes_the_mark_and_nothing_else_and_nothing_is_told(self):
        outbound: list = []
        with (
            flag(True),
            mock.patch(
                "requests.sessions.Session.request",
                side_effect=lambda *a, **k: outbound.append((a, k)),
            ),
        ):
            before = table_counts()
            res = self.rest("put", self.task, mark="on_it", source="report")
            after = table_counts()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(changed(before, after), {"origin.MemberImportanceMark": 1})
        self.assertEqual(outbound, [])
        self.assertEqual(mail.outbox, [])

    def test_the_tools_door_writes_the_mark_and_only_what_every_connected_app_call_keeps(self):
        """What a connected-app call records is the MCP layer's own and the same for the old marks and `on_it`. Measured on
        two like calls after a first one (a key's first call may log more), and pinned, so a new channel (a notification, a
        push, an activity row, a to-do, a comment, a message) is noticed."""
        from origin.models.common.security_event_models import SecurityEvent  # noqa: PLC0415

        outbound: list = []
        with mock.patch(
            "requests.sessions.Session.request",
            side_effect=lambda *a, **k: outbound.append((a, k)),
        ):
            self.set("not_important", task=self.task, flag_on=False)  # the key's first call
            third = self.make_task("Third")
            deltas = {}
            for name, on, mark, task in (
                ("old", False, "not_important", self.other_task),
                ("new", True, "on_it", third),
            ):
                before = table_counts()
                self.set(mark, task=task, flag_on=on)
                deltas[name] = changed(before, table_counts())
        for name, delta in deltas.items():
            self.assertEqual(delta.pop("origin.MemberImportanceMark", 0), 1, name)
        self.assertEqual(
            deltas["old"],
            deltas["new"],
            list(SecurityEvent.objects.values_list("action", flat=True)),
        )
        self.assertEqual(outbound, [])
        self.assertEqual(mail.outbox, [])
        for name in deltas["new"]:
            self.assertNotRegex(
                name, r"Notification|Activity|ToDo|Message|Comment|InboxItems|Push", name
            )

    def test_the_tool_leaves_the_task_itself_alone(self):
        self.task.refresh_from_db()
        fields = ("status", "priority", "due_date", "title", "ts_updated_at")
        before = {f: getattr(self.task, f) for f in fields}
        self.set("on_it")
        self.task.refresh_from_db()
        self.assertEqual(before, {f: getattr(self.task, f) for f in fields})
