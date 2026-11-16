"""`python manage.py record_agent_goldens`: re-record every pin on what the in-app model is sent (MIP-1653).

    genos-platform/docker/dt.sh <worktree> <db-suffix> 'python manage.py record_agent_goldens'

Changing a tool description, a prompt line or a cap is a model-visible change, and the free evidence
for it is a set of sha256 goldens that must be re-recorded in the same PR:

  * the fingerprint golden, `origin/tests/golden/agent_fingerprint.json` (every surface, in the two
    golden states; `agent_fingerprint --write-golden` writes only this one);
  * the pins of the test modules listed in `PIN_MODULES` (`origin/services/agent_pins.py`), stored in
    `origin/tests/golden/agent_pins.json`: "with the flag off the model is sent exactly this" for the
    declarations, the system prompt, whole request shapes and a few tool results.

This command re-records both in one run, through the test runner's own machinery (a throwaway test
database, the test environment), so it works wherever `manage.py test` does, `dt.sh` included. It runs
the recording classes of each listed module with `RECORD_GOLDEN=1`, which makes `Pins.check` store
the digest instead of asserting it, then computes the fingerprint golden in the same database.

It writes NOTHING unless the whole run is clean: a recording class that fails or errors, a key that was
recorded twice with two different values (a nondeterministic pin), a module that recorded no key, or a key
in the file that was not recorded this time (`--drop-missing` if removing it is the point) stops it, and
the files stay as they were; the two files are written together or not at all. A test database is built
and dropped the way `manage.py test --noinput` does it (named by `DJANGO_TEST_DB_NAME`, which `dt.sh`
sets): use your own suffix, never one a test run is using. On success it says, per module, which keys moved;
that list, and `git diff origin/tests/golden`, are what the PR body names.

Like `manage.py test` it needs the test settings (`apis.settings_test`) and refuses live model or
billing credentials in the environment (`dt.sh` has none); no model is called.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test.utils import get_runner

from origin.search_engine.management.commands import agent_fingerprint as fp
from origin.services import agent_pins


def labels() -> list[str]:
    """The test classes that record the pins, one label per class of `PIN_MODULES`."""
    return [
        f"origin.tests.{module}.{cls}"
        for module, classes in agent_pins.PIN_MODULES.items()
        for cls in classes
    ]


def settle(
    recorded: dict[str, dict[str, list[str]]],
    previous: dict[str, dict[str, str]] | None = None,
    *,
    drop_missing: bool = False,
) -> dict[str, dict[str, str]]:
    """One digest per key, or a `CommandError` naming what cannot be recorded.

    A key recorded more than once (a pin called in a loop, or a test class run twice) must have
    produced the same digest every time: otherwise the pin depends on something that changes, and
    recording either value would make the pin flaky. A module with no key at all did not run. A key
    that is in the file but was not recorded this time (its test was deleted, or skipped) would
    silently disappear from the file, so it stops the run unless `drop_missing` says it is on purpose.
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
        gone = sorted(set((previous or {}).get(module, {})) - set(keys))
        if gone and not drop_missing:
            problems.append(
                f"{module}: no longer recorded: {', '.join(gone)} (a deleted or skipped test? "
                "pass --drop-missing if removing them is the point)"
            )
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


def write_atomically(files: dict[Path, str]) -> None:
    """Write every file, or none of them: each goes to a temp file first, then they are renamed."""
    staged: list[tuple[Path, Path]] = []
    try:
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            staged.append((Path(tmp), path))
    except BaseException:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        raise
    for tmp, path in staged:
        os.replace(tmp, path)


class Command(BaseCommand):
    help = (
        "Re-record the fingerprint golden and every model-visible sha256 pin "
        "(origin/services/agent_pins.py lists them) in one run. Test settings only; no model call."
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--drop-missing",
            action="store_true",
            help="Let a key that is in the file but was not recorded this time disappear from it.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        refuse_unless_safe()
        before = agent_pins.read_all()
        old_fingerprint = (
            json.loads(fp.GOLDEN_PATH.read_text(encoding="utf-8"))
            if fp.GOLDEN_PATH.exists()
            else None
        )

        agent_pins.RECORDED.clear()
        agent_pins.COMMAND_ACTIVE = True
        previous = os.environ.get("RECORD_GOLDEN")
        os.environ["RECORD_GOLDEN"] = "1"
        runner = get_runner(settings)(interactive=False, verbosity=0)
        pins: dict[str, dict[str, str]] = {}
        record: dict[str, Any] = {}
        skipped = 0
        logging.disable(logging.CRITICAL)  # the app's request/perf log lines would bury the report
        try:
            runner.setup_test_environment()
            try:
                suite = runner.build_suite(labels())
                old_config = runner.setup_databases(aliases={"default"})
                try:
                    # The fingerprint first, before any test class has run in this process.
                    record = fp.golden_record()
                    result = runner.run_suite(suite)
                    if result.errors or result.failures:
                        raise CommandError(
                            "Not recording anything: a recording class did not pass cleanly "
                            f"({len(result.errors)} errors, {len(result.failures)} failures). "
                            "The output above names the test."
                        )
                    if result.testsRun == 0:
                        raise CommandError("Not recording anything: no test ran.")
                    skipped = len(result.skipped)
                    pins = settle(agent_pins.RECORDED, before, drop_missing=options["drop_missing"])
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

        write_atomically(
            {
                agent_pins.PINS_PATH: agent_pins.render(pins),
                fp.GOLDEN_PATH: json.dumps(record, indent=2, sort_keys=True) + "\n",
            }
        )
        self.report(before, pins, old_fingerprint, record, skipped)

    def report(
        self,
        before: dict[str, dict[str, str]],
        pins: dict[str, dict[str, str]],
        old_fingerprint: dict[str, Any] | None,
        record: dict[str, Any],
        skipped: int,
    ) -> None:
        out = self.stdout.write
        changed = moved(before, pins)
        out(f"pins: {len(pins)} test modules, {sum(len(v) for v in pins.values())} keys")
        if not before:
            out("  the pins file did not exist: every key is new")
        for module, keys in changed.items():
            out(f"  moved  {module}: {', '.join(keys)}")
        if before and not changed:
            out("  no pin moved")
        out(
            f"fingerprint: {len(fp.GOLDEN_STATES)} states, "
            f"{len(record['states'][fp.GOLDEN_STATES[0]]['surfaces'])} surfaces each"
        )
        if old_fingerprint is None:
            out("  the fingerprint golden did not exist: every surface is new")
        else:
            lines = fp.golden_diff(old_fingerprint, record["states"])
            for line in lines:
                out(f"  moved  {line}")
            if not lines:
                out("  no surface moved")
        if skipped:
            out(
                f"note: {skipped} test(s) in the recording classes were skipped (they record no key)"
            )
        out(f"wrote {agent_pins.PINS_PATH}")
        out(f"wrote {fp.GOLDEN_PATH}")
        out(
            "Commit both files in the PR that changed what the model sees, and say in the PR body "
            "which keys moved (the lines above): the change is owed the grouped paid run."
        )
