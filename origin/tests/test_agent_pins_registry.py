"""MIP-1653: the list of model-visible sha256 pins is complete, and the pin helper does what it says.

`origin/services/agent_pins.py` holds `PIN_MODULES`, the one list that `manage.py record_agent_goldens`
re-records. A pin that is not on it would be left stale by the next deliberate change to a tool
description, a prompt line or a cap, and show up only as a failure in a whole-suite run. So:

  * every test module that holds a sha256 literal is either in `PIN_MODULES` or in `OTHER_HASHES`
    below, with the reason it is not about what the model is sent;
  * every listed module exists, uses the helper, has a recording class that loads, and has an entry
    in `origin/tests/golden/agent_pins.json` (and the file has no entry for an unlisted module);
    a key a pin reads through a helper or a loop is checked when the pin test runs (a key the file lacks
    fails it with a message naming the command, which adds it), not by the static scan here;
  * `test_agent_content_length` and `test_agent_body_on_user_model` hold no hash of their own: they read
    `GOLDEN` of `test_agent_content_length_pins`, which the command records;
  * `Pins.check` asserts normally, stores while the command records, and refuses `RECORD_GOLDEN`
    set by hand (it would silently record nothing).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from django.core.management import CommandError
from django.test import SimpleTestCase, override_settings

from origin.search_engine.management.commands import record_agent_goldens as command
from origin.services import agent_pins

TESTS = Path(__file__).resolve().parent
HASH = re.compile(r"[\"'][0-9a-f]{64}[\"']")

# Test modules that hold a sha256 literal and are NOT pins on what the in-app model is sent: each has
# its own recipe. A new module with a hash that is not about the model's input belongs here, with a
# reason; one that IS about it belongs in `agent_pins.PIN_MODULES`.
OTHER_HASHES = {
    "test_agent_eval_sequence_fixture.py": "the eval fixture's own source and setup output, not a model input",
    "test_builtin_task_templates.py": "the built-in task templates a person picks from, not the agent's input",
    "test_demo_v1_golden.py": "the demo workspace seeder's output",
    "test_mcp_instructions.py": "the MCP handshake text sent to EXTERNAL clients (MIP-1619), never the in-app agent's input",
}


def test_modules_with_hashes() -> set[str]:
    out = set()
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        if HASH.search(path.read_text(encoding="utf-8")):
            out.add(path.name)
    return out


class TheListIsComplete(SimpleTestCase):
    def test_every_module_with_a_hash_is_a_pin_or_says_why_not(self):
        listed = {f"{m}.py" for m in agent_pins.PIN_MODULES}
        found = test_modules_with_hashes()
        unknown = sorted(found - listed - set(OTHER_HASHES))
        self.assertEqual(
            unknown,
            [],
            "A test module holds a sha256 literal and is neither a pin nor allowed: if it pins what "
            "the in-app model is sent, give it `PINS = agent_pins.Pins(__name__)` and add it (and the "
            "class that records it) to PIN_MODULES in origin/services/agent_pins.py; if not, add it to "
            "OTHER_HASHES in this file with the reason.",
        )

    def test_the_allowlist_does_not_name_a_module_that_has_no_hash_or_is_a_pin(self):
        listed = {f"{m}.py" for m in agent_pins.PIN_MODULES}
        found = test_modules_with_hashes()
        self.assertEqual(sorted(set(OTHER_HASHES) - found), [], "no longer holds a hash: drop it")
        self.assertEqual(sorted(set(OTHER_HASHES) & listed), [], "it is a pin now: drop it here")

    def test_a_module_that_uses_the_helper_is_listed(self):
        users = {
            path.stem
            for path in TESTS.glob("test_*.py")
            if "agent_pins.Pins(" in path.read_text(encoding="utf-8")
            and path.name != Path(__file__).name
        }
        self.assertEqual(sorted(users - set(agent_pins.PIN_MODULES)), [])
        self.assertEqual(sorted(set(agent_pins.PIN_MODULES) - users), [])

    def test_every_recording_class_loads_and_holds_tests(self):
        for label in command.labels():
            with self.subTest(label=label):
                suite = unittest.defaultTestLoader.loadTestsFromName(label)
                self.assertGreater(suite.countTestCases(), 0)
                self.assertFalse(
                    any(type(t).__name__ == "_FailedTest" for t in _flatten(suite)), label
                )

    def test_the_golden_file_has_every_listed_module_and_nothing_else(self):
        recorded = agent_pins.read_all()
        self.assertEqual(sorted(recorded), sorted(agent_pins.PIN_MODULES))
        for module, keys in recorded.items():
            self.assertTrue(keys, module)
            for key, digest in keys.items():
                self.assertRegex(digest, r"^[0-9a-f]{64}$", f"{module} {key}")

    def test_every_key_written_as_a_literal_is_in_the_file(self):
        # A PARTIAL static check: only `PINS.check(self, "<key>", ...)` with a literal key. Keys passed
        # through a helper or a loop are covered when the pin test runs (a key the file lacks fails it
        # with a message naming the command, which adds the key).
        recorded = agent_pins.read_all()
        for module in agent_pins.PIN_MODULES:
            source = (TESTS / f"{module}.py").read_text(encoding="utf-8")
            literal = set(re.findall(r'PINS\.check\(self, "([^"]+)"', source))
            self.assertLessEqual(literal, set(recorded[module]), module)


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


class PinsHelper(SimpleTestCase):
    def setUp(self):
        self.pins = agent_pins.Pins("origin.tests.test_agent_pins_registry")
        self.pins.golden = {"a": "1" * 64}
        saved = dict(agent_pins.RECORDED)
        agent_pins.RECORDED.clear()
        self.addCleanup(lambda: (agent_pins.RECORDED.clear(), agent_pins.RECORDED.update(saved)))

    def test_it_asserts_against_the_recorded_hash(self):
        self.pins.check(self, "a", "1" * 64)
        with self.assertRaises(AssertionError) as ctx:
            self.pins.check(self, "a", "2" * 64, "what the model is sent changed")
        self.assertIn("record_agent_goldens", str(ctx.exception))
        self.assertIn("PIN_MODULES", str(ctx.exception))

    def test_require_returns_the_hash_and_a_missing_key_fails_instead_of_comparing_with_nothing(
        self,
    ):
        self.assertEqual(self.pins.require("a"), "1" * 64)
        with self.assertRaises(AssertionError) as ctx:
            self.pins.require("missing")
        self.assertIn("record_agent_goldens", str(ctx.exception))

    def test_require_while_recording_returns_what_was_recorded_so_far(self):
        with mock.patch.dict("os.environ", {"RECORD_GOLDEN": "1"}):
            self.assertEqual(self.pins.require("a"), "1" * 64)  # the old hash until re-recorded
            self.assertEqual(self.pins.require("new"), "")  # a new pin: nothing to compare with
            with mock.patch.object(agent_pins, "COMMAND_ACTIVE", True):
                self.pins.check(self, "a", "2" * 64)
            self.assertEqual(self.pins.require("a"), "2" * 64)

    def test_a_key_the_file_lacks_fails_naming_the_command(self):
        with self.assertRaises(AssertionError) as ctx:
            self.pins.check(self, "missing", "1" * 64)
        self.assertIn("record_agent_goldens", str(ctx.exception))

    def test_while_the_command_records_it_stores_and_does_not_assert(self):
        with (
            mock.patch.dict("os.environ", {"RECORD_GOLDEN": "1"}),
            mock.patch.object(agent_pins, "COMMAND_ACTIVE", True),
        ):
            self.pins.check(self, "a", "2" * 64)
            self.pins.check(self, "new", "3" * 64)
        self.assertEqual(
            agent_pins.RECORDED, {self.pins.module: {"a": ["2" * 64], "new": ["3" * 64]}}
        )

    def test_record_golden_set_by_hand_is_refused_so_nothing_is_silently_skipped(self):
        with mock.patch.dict("os.environ", {"RECORD_GOLDEN": "1"}):
            with self.assertRaises(AssertionError) as ctx:
                self.pins.check(self, "a", "1" * 64)
        self.assertIn("record_agent_goldens", str(ctx.exception))
        self.assertEqual(agent_pins.RECORDED, {})

    def test_the_rendered_file_is_stable_and_sorted(self):
        text = agent_pins.render({"b": {"z": "1" * 64, "a": "2" * 64}, "a": {"k": "3" * 64}})
        self.assertEqual(
            text, agent_pins.render({"a": {"k": "3" * 64}, "b": {"a": "2" * 64, "z": "1" * 64}})
        )
        self.assertTrue(text.endswith("}\n"))
        self.assertLess(text.index('"a"'), text.index('"b"'))


class TheCommandsGuards(SimpleTestCase):
    def test_settle_refuses_a_nondeterministic_or_missing_pin(self):
        modules = list(agent_pins.PIN_MODULES)
        full = {m: {"k": ["1" * 64]} for m in modules}
        self.assertEqual(command.settle(full), {m: {"k": "1" * 64} for m in modules})
        flaky = {**full, modules[0]: {"k": ["1" * 64, "2" * 64]}}
        with self.assertRaisesRegex(CommandError, "different digests"):
            command.settle(flaky)
        missing = {m: v for m, v in full.items() if m != modules[0]}
        with self.assertRaisesRegex(CommandError, "recorded no key"):
            command.settle(missing)
        with self.assertRaisesRegex(CommandError, "not in PIN_MODULES"):
            command.settle({**full, "test_stray": {"k": ["1" * 64]}})

    def test_settle_refuses_a_key_that_silently_disappears_unless_told(self):
        modules = list(agent_pins.PIN_MODULES)
        full = {m: {"k": ["1" * 64]} for m in modules}
        previous = {m: {"k": "1" * 64} for m in modules}
        previous[modules[0]]["old"] = "2" * 64
        with self.assertRaisesRegex(CommandError, "no longer recorded: old"):
            command.settle(full, previous)
        self.assertNotIn("old", command.settle(full, previous, drop_missing=True)[modules[0]])

    def test_the_files_are_written_together_or_not_at_all(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a.json", Path(tmp) / "sub" / "b.json"
            command.write_atomically({a: "A", b: "B"})
            self.assertEqual((a.read_text(), b.read_text()), ("A", "B"))
            with mock.patch.object(command.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    command.write_atomically({a: "A2", b: "B2"})
            self.assertEqual((a.read_text(), b.read_text()), ("A", "B"))

    def test_an_unwritable_target_leaves_no_temp_file_and_no_partial_write(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a.json"
            blocked = Path(tmp) / "file"
            blocked.write_text("x")
            with self.assertRaises(OSError):
                command.write_atomically({a: "A", blocked / "b.json": "B"})
            self.assertFalse(a.exists())
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["file"])

    def test_moved_names_the_keys_that_changed_appeared_or_vanished(self):
        before = {"m": {"a": "1", "b": "1", "c": "1"}, "same": {"x": "1"}}
        after = {"m": {"a": "2", "b": "1", "d": "1"}, "same": {"x": "1"}}
        self.assertEqual(command.moved(before, after), {"m": ["a", "c (gone)", "d (new)"]})

    def test_it_refuses_settings_that_are_not_the_test_settings(self):
        with override_settings(SETTINGS_MODULE="apis.settings"):
            with self.assertRaisesRegex(CommandError, "dt.sh"):
                command.refuse_unless_safe()

    def test_it_refuses_live_credentials(self):
        from apis import settings_test

        with (
            mock.patch.object(settings_test, "_live", ["CLAUDE_API_KEY"]),
            mock.patch.dict("os.environ", {"ALLOW_LIVE_LLM_IN_TESTS": ""}),
        ):
            with self.assertRaisesRegex(CommandError, "live credentials"):
                command.refuse_unless_safe()
