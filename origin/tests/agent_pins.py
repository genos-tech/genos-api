"""The one home of every sha256 pin on what the in-app agent's model is sent (MIP-1653).

A "pin" test says: with the flag off (or in this fixed scenario) the model is sent exactly these bytes.
Each pin used to hold its own hash in its own test module, with its own `RECORD_GOLDEN=1` recipe, so
changing one tool description meant finding ten modules by their failures and re-recording each. Now:

  * the hashes live in ONE file, `origin/tests/golden/agent_pins.json` (`{module: {key: sha256}}`);
  * `PIN_MODULES` below is the ONE list of the modules that hold pins, with the test classes that
    record them;
  * ONE command re-records all of them and the fingerprint golden
    (`origin/tests/golden/agent_fingerprint.json`) together:

        python manage.py record_agent_goldens

    In a worktree, through the key-free test runner (the command needs the test database and refuses
    to run with live credentials; the same shape as `manage.py test`):

        genos-platform/docker/dt.sh <worktree> <db-suffix> 'python manage.py record_agent_goldens'

    It prints, per module, which keys moved. `git diff origin/tests/golden` is then the whole
    model-visible change as a reviewer sees it; say so in the PR body (the change is owed the grouped
    paid run, not a per-PR eval).

What each pin computes is unchanged by this module: the digest functions stay in the pin modules, so a
pin fails on the same kind of change as before. Only where the expected value is stored moved.
`test_agent_pins_registry` fails when a test module holds a sha256 literal and is neither in
`PIN_MODULES` nor on its short allowlist of hashes that are not about what the model is sent, so a new
pin cannot be forgotten.

ADDING A PIN. In the new module: `PINS = agent_pins.Pins(__name__)`, `GOLDEN = PINS.golden`, and
`PINS.check(self, key, digest)` where the test used to `assertEqual(digest, GOLDEN[key])`. Add the module
and the class that records it to `PIN_MODULES`, run the command, commit the JSON.

RECORDING MODE. `record_agent_goldens` sets `RECORD_GOLDEN=1` for the run: `Pins.check` then stores the
digest instead of asserting. The variable is not a recipe on its own any more: outside the command,
`RECORD_GOLDEN=1 manage.py test <pin module>` fails with a message that points at the command, so it can
never silently skip a pin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PINS_PATH = Path(__file__).resolve().parent / "golden" / "agent_pins.json"
PINS_VERSION = 1

# Set by `record_agent_goldens` while it runs the recording classes; the only thing that may switch
# `Pins.check` from asserting to storing.
COMMAND_ACTIVE = False

# module name (`origin/tests/<name>.py`) -> the test classes that record its pins. One entry per
# module that holds a pin. A class listed here must make every `Pins.check` call of the module (and
# nothing in it may need the other classes), because the command runs only these classes.
PIN_MODULES: dict[str, tuple[str, ...]] = {
    "test_agent_closed_ancestor_rule": ("FlagOffTests",),
    "test_agent_content_length_pins": ("FlagOffPins",),
    "test_agent_fetch_task_anchors": ("FlagOffTests",),
    "test_agent_inbox_report_label": ("FlagOffTests",),
    "test_agent_note_threads_in_get_note": ("FlagOffTests",),
    "test_agent_reject_reason": ("PromptPins", "FlagOffTests"),
    "test_agent_resume_flag_pins": ("FlagOffTests", "FollowUpFlagOffTests"),
    "test_agent_team_in_current_user": ("FlagOffTests",),
    "test_agent_tell_model_undo": ("FlagOffTests",),
    "test_agent_truncation_pins": ("NormalRunPins",),
    "test_inapp_timeline_tool": ("TestFlagOffIsByteIdenticalToMain",),
    "test_mcp_search_window": ("InAppAgentIsUnchangedTests",),
    "test_task_close_notice": ("TestFlagOffIsMain",),
}

# What a failing pin says. One command, named once, here.
RECORD_COMMAND = "python manage.py record_agent_goldens"
RE_RECORD_HINT = (
    "If this change is MEANT to move what the model sees, re-record every pin and the fingerprint "
    f"golden in one go with `{RECORD_COMMAND}` (under dt.sh: genos-platform/docker/dt.sh <worktree> "
    f"<suffix> '{RECORD_COMMAND}') and say so in the PR body; the list of pin modules is "
    "PIN_MODULES in origin/tests/agent_pins.py."
)


def record_mode() -> bool:
    return bool(os.environ.get("RECORD_GOLDEN"))


def read_all() -> dict[str, dict[str, str]]:
    """Every module's recorded hashes, `{}` when the file does not exist yet."""
    if not PINS_PATH.exists():
        return {}
    return json.loads(PINS_PATH.read_text(encoding="utf-8")).get("pins", {})


def render(pins: dict[str, dict[str, str]]) -> str:
    """The file's text for `pins`: stable, so a re-record with no change is a no-op in git."""
    record = {
        "version": PINS_VERSION,
        "about": (
            "sha256 pins on what the in-app agent's model is sent, one object per pin module. "
            f"Do not edit by hand: re-record all of them with `{RECORD_COMMAND}` "
            "(origin/tests/agent_pins.py lists the modules)."
        ),
        "pins": {module: dict(sorted(keys.items())) for module, keys in sorted(pins.items())},
    }
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


# Digests taken while recording: {module: {key: [digest, ...]}}; the command reads and clears it.
RECORDED: dict[str, dict[str, list[str]]] = {}


class Pins:
    """The pins of one test module: its recorded hashes and the check every pin test calls."""

    def __init__(self, module_name: str):
        self.module = module_name.rsplit(".", 1)[-1]
        # Read at import: importers (`from ..._pins import GOLDEN`) get a plain dict.
        self.golden: dict[str, str] = dict(read_all().get(self.module, {}))

    def require(self, key: str) -> str:
        """The recorded hash for `key`, for a test that compares with it some other way than
        `check` (an `assertNotEqual` on the flag-on side); a missing key fails loudly, so such a
        comparison can never pass against nothing."""
        try:
            return self.golden[key]
        except KeyError:
            raise AssertionError(
                f"{self.module}: no recorded hash for {key!r} in origin/tests/golden/agent_pins.json. "
                f"{RE_RECORD_HINT}"
            ) from None

    def check(self, test, key: str, digest: str, message: str | None = None) -> None:
        """Assert `digest` is what was recorded for `key`; while recording, store it instead."""
        if record_mode():
            if not COMMAND_ACTIVE:
                test.fail(
                    "RECORD_GOLDEN is set outside the recording command, so nothing would be "
                    f"recorded. Run `{RECORD_COMMAND}` instead."
                )
            RECORDED.setdefault(self.module, {}).setdefault(key, []).append(digest)
            return
        recorded = self.golden.get(key)
        if recorded is None:
            test.fail(
                f"{self.module}: no recorded hash for {key!r} in origin/tests/golden/agent_pins.json. "
                f"{RE_RECORD_HINT}"
            )
        test.assertEqual(
            digest,
            recorded,
            f"{message or 'what the model is sent changed'}: {key}. {RE_RECORD_HINT}",
        )
