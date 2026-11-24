"""Drift guard for the landing / developers page tool list (MIP-2335).

genos-frontend `src/lp/mcpTools.tsx` (the /developers tool table and the home tool count) is a
hand-kept list. A copy only changes when someone remembers, and it drifted twice (14 -> 39 in
MIP-702, 85 vs 110 in MIP-2309). This test pins `tools.CATALOG` to a small checked-in JSON snapshot
(`fixtures/mcp_catalog_snapshot.json`: name, read/write/destructive, and the switch that gates the
tool) and fails, naming the tools, when the two differ.

When it fails on purpose (a tool was added, removed or re-classified):
  1. regenerate:  MCP_CATALOG_SNAPSHOT_WRITE=1 python manage.py test origin.tests.test_mcp_catalog_snapshot
  2. copy the file into genos-frontend's test fixture (the same JSON, so one change updates both)
  3. update `src/lp/mcpTools.tsx` to match.
A tool with a `switch` (a Django setting such as `HTML_NOTES` / `GENOS_SKILLS`, or a SEARCH_ENGINE
flag such as `AGENT_PARITY_TOOLS`) exists only while that switch is on: the page holds it back
until then.

No model-visible change: this reads the catalog, it never alters it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from django.test import SimpleTestCase

from origin.views.public.mcp import tools

SNAPSHOT = Path(__file__).parent / "fixtures" / "mcp_catalog_snapshot.json"
HOWTO = (
    "Regenerate with MCP_CATALOG_SNAPSHOT_WRITE=1 python manage.py test "
    "origin.tests.test_mcp_catalog_snapshot, copy the file into genos-frontend's test fixture and "
    "update src/lp/mcpTools.tsx (MIP-2335)."
)


def current_snapshot() -> list[dict]:
    return [
        {
            "name": t.name,
            "write": bool(t.write),
            "destructive": bool(t.destructive),
            "switch": t.setting or t.flag or "",
        }
        for t in tools.CATALOG
    ]


def render(rows: list[dict]) -> str:
    return json.dumps(rows, indent=2, ensure_ascii=False) + "\n"


class TestMcpCatalogSnapshot(SimpleTestCase):
    def test_catalog_matches_the_checked_in_snapshot(self):
        rows = current_snapshot()
        if os.environ.get("MCP_CATALOG_SNAPSHOT_WRITE") == "1":
            SNAPSHOT.write_text(render(rows), encoding="utf-8")
        pinned = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        now = {r["name"]: r for r in rows}
        was = {r["name"]: r for r in pinned}
        added = sorted(set(now) - set(was))
        removed = sorted(set(was) - set(now))
        changed = sorted(n for n in set(now) & set(was) if now[n] != was[n])
        problems = []
        if added:
            problems.append(f"added to CATALOG, not in the snapshot: {', '.join(added)}")
        if removed:
            problems.append(f"in the snapshot, gone from CATALOG: {', '.join(removed)}")
        if changed:
            problems.append(
                "read/write/destructive/switch changed: "
                + "; ".join(f"{n} ({was[n]} -> {now[n]})" for n in changed)
            )
        if not problems and [r["name"] for r in rows] != [r["name"] for r in pinned]:
            problems.append("same tools, different order")
        self.assertFalse(problems, "\n".join(problems) + "\n" + HOWTO)

    def test_snapshot_file_is_what_the_generator_writes(self):
        self.assertEqual(SNAPSHOT.read_text(encoding="utf-8"), render(current_snapshot()), HOWTO)

    def test_names_are_unique_and_switches_are_known(self):
        rows = current_snapshot()
        names = [r["name"] for r in rows]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(
            {r["switch"] for r in rows} - {""}
            <= {"HTML_NOTES", "GENOS_SKILLS", "AGENT_PARITY_TOOLS"}
        )
