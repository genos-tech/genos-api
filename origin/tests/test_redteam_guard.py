"""The local red-team harness must never leave the machine (MIP-2267).

The harness lives in `scripts/redteam/` (standard library only, run next to a local copy of the
api). Its guard tests are collected here so CI runs them on every PR: a change that let the
harness reach a public host, follow a redirect off the machine or carry credentials in a URL
fails the suite.
"""

import importlib.util
import pathlib
import sys

_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "redteam"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"redteam_{name}", _DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# test_guard imports `guard` by its plain name, as it does when run from its own directory.
sys.modules.setdefault("guard", _load("guard"))
_tests = _load("test_guard")

CheckLocalTests = _tests.CheckLocalTests
RedirectTests = _tests.RedirectTests
