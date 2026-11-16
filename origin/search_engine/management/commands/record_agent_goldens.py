"""`python manage.py record_agent_goldens`: re-record every pin on what the in-app model is sent (MIP-1653).

    genos-platform/docker/dt.sh <worktree> <db-suffix> 'python manage.py record_agent_goldens'

Changing a tool description, a prompt line or a cap is a model-visible change, and the free evidence
for it is a set of sha256 goldens that must be re-recorded in the same PR:

  * the fingerprint golden, `origin/tests/golden/agent_fingerprint.json` (every surface, in the two
    golden states; `agent_fingerprint --write-golden` writes only this one);
  * the pins of the test modules listed in `PIN_MODULES` (`origin/tests/agent_pins.py`), stored in
    `origin/tests/golden/agent_pins.json`: "with the flag off the model is sent exactly this" for the
    declarations, the system prompt, whole request shapes and a few tool results.

This command re-records both in one run, through the test runner's own machinery (a throwaway test
database, the test environment), so it works wherever `manage.py test` does, `dt.sh` included. It runs
the recording classes of each listed module with `RECORD_GOLDEN=1`, which makes `Pins.check` store
the digest instead of asserting it, then computes the fingerprint golden in the same database.

It writes NOTHING unless the whole run is clean: a recording class that fails, errors or skips, a key
that was recorded twice with two different values (a nondeterministic pin), or a module that recorded
no key stops it, and the files stay as they were. On success it says, per module, which keys moved;
that list, and `git diff origin/tests/golden`, are what the PR body names.

Like `manage.py test` it needs the test settings (`apis.settings_test`) and refuses live model or
billing credentials in the environment (`dt.sh` has none); no model is called.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import get_runner

from origin.search_engine.management.commands import agent_fingerprint as fp
from origin.tests import agent_pins


def labels() -> list[str]:
    """The test classes that record the pins, one label per class of `PIN_MODULES`."""
    return [
        f"origin.tests.{module}.{cls}"
        for module, classes in agent_pins.PIN_MODULES.items()
        for cls in classes
    ]


def settle(recorded: dict[str, dict[str, list[str]]]) -> dict[str, dict[str, str]]:
    """One digest per key, or a `CommandError` naming what cannot be recorded.

    A key recorded more than once (a pin called in a loop, or a test class run twice) must have
    produced the same digest every time: otherwise the pin depends on something that changes, and
    recording either value would make the pin flaky. A module with no key at all did not run.
    """
    problems: list[str] = []
    pins: dict[str, dict[str, str]] = {}
    for module in agent_pins.PIN_MODULES:
        keys = recorded.get(module, {})
        if not keys:
            problems.append(f"{module}: recorded no key (did its recording class run?)")
            continue
        pins[module] = {}
        for key, digests in keys.items():
            if len(set(digests)) != 1:
                problems.append(f"{module}: {key!r} produced {len(set(digests))} different digests")
            pins[module][key] = digests[0]
    stray = sorted(set(recorded) - set(agent_pins.PIN_MODULES))
    if stray:
        problems.append(f"recorded by modules that are not in PIN_MODULES: {', '.join(stray)}")
    if problems:
        raise CommandError("Not recording anything:\n  " + "\n  ".join(problems))
    return pins


def moved(
    before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]
) -> dict[str, list[str]]:
    """Per module, the keys whose hash changed, appeared or vanished; modules with none are absent."""
    out: dict[str, list[str]] = {}
    for module in sorted(set(before) | set(after)):
        old, new = before.get(module, {}), after.get(module, {})
        keys = [
            f"{key}{'' if key in old and key in new else (' (new)' if key in new else ' (gone)')}"
            for key in sorted(set(old) | set(new))
            if old.get(key) != new.get(key)
        ]
        if keys:
            out[module] = keys
    return out


def refuse_unless_safe() -> None:
    if settings.SETTINGS_MODULE != "apis.settings_test":
        raise CommandError(
            "record_agent_goldens runs the test classes that hold the pins, so it needs the test "
            "settings: use genos-platform/docker/dt.sh (`dt.sh <worktree> <db-suffix> 'python "
            "manage.py record_agent_goldens'`, key-free, on the tmpfs Postgres), or "
            "set DJANGO_SETTINGS_MODULE=apis.settings_test."
        )
    from apis import settings_test  # noqa: PLC0415

    live = getattr(settings_test, "_live", [])
    if live and os.environ.get("ALLOW_LIVE_LLM_IN_TESTS") != "1":
        raise CommandError(
            "Refusing to run with live credentials in the environment ("
            + ", ".join(live)
            + "): `manage.py test` refuses the same. Run it through genos-platform/docker/dt.sh."
        )


class Command(BaseCommand):
    help = (
        "Re-record the fingerprint golden and every model-visible sha256 pin "
        "(origin/tests/agent_pins.py lists them) in one run. Test settings only; no model call."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        refuse_unless_safe()
        before = agent_pins.read_all()
        old_fingerprint = (
            json.loads(fp.GOLDEN_PATH.read_text(encoding="utf-8"))
            if fp.GOLDEN_PATH.exists()
            else {}
        )

        agent_pins.RECORDED.clear()
        agent_pins.COMMAND_ACTIVE = True
        previous = os.environ.get("RECORD_GOLDEN")
        os.environ["RECORD_GOLDEN"] = "1"
        runner = get_runner(settings)(interactive=False, verbosity=0)
        record = None
        logging.disable(logging.CRITICAL)  # the app's request/perf log lines would bury the report
        try:
            runner.setup_test_environment()
            try:
                suite = runner.build_suite(labels())
                old_config = runner.setup_databases(aliases={"default"})
                try:
                    result = runner.run_suite(suite)
                    if result.errors or result.failures or result.skipped:
                        raise CommandError(
                            "Not recording anything: a recording class did not pass cleanly "
                            f"({len(result.errors)} errors, {len(result.failures)} failures, "
                            f"{len(result.skipped)} skipped). The output above names the test."
                        )
                    if result.testsRun == 0:
                        raise CommandError("Not recording anything: no test ran.")
                    pins = settle(agent_pins.RECORDED)
                    record = fp.golden_record()
                finally:
                    runner.teardown_databases(old_config)
            finally:
                runner.teardown_test_environment()
        finally:
            logging.disable(logging.NOTSET)
            agent_pins.COMMAND_ACTIVE = False
            if previous is None:
                os.environ.pop("RECORD_GOLDEN", None)
            else:
                os.environ["RECORD_GOLDEN"] = previous

        assert record is not None
        agent_pins.PINS_PATH.parent.mkdir(parents=True, exist_ok=True)
        agent_pins.PINS_PATH.write_text(agent_pins.render(pins), encoding="utf-8")
        fp.GOLDEN_PATH.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.report(before, pins, old_fingerprint, record)

    def report(
        self,
        before: dict[str, dict[str, str]],
        pins: dict[str, dict[str, str]],
        old_fingerprint: dict[str, Any],
        record: dict[str, Any],
    ) -> None:
        out = self.stdout.write
        changed = moved(before, pins)
        out(f"pins: {len(pins)} test modules, {sum(len(v) for v in pins.values())} keys")
        for module, keys in changed.items():
            out(f"  moved  {module}: {', '.join(keys)}")
        if not changed:
            out("  no pin moved")
        lines = fp.golden_diff(old_fingerprint, record["states"]) if old_fingerprint else []
        out(
            f"fingerprint: {len(fp.GOLDEN_STATES)} states, "
            f"{len(record['states'][fp.GOLDEN_STATES[0]]['surfaces'])} surfaces each"
        )
        for line in lines:
            out(f"  moved  {line}")
        if not lines:
            out("  no surface moved")
        out(f"wrote {agent_pins.PINS_PATH}")
        out(f"wrote {fp.GOLDEN_PATH}")
        out(
            "Commit both files in the PR that changed what the model sees, and say in the PR body "
            "which keys moved (the lines above): the change is owed the grouped paid run."
        )
