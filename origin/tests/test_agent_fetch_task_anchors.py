"""MIP-1288: `fetch_task` names the thread, the chat and the note of a task, behind AGENT_FETCH_TASK_ANCHORS.

Off (the default) the tool's result and the declaration the model reads are what `origin/main`
produces. On, the result gains `anchors`: `[{anchor_type, anchor_id}]`, `thread` (the root message
the task was made from), `chat` (its channel), `note` (`task:<id>`), each only when the caller can
open it, and the declaration gains one sentence.

PINS. The off-state goldens were recorded on PRISTINE `origin/main` (09f52388), where the flag does
not exist, over the 12-step sequence fixture with a fixed clock:

    git -C <genos-api> worktree add --detach <wt> origin/main
    cp origin/tests/test_agent_fetch_task_anchors.py <wt>/origin/tests/
    RECORD_GOLDEN=1 python manage.py test origin.tests.test_agent_fetch_task_anchors.FlagOffTests --noinput
    # prints `GOLDEN <key> = "<sha256>"`; paste below

`result/*` hash `fetch_task`'s result (ids and uuids replaced by their order of appearance, the
rest verbatim: titles, texts, timestamps, key order) as the PM, the developer and the client;
`declarations` hashes every tool declaration the model is sent; `prompt/*` the system prompt.
The FlagOffTests class imports nothing this change adds, so it runs on main as it is.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.test import override_settings

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.team_models import TeamMembers
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.prompts import agent_system_prompt
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden
from origin.tests.fixtures.sequence_scenario import SequenceScenario

FLAG = "AGENT_FETCH_TASK_ANCHORS"
JST = ZoneInfo("Asia/Tokyo")

GOLDEN: dict[str, str] = {
    "result/pm": "ba60f8481e34decbcac4abb246c8cc6fee9b7b54a34efe469f3f011154c5aec0",
    "result/dev": "ba60f8481e34decbcac4abb246c8cc6fee9b7b54a34efe469f3f011154c5aec0",
    "result/client": "ba60f8481e34decbcac4abb246c8cc6fee9b7b54a34efe469f3f011154c5aec0",
    "declarations": "fbf3ed7b658224371e3efa742d2679a66e40e1c5380db0a26faac2eaf72ed9c4",
    "prompt/legacy": "5aa0aeaa405f8b1b0759c04a4c99422b2778731d470ad9bfff8a2e30e936c6df",
    "prompt/registry": "f5a822b4a2ff2aba264e11ab94840f240bd94433449ebb8f2dff5acc1dc0477c",
}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def normalised(result) -> str:
    """The result as stable text: every integer under an `...id` key and every uuid becomes its
    order of appearance, so the same story hashes the same on any database."""
    seen: dict[str, str] = {}

    def walk(value, key=""):
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, key) for v in value]
        if isinstance(value, int) and not isinstance(value, bool) and key.endswith("id"):
            return "<int>"
        if isinstance(value, str):
            return _UUID.sub(lambda m: seen.setdefault(m.group(0), f"<uuid{len(seen)}>"), value)
        return value

    return json.dumps(walk(result), ensure_ascii=False, indent=1)


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def on(extra=None):
    return override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, FLAG: True, **(extra or {})})


def off():
    return override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, FLAG: False})


class Base(SequenceScenario):
    def setUp(self):
        super().setUp()
        self.base = datetime(2026, 11, 2, tzinfo=JST)  # fixed: the comments' `ts` are hashed

    def ctx(self, user):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))

    def fetch(self, user, seq=None, **kw):
        seq = seq or self.seq
        return REGISTRY["fetch_task"].run({"task_id": seq.task.task_id}, self.ctx(user))

    def anchors(self, user, **kw):
        with on():
            return self.fetch(user, **kw)["anchors"]


class FlagOffTests(Base):
    """What the model is sent with the flag off: main's, byte for byte (hashes from main)."""

    def pin(self, key, text):
        digest = digest_text(text)
        if os.environ.get("RECORD_GOLDEN"):
            print(f'GOLDEN {key} = "{digest}"')
            return
        self.assertEqual(digest, GOLDEN[key], f"what the model is sent changed: {key}")

    def test_the_tool_result_as_the_pm_the_dev_and_the_client(self):
        seq = self.build_sequence()
        for key, user in (("pm", seq.b), ("dev", seq.c), ("client", seq.a)):
            self.pin(f"result/{key}", normalised(self.fetch(user, seq)))

    def test_the_declarations_the_model_reads(self):
        text = json.dumps([asdict(d) for d in _build_tool_declarations()], sort_keys=True)
        self.pin("declarations", text)

    def test_the_system_prompt_in_both_variants(self):
        for key, registry in (("legacy", False), ("registry", True)):
            cfg = {**settings.SEARCH_ENGINE, "AGENT_CHEATSHEET_FROM_REGISTRY": registry}
            with override_settings(SEARCH_ENGINE=cfg):
                self.pin(f"prompt/{key}", agent_system_prompt())

    def test_no_anchors_key_at_all_with_the_flag_off(self):
        seq = self.build_sequence()
        self.assertNotIn("anchors", self.fetch(seq.b, seq))
        with off():
            self.assertNotIn("anchors", self.fetch(seq.b, seq))


# --------------------------------------------------------------------------- #
# With the flag on                                                            #
# --------------------------------------------------------------------------- #


class AnchorTests(Base):
    """`anchors`, as each person of the story reads them."""

    def setUp(self):
        super().setUp()
        self.seq = self.build_sequence()
        self.thread = {"anchor_type": "thread", "anchor_id": str(self.seq.thread_root.id)}
        self.chat = {"anchor_type": "chat", "anchor_id": str(self.seq.gm.id)}
        self.note = {"anchor_type": "note", "anchor_id": f"task:{self.seq.note['note_id']}"}

    def other_task(self, **fields):
        """Another task row, as a second claimant of the thread."""
        task = self.seq.task
        base = dict(
            team=self.team,
            project=self.seq.project,
            reporter=self.seq.b,
            title="Another",
            status="Open",
            thread_id=task.thread_id,
            chat_id=task.chat_id,
            project_task_number=900 + TaskMaster.objects.count(),
        )
        return TaskMaster.objects.create(**{**base, **fields})

    # -- what is listed ---------------------------------------------------- #

    def test_the_pm_who_is_in_the_chat_gets_thread_then_chat_then_note(self):
        self.assertEqual(self.anchors(self.seq.b), [self.thread, self.chat, self.note])

    def test_the_client_on_the_project_and_in_the_chat_gets_all_three(self):
        self.assertEqual(self.anchors(self.seq.a), [self.thread, self.chat, self.note])

    def test_the_developer_who_is_not_in_the_chat_gets_the_note_alone(self):
        self.assertEqual(self.anchors(self.seq.c), [self.note])

    def test_the_developer_in_the_chat_gets_all_three(self):
        ChannelMember.objects.create(channel=self.seq.gm, user=self.seq.c)
        self.assertEqual(self.anchors(self.seq.c), [self.thread, self.chat, self.note])

    def test_the_words_are_the_timelines(self):
        for anchor in self.anchors(self.seq.b):
            self.assertEqual(sorted(anchor), ["anchor_id", "anchor_type"])
            self.assertIn(anchor["anchor_type"], ("thread", "chat", "note"))
        self.assertRegex(self.note["anchor_id"], r"^task:\d+$")

    def test_only_the_anchors_key_is_added_and_it_sits_before_the_summary(self):
        off_result = self.fetch(self.seq.b)
        with on():
            on_result = self.fetch(self.seq.b)
        self.assertEqual({k: v for k, v in on_result.items() if k != "anchors"}, off_result)
        keys = list(on_result)
        self.assertEqual(keys[-2:], ["anchors", "__summary__"])
        self.assertEqual([k for k in keys if k != "anchors"], list(off_result))

    def test_a_task_with_no_thread_no_chat_and_no_note_has_an_empty_list(self):
        TaskNoteMaster.objects.filter(task_id=self.seq.task.task_id).delete()
        TaskMaster.objects.filter(pk=self.seq.task.pk).update(thread_id=None, chat_id=None)
        self.assertEqual(self.anchors(self.seq.b), [])

    def test_the_same_through_mcp_get_task(self):
        with on():
            out = self.mcp_tool(self.seq.b, "get_task", task_id=self.seq.task.task_id)
        self.assertEqual(out["anchors"], [self.thread, self.chat, self.note])
        out = self.mcp_tool(self.seq.b, "get_task", task_id=self.seq.task.task_id)
        self.assertNotIn("anchors", out)

    # -- access: no trace, no oracle -------------------------------------- #

    def test_a_viewer_who_cannot_read_the_chat_gets_exactly_what_a_task_with_no_chat_gives(self):
        """The oracle test: the whole result, not just the list, equals the result for the same
        task with no thread and no chat on it."""
        with on():
            seen = self.fetch(self.seq.c)
        TaskMaster.objects.filter(pk=self.seq.task.pk).update(thread_id=None, chat_id=None)
        with on():
            bare = self.fetch(self.seq.c)
        self.assertEqual(seen, bare)
        self.assertNotIn(str(self.seq.gm.id), json.dumps(seen, default=str))
        self.assertNotIn(str(self.seq.thread_root.id), json.dumps(seen, default=str))

    def test_a_reporter_off_the_project_sees_no_note_and_no_chat_and_no_trace(self):
        user = self.user2
        TaskMaster.objects.filter(pk=self.seq.task.pk).update(reporter=user)
        with on():
            seen = self.fetch(user)  # may open the task: the reporter
        self.assertEqual(seen["anchors"], [])
        TaskNoteMaster.objects.filter(task_id=self.seq.task.task_id).delete()
        TaskMaster.objects.filter(pk=self.seq.task.pk).update(thread_id=None, chat_id=None)
        with on():
            bare = self.fetch(user)
        self.assertEqual(seen, bare)

    def test_that_reporter_in_the_chat_gets_the_chat_but_still_not_the_note(self):
        user = self.user2
        TaskMaster.objects.filter(pk=self.seq.task.pk).update(reporter=user)
        ChannelMember.objects.create(channel=self.seq.gm, user=user)
        self.assertEqual(self.anchors(user), [self.thread, self.chat])

    def test_a_left_chat_member_loses_the_anchors(self):
        ChannelMember.objects.filter(channel=self.seq.gm, user=self.seq.a).update(is_deleted=True)
        self.assertEqual(self.anchors(self.seq.a), [self.note])

    def test_a_deleted_chat_gives_neither_the_chat_nor_its_thread(self):
        Channel.objects.filter(pk=self.seq.gm.pk).update(is_deleted=True)
        self.assertEqual(self.anchors(self.seq.b), [self.note])

    def test_a_chat_shared_into_this_team_is_readable_only_through_the_team_rule(self):
        """A chat of another team the caller is a member of is not in this team's list unless it
        is shared into it: `can_read_chat` decides, not the membership row."""
        other = type(self.team).objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        Channel.objects.filter(pk=self.seq.gm.pk).update(team=other)
        self.assertEqual(self.anchors(self.seq.b), [self.note])

    def test_another_teams_note_is_never_listed(self):
        other = type(self.team).objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        TaskNoteMaster.objects.filter(task_id=self.seq.task.task_id).update(team=other)
        self.assertEqual(self.anchors(self.seq.b), [self.thread, self.chat])

    def test_a_private_grant_on_the_note_is_followed_by_the_note_tools_too(self):
        """Whatever is listed, the next call opens: over every viewer, an anchor is listed iff
        the tool that opens it does not refuse."""
        task = self.seq.task
        TaskMaster.objects.filter(pk=task.pk).update(reporter=self.user2)
        for user in (self.seq.a, self.seq.b, self.seq.c, self.user2):
            for member_of_chat in (False, True):
                ChannelMember.objects.filter(channel=self.seq.gm, user=user).delete()
                if member_of_chat:
                    ChannelMember.objects.create(channel=self.seq.gm, user=user)
                ctx = self.ctx(user)
                with on():
                    anchors = self.fetch(user)["anchors"]
                listed = {a["anchor_type"]: a["anchor_id"] for a in anchors}
                try:
                    REGISTRY["fetch_note"].run(
                        {"note_type": "task", "note_id": self.seq.note["note_id"]}, ctx
                    )
                    note_opens = True
                except ToolError:
                    note_opens = False
                self.assertEqual("note" in listed, note_opens, (user.username, member_of_chat))
                try:
                    REGISTRY["fetch_chat_thread"].run(
                        {"chat_id": str(self.seq.gm.id), "thread_id": str(self.seq.thread_root.id)},
                        ctx,
                    )
                    chat_opens = True
                except ToolError:
                    chat_opens = False
                self.assertEqual("chat" in listed, chat_opens, (user.username, member_of_chat))
                self.assertEqual("thread" in listed, chat_opens, (user.username, member_of_chat))

    # -- the thread claim rule -------------------------------------------- #

    def test_a_thread_two_live_tasks_claim_is_attached_to_neither_but_the_chat_stays(self):
        second = self.other_task()
        self.assertEqual(self.anchors(self.seq.b), [self.chat, self.note])
        with on():
            seen = REGISTRY["fetch_task"].run({"task_id": second.task_id}, self.ctx(self.seq.b))
        self.assertEqual(seen["anchors"], [self.chat])

    def test_a_deleted_second_claimant_does_not_count(self):
        self.other_task(is_deleted=True)
        self.assertIn(self.thread, self.anchors(self.seq.b))

    def test_an_init_task_claiming_the_thread_does_not_count(self):
        self.other_task(is_init_task=True)
        self.assertIn(self.thread, self.anchors(self.seq.b))

    def test_another_teams_task_with_the_same_thread_id_does_not_count(self):
        other = type(self.team).objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        self.other_task(team=other, project=None)
        self.assertIn(self.thread, self.anchors(self.seq.b))

    def test_the_claim_check_is_item_lines_claims_over_every_kind_of_second_row(self):
        from origin.search_engine.agent.tools.task_anchors import _sole_claimant  # noqa: PLC0415
        from origin.services.item_lines import _claims  # noqa: PLC0415

        thread = str(self.seq.thread_root.id)
        team = str(self.team.team_id)
        task_id = self.seq.task.task_id

        def agree():
            expected = _claims(team, [thread]).get(thread) == [task_id]
            self.assertEqual(_sole_claimant(team, thread, task_id), expected)
            return expected

        self.assertTrue(agree())
        second = self.other_task(is_deleted=True)
        self.assertTrue(agree())
        TaskMaster.objects.filter(pk=second.pk).update(is_deleted=False, is_init_task=True)
        self.assertTrue(agree())
        TaskMaster.objects.filter(pk=second.pk).update(is_init_task=False)
        self.assertFalse(agree())
        other = type(self.team).objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        TaskMaster.objects.filter(pk=second.pk).update(team=other)
        self.assertTrue(agree())

    # -- what is a thread -------------------------------------------------- #

    def test_a_project_chat_thread_is_not_a_thread_anchor_but_its_chat_is(self):
        pm, _ = Channel.objects.get_or_create(
            team=self.team,
            kind=ChannelKind.PM,
            project=self.seq.project,
            defaults={"title": "Project"},
        )
        root = Message.objects.create(
            channel=pm, sender=self.seq.b, seq=900, body={}, body_text="hello"
        )
        TaskMaster.objects.filter(pk=self.seq.task.pk).update(
            chat_id=str(pm.id), thread_id=str(root.id)
        )
        self.assertEqual(
            self.anchors(self.seq.b), [{"anchor_type": "chat", "anchor_id": str(pm.id)}, self.note]
        )

    def test_a_reply_id_is_not_a_thread(self):
        reply = Message.objects.filter(thread_root_id=self.seq.thread_root.id).first()
        self.assertIsNotNone(reply)
        TaskMaster.objects.filter(pk=self.seq.task.pk).update(thread_id=str(reply.id))
        self.assertEqual(self.anchors(self.seq.b), [self.chat, self.note])

    def test_a_deleted_root_message_is_not_a_thread(self):
        from django.utils import timezone  # noqa: PLC0415

        Message.objects.filter(pk=self.seq.thread_root.pk).update(deleted_at=timezone.now())
        self.assertEqual(self.anchors(self.seq.b), [self.chat, self.note])

    def test_the_channel_is_the_roots_not_the_tasks_chat_id_column(self):
        """The column is informational: a thread whose root is in a chat the caller cannot read
        is not listed because the TASK says it is in a chat they can."""
        secret = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Secret")
        ChannelMember.objects.create(channel=secret, user=self.seq.c)
        root = Message.objects.create(
            channel=secret, sender=self.seq.c, seq=1, body={}, body_text="private"
        )
        TaskMaster.objects.filter(pk=self.seq.task.pk).update(thread_id=str(root.id))
        # B reads the task's own chat_id chat, not the secret one the thread root is in.
        self.assertEqual(self.anchors(self.seq.b), [self.chat, self.note])
        self.assertNotIn(str(root.id), json.dumps(self.anchors(self.seq.b)))

    def test_ids_that_are_not_uuids_give_nothing_and_do_not_raise(self):
        TaskMaster.objects.filter(pk=self.seq.task.pk).update(thread_id="12", chat_id="7")
        self.assertEqual(self.anchors(self.seq.b), [self.note])
        TaskMaster.objects.filter(pk=self.seq.task.pk).update(thread_id="", chat_id="")
        self.assertEqual(self.anchors(self.seq.b), [self.note])

    def test_a_task_note_that_is_not_this_tasks_is_not_listed(self):
        other = self.other_task(thread_id=None, chat_id=None)
        TaskNoteMaster.objects.filter(task_id=self.seq.task.task_id).update(task=other)
        self.assertEqual(self.anchors(self.seq.b), [self.thread, self.chat])

    def test_two_notes_are_listed_by_id(self):
        extra = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.seq.project,
            owner=self.seq.c,
            task=self.seq.task,
            title="Second",
        )
        notes = [a["anchor_id"] for a in self.anchors(self.seq.b) if a["anchor_type"] == "note"]
        self.assertEqual(notes, [self.note["anchor_id"], f"task:{extra.note_id}"])

    def test_the_tenant_guard_still_comes_first(self):
        outsider = type(self.seq.b).objects.create_user(
            username="out", email="out@example.com", password="x"
        )
        other = type(self.team).objects.create(
            team_name="Other", team_email="o@example.com", owner=outsider
        )
        TeamMembers.objects.create(team=other, attendee=outsider)
        ctx = ToolContext(team_id=str(other.team_id), user_id=str(outsider.id))
        with on():
            with self.assertRaises(ToolForbidden):
                REGISTRY["fetch_task"].run({"task_id": self.seq.task.task_id}, ctx)

    def test_the_viewer_without_the_task_is_refused_as_before_and_gets_no_anchor(self):
        seq = self.seq
        ProjectMembers.objects.filter(attendee=seq.a).delete()  # the V2 client: in the chat only
        with on(), self.assertRaises(ToolForbidden):
            REGISTRY["fetch_task"].run({"task_id": seq.task.task_id}, self.ctx(seq.a))

    # -- the declaration --------------------------------------------------- #

    def test_with_the_flag_on_one_sentence_is_added_to_fetch_task_and_nothing_else_moves(self):
        from origin.search_engine.agent.tools.task_anchors import DESCRIPTION_ADDITION  # noqa: PLC0415

        before = {d.name: d for d in _build_tool_declarations()}
        with on():
            after = {d.name: d for d in _build_tool_declarations()}
        self.assertEqual(list(before), list(after))
        for name in before:
            if name == "fetch_task":
                continue
            self.assertEqual(before[name], after[name], name)
        self.assertEqual(
            after["fetch_task"].description, before["fetch_task"].description + DESCRIPTION_ADDITION
        )
        self.assertEqual(
            before["fetch_task"].parameters_schema, after["fetch_task"].parameters_schema
        )
        for word in ("anchors", "thread", "chat", "note", "task:<note_id>", "fetch_note"):
            self.assertIn(word, DESCRIPTION_ADDITION)

    def test_the_flag_defaults_off(self):
        from origin.search_engine.agent.tools import task_anchors  # noqa: PLC0415

        self.assertIs(settings.SEARCH_ENGINE[FLAG], False)
        self.assertFalse(task_anchors.enabled())
