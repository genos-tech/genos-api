#!/usr/bin/env python3
"""A hash of what the in-app agent's MODEL is shown, with no model, no network and no database (MIP-1101).

`agent-evals` bills a Vertex run. Whether a PR can move its numbers is not a question about
directories (an MCP-only tool, a refactor of a tool's lookup or a log line live under
`origin/search_engine/` and cannot change a word the model reads): it is a question about what the
model sees. This prints one sha256 of exactly that, so the workflow can run it on the PR merged into
its base and on the base and skip the billed steps when the two are equal.

    python scripts/agent_surface_hash.py [--repo DIR] [--quiet]

prints one `<component> <sha256>` line per component, then `surface-hash: <sha256>` as the last line.
Exit 0 with a hash; exit 2 when it could not be computed (the workflow then FAILS OPEN and runs the
evals: a bug here must never silently skip a real change).

What is hashed. Nothing here is written again: the first three parts are the lines of the
`agent_fingerprint` command (`--no-results`, so no database), which builds the real thing by running
the real controller loop against a recording adapter.

  fingerprint.shipped.*    the tool declarations exactly as each provider adapter (Gemini, Claude,
                           OpenAI) is handed them, at full and read agency, and the system prompt
                           plus first user turn `run_agent` builds (plain, Japanese, @mentions, the
                           three agency levels), and the thread / note ask templates; with every
                           flag as it SHIPS (a default-ON flag is on).
  fingerprint.flags-off.*  the same with every boolean `AGENT_*` flag off (the state an operator gets
                           by switching flags off, and the one the eval harness' arm 0 measures).
  registry.directory       every in-app tool: name, whether it needs approval, its category; plus
                           the flag-gated tools whose flag is on as shipped. The names the prompt's
                           tool directory is generated from.
  settings.model_visible   the `SEARCH_ENGINE` knobs that steer the model (model ids, step caps,
                           retrieval knobs ...) that hold a value, and `AGENT_MAX_BODY_BYTES`.
                           Entries holding an "off" value (False, 0, empty) are left out, so a
                           NEW flag that ships off changes nothing, and switching one on does.

What is NOT hashed (on purpose, and the reason a PR that only does these starts no billed run):
the code inside a tool's `run`, MCP-only tools (they are in another registry the in-app model
cannot reach), logging, the ACL helpers, flag-gated text while its flag is off, settings that are
not model knobs (middleware, quotas, credentials, hosts). TOOL RESULT text on a fixture is a model
surface too: `agent_fingerprint` fingerprints it (`result.*`), but that needs a database, so this
hash leaves it out. The workflow still runs the evals whenever one of the files that shape what
retrieval returns or what the loop sends changes (see `FORCE_PATHS` in `agent_eval_gate.py`).

The script runs from ANY checkout against ANOTHER tree (`--repo`), so CI can hash the base tree with
the head's copy of this file (the base does not have it yet when this lands).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
from pathlib import Path

# SEARCH_ENGINE keys that are NOT what the model sees: credentials, hosts, batch sizes, money. Every
# other key is hashed, so a knob nobody thought of fails towards running the evals.
_NOT_MODEL_VISIBLE = re.compile(
    r"^(OPENSEARCH_|AI_|TIER_QUOTAS$|MODEL_QUOTA_FALLBACK$|MODEL_CATALOG$|BULK_BATCH_SIZE$"
    r"|EMBEDDING_BATCH_SIZE$|RAG_BULK_REFRESH$|LLM_LOG_USAGE_METADATA$|RAG_JUDGE_)"
    r"|(_API_KEY|_PASSWORD|_CA_CERT|_USERNAME|_SERVICE_ACCOUNT_FILE|_BASE_URL)$"
    r"|^GEMINI_(PROJECT|LOCATION|USE_VERTEX)$"
)

# A value that means "this is off / unset": left out of the settings component.
_OFF = (False, None, 0, 0.0, "", (), frozenset())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canon(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=_jsonable)


def _jsonable(value: object) -> object:
    if isinstance(value, (set, frozenset)):
        return sorted(str(v) for v in value)
    return str(value)


def _fingerprint_lines() -> dict[str, str]:
    """`name -> sha256` of every `agent_fingerprint --no-results` line (the ALL line is dropped)."""
    from django.core.management import call_command  # noqa: PLC0415

    out = io.StringIO()
    call_command("agent_fingerprint", "--no-results", stdout=out)
    lines: dict[str, str] = {}
    for line in out.getvalue().strip().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] != "ALL" and re.fullmatch(r"[0-9a-f]{64}", parts[1]):
            lines[parts[0]] = parts[1]
    if not lines:
        raise RuntimeError("agent_fingerprint printed no surface lines")
    return lines


def _registry_directory() -> str:
    from origin.search_engine.agent.tools import REGISTRY, flagged_tools  # noqa: PLC0415
    from origin.search_engine.agent.tools.categories import TOOL_CATEGORY  # noqa: PLC0415

    rows = [
        [name, bool(tool.requires_approval), TOOL_CATEGORY.get(name, "")]
        for name, tool in REGISTRY.items()
    ]
    rows.sort()
    flagged = sorted([tool.name, bool(tool.requires_approval)] for tool in flagged_tools())
    return _canon({"registry": rows, "flagged_on": flagged})


def _model_visible_settings() -> str:
    from django.conf import settings  # noqa: PLC0415

    knobs = {
        key: value
        for key, value in settings.SEARCH_ENGINE.items()
        if not _NOT_MODEL_VISIBLE.search(key) and value not in _OFF
    }
    body = getattr(settings, "AGENT_MAX_BODY_BYTES", None)
    return _canon({"SEARCH_ENGINE": knobs, "AGENT_MAX_BODY_BYTES": body})


def compute_components() -> dict[str, str]:
    """Every component of the hash, `name -> sha256`. Django must be set up."""
    from django.conf import settings  # noqa: PLC0415
    from django.test.utils import override_settings  # noqa: PLC0415

    components: dict[str, str] = {}
    for name, sha in _fingerprint_lines().items():
        components[f"fingerprint.shipped.{name}"] = sha

    flags_off = {
        key: False
        for key, value in settings.SEARCH_ENGINE.items()
        if key.startswith("AGENT_") and isinstance(value, bool)
    }
    with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **flags_off}):
        for name, sha in _fingerprint_lines().items():
            components[f"fingerprint.flags-off.{name}"] = sha

    components["registry.directory"] = _sha(_registry_directory())
    components["settings.model_visible"] = _sha(_model_visible_settings())
    return components


def combine(components: dict[str, str]) -> str:
    """One sha256 over the sorted `name sha` lines: a change in any component moves it."""
    return _sha("".join(f"{name} {sha}\n" for name, sha in sorted(components.items())))


def _setup_django(repo: Path) -> None:
    os.chdir(repo)
    sys.path.insert(0, str(repo))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apis.settings_test")
    import django  # noqa: PLC0415

    django.setup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--repo", default=".", help="the tree to hash (default: the current directory)"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="print only the final surface-hash line"
    )
    args = parser.parse_args(argv)
    try:
        _setup_django(Path(args.repo).resolve())
        components = compute_components()
    except Exception as exc:  # noqa: BLE001 - ANY failure must read as "could not compute"
        print(
            f"agent_surface_hash: could not compute: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return 2
    if not args.quiet:
        for name, sha in sorted(components.items()):
            print(f"{name} {sha}")
    print(f"surface-hash: {combine(components)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
