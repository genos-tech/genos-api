#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys

PRODUCTION_SETTINGS = "apis.settings"
TEST_SETTINGS = "apis.settings_test"


def settings_module_for(argv, environ):
    """Pick the settings module for this run, as `(module, defaulted_for_tests)`.

    `manage.py test` runs on `apis.settings_test` (what CI and genos-platform's `dt.sh` use) unless
    the caller chose otherwise: a `DJANGO_SETTINGS_MODULE` in the environment or a `--settings`
    argument wins. Every other command keeps `apis.settings`. Without this, a bare
    `manage.py test` ran on production settings (flags on, the schedule plan gate shut) and failed
    tests that pass in CI (MIP-1634). The condition on `argv` is the one `settings_test`'s
    live-credentials guard uses, so the two agree on what "a test run" is.
    """
    if environ.get("DJANGO_SETTINGS_MODULE"):  # set and not empty
        return environ["DJANGO_SETTINGS_MODULE"], False
    if argv[1:2] == ["test"] and not any(
        arg == "--settings" or arg.startswith("--settings=") for arg in argv[2:]
    ):
        return TEST_SETTINGS, True
    return PRODUCTION_SETTINGS, False


def main():
    """Run administrative tasks."""
    module, defaulted_for_tests = settings_module_for(sys.argv, os.environ)
    os.environ["DJANGO_SETTINGS_MODULE"] = module
    if defaulted_for_tests:
        print(
            f"manage.py test: using {TEST_SETTINGS} (as CI does). "
            f"Override: --settings={PRODUCTION_SETTINGS} or DJANGO_SETTINGS_MODULE.",
            file=sys.stderr,
        )
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
