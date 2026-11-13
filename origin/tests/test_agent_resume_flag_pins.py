"""Flag pins for what a RESUMED leg sends the model (MIP-977, and MIP-991 once it lands).

`AGENT_RESUME_KEEPS_BLOCKS` (977) and `AGENT_RESUME_KEEPS_TURNS` (991) change what the
leg of a run that follows an approve or reject sends the model. Their contract is
"flag off = what `origin/main` sends, byte for byte". This module pins that contract
with sha256 goldens that were recorded on PRISTINE `origin/main` (346816f2, a clean
worktree with none of the change), and pins the flag-on shapes against them.

THE RECORDING (how `GOLDEN` below was made; run it again after anything that moves the
prompt on main, and compare, rather than editing a hash to make a test pass):

    git -C <genos-api> worktree add --detach <wt> origin/main
    cp origin/tests/test_agent_resume_flag_pins.py \\
       origin/tests/test_agent_resume_system_extra.py <wt>/origin/tests/
    # (the pin module imports only the harness of the 977 module, which touches no
    #  `resume_context`, so it runs on code that has never heard of the flags)
    RECORD_GOLDEN=1 python manage.py test origin.tests.test_agent_resume_flag_pins --noinput
    # prints  GOLDEN <shape>/<decision> = <sha256>  for every shape; paste them below

What is hashed, per shape and decision: EVERY model call of the whole run (the ask leg
and the resumed leg), each as its system instruction (the date line is switched off, ids
are replaced by placeholders) plus its full message list (role, text, call, result).
That is what the provider receives, so a flag that leaks into the off state, in the
system prompt, the messages, or a call that should not exist, changes a hash.

A hash pin on the flag-off state alone is blind to a gate that is half removed (two
places read the flag: the write of `resume_context` at the pause and the read at
`/decide/`). So there are two off-state tests that each defeat one of the gates
(`FlagOffTests.test_a_row_kept_while_the_flag_was_on...` and `...writes_nothing...`).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import re
from unittest import mock

from django.conf import settings
from django.test import override_settings

from origin.search_engine import agent_views
from origin.search_engine.agent.mentions import ResolvedMention
from origin.search_engine.models import AgentRun
from origin.tests.test_agent_resume_system_extra import (
    NOTE_SUMMARY,
    THREAD_SUMMARY,
    ResumeBase,
)

OFF: dict = {}
ON_977 = {"AGENT_RESUME_KEEPS_BLOCKS": True}

# sha256 of every model call of one ask + decision, per shape/decision, recorded on
# pristine origin/main 346816f2 (see the module docstring for the recipe).
GOLDEN: dict[str, str] = {
    "mention/approve": "0ad49cdb0a5ba38bfeffd8d9eac215340682343f1b283affb17ed15198b55ad5",
    "mention/reject": "a8a4728520f467f74208d7cf5262967e473076d8412d3db8faf3bde7a36df904",
    "note/approve": "75b6185e2854bed2930ee6d42bf44db386be7f661bec9079c2e83e55e0736e36",
    "note/reject": "183247f947d8f8d228968a04e99a96e664739a4e335b329ab24cbc935fa6d058",
    "plain/approve": "d207f4cbcea6328a577943e6772b7996fecf82b89a189482691154d1fcbd4661",
    "plain/reject": "9b7aa41657fb037374a3c850064089fbee6b88f94da161e3ccb59fb839852df4",
    "thread+mention/approve": "22e272d2f7762cc115cd032d518b580e6d4fabbddf86bae8e626f17891f777fa",
    "thread+mention/reject": "9c6038628c26c0ca065bb685f92b2e0ecc4d700483586ec38b915d4bc053a904",
    "thread/approve": "5eae87c295b21687d4854741b9a65bf0a04467032da9067d5c8f713a618db777",
    "thread/reject": "b67588e526cb2ae922eeb806e9bc8e0458b97ba6c8ee3b32666f45329ea11900",
}

MENTION = ResolvedMention(
    kind="task", label="Fix login flow", task_id=123, display_id="WRD-5", project_id="77"
)
MENTION_ARGS = [{"type": "task", "task_id": 123, "label": "x"}]

SHAPES = ("plain", "thread", "note", "mention", "thread+mention")
DECISIONS = ("approve", "reject")


class PinBase(ResumeBase):
    """`ResumeBase` with the flags chosen per class, the date line off, and a record
    of every model call (system instruction and messages) of the test."""

    flags: dict = {"AGENT_INJECT_CURRENT_DATE": False}

    def setUp(self):
        super().setUp()
        self.calls: list[tuple[str, list]] = []
        real = self.model.generate_step

        def spy(messages, tools, system_instruction, **kwargs):
            self.calls.append((system_instruction, copy.deepcopy(messages)))
            return real(messages, tools, system_instruction, **kwargs)

        self.model.generate_step = spy

    # -- one ask and its decision, the way a person does it ---------------------

    def ask_kwargs(self, shape: str) -> dict:
        kwargs: dict = {}
        if "thread" in shape:
            kwargs["thread_context"] = self.make_thread()
        if shape == "note":
            kwargs["note_context"] = self.make_note()
        if "mention" in shape:
            kwargs["mentions"] = MENTION_ARGS
        return kwargs

    def run_shape(self, shape: str, decision: str):
        with mock.patch.object(agent_views, "resolve_mentions", return_value=[MENTION]):
            run_id, token = self.ask(**self.ask_kwargs(shape))
            self.decide_to_the_end(run_id, token, decision)
        return run_id

    # -- what the provider was sent ---------------------------------------------

    def normalised(self, text: str | None) -> str | None:
        """Ids differ per run of the suite; the text around them is the subject."""
        if text is None:
            return None
        for name in ("channel", "root", "note"):
            obj = getattr(self, name, None)
            if obj is not None:
                text = text.replace(str(getattr(obj, "id", None) or obj.pk), f"<{name}>")
        text = text.replace(str(self.team.team_id), "<team>").replace(str(self.user.id), "<user>")
        return re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", text)

    def transcript(self) -> list[dict]:
        out = []
        for system_instruction, messages in self.calls:
            out.append(
                {
                    "system": self.normalised(system_instruction),
                    "messages": [
                        json.loads(self.normalised(json.dumps(dataclasses.asdict(m), default=str)))
                        for m in messages
                    ],
                }
            )
        return out

    def digest(self) -> str:
        blob = json.dumps(self.transcript(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def assert_golden(self, shape: str, decision: str):
        key = f"{shape}/{decision}"
        digest = self.digest()
        if os.environ.get("RECORD_GOLDEN"):
            print(f'GOLDEN {key} = "{digest}"')
            return
        self.assertEqual(digest, GOLDEN[key], f"what the model is sent changed for {key}")


class FlagOffTests(PinBase):
    """Every flag off: the whole run sends what `origin/main` sends."""

    flags = {**PinBase.flags, **OFF}

    def pin(self, shape, decision):
        self.run_shape(shape, decision)
        self.assertEqual(len(self.calls), 2, "the ask leg's call and the resumed leg's")
        self.assert_golden(shape, decision)

    def test_plain_approve(self):
        self.pin("plain", "approve")

    def test_plain_reject(self):
        self.pin("plain", "reject")

    def test_thread_approve(self):
        self.pin("thread", "approve")

    def test_thread_reject(self):
        self.pin("thread", "reject")

    def test_note_approve(self):
        self.pin("note", "approve")

    def test_note_reject(self):
        self.pin("note", "reject")

    def test_mention_approve(self):
        self.pin("mention", "approve")

    def test_mention_reject(self):
        self.pin("mention", "reject")

    def test_thread_and_mention_approve(self):
        self.pin("thread+mention", "approve")

    def test_thread_and_mention_reject(self):
        self.pin("thread+mention", "reject")

    # -- the two gates, each on its own -------------------------------------------

    def test_the_pause_keeps_nothing_with_the_flag_off(self):
        # Defeats a removed WRITE gate: the hash pins alone stay green when the row is
        # written and merely not read.
        with mock.patch.object(agent_views, "resolve_mentions", return_value=[MENTION]):
            run_id, _token = self.ask(**self.ask_kwargs("thread+mention"))
        run = AgentRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertIsNone(run.resume_context)

    def test_a_row_kept_while_the_flag_was_on_is_not_replayed_with_it_off(self):
        # Defeats a removed READ gate: a run paused while the flag was on (so its row
        # holds the blocks) and decided after it was switched off resumes as main does.
        with (
            override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **ON_977}),
            mock.patch.object(agent_views, "resolve_mentions", return_value=[MENTION]),
        ):
            run_id, token = self.ask(**self.ask_kwargs("thread+mention"))
        self.assertIsNotNone(AgentRun.objects.get(run_id=run_id).resume_context)
        self.decide_to_the_end(run_id, token, "approve")
        self.assertEqual(self.legs[1], None, "the resumed leg must carry no system_extra")
        self.assertNotIn(THREAD_SUMMARY, self.calls[1][0])

class FlagOnTests(PinBase):
    """977 on: the resumed leg differs from main, in the one intended way."""

    flags = {**PinBase.flags, **ON_977}

    def resumed_system(self, shape, decision="approve") -> str:
        self.run_shape(shape, decision)
        self.assertEqual(len(self.calls), 2)
        if not os.environ.get("RECORD_GOLDEN"):
            self.assertNotEqual(self.digest(), GOLDEN[f"{shape}/{decision}"])
        return self.calls[1][0]

    def test_the_thread_block_reaches_the_resumed_leg(self):
        system = self.resumed_system("thread")
        self.assertIn(THREAD_SUMMARY, system)
        self.assertEqual(system, self.calls[0][0], "both legs send the same system instruction")

    def test_the_note_block_reaches_the_resumed_leg(self):
        self.assertIn(NOTE_SUMMARY, self.resumed_system("note", "reject"))

    def test_the_references_reach_the_resumed_leg(self):
        self.assertIn("Fix login flow", self.resumed_system("mention"))

    def test_a_plain_ask_is_unchanged_with_the_flag_on(self):
        self.run_shape("plain", "approve")
        if not os.environ.get("RECORD_GOLDEN"):
            self.assertEqual(self.digest(), GOLDEN["plain/approve"])
