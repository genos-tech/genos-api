"""MIP-1478: the closed-ancestor rule's pieces sit where the layers can import them downward.

The in-app `ancestor_rule` (a tool module) used to import the MCP view layer's private `_fields` and
the service's private `_COLUMNS`: tools depending on views. The one shared reader of "what does this
task read as, and who may see the ancestor that decided it" is now `services.task_ancestry`'s
`effective_fields`, and the MCP augment and the in-app tools both call it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from origin.search_engine.agent.tools import ToolContext, ancestor_rule
from origin.services import task_ancestry
from origin.tests.test_task_ancestry import AncestryBase

TOOLS = Path(ancestor_rule.__file__)


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
    return found


class LayeringTests(AncestryBase):
    def test_the_in_app_rule_does_not_import_the_mcp_view_layer(self):
        self.assertEqual([m for m in _imports(TOOLS) if m.startswith("origin.views")], [])

    def test_the_in_app_rule_uses_no_private_name_of_the_service(self):
        private = [m for m in _imports(TOOLS) if re.match(r"_[^_]", m.rsplit(".", 1)[-1])]
        self.assertEqual(private, [])
        self.assertTrue(hasattr(task_ancestry, "COLUMNS"))
        self.assertFalse(hasattr(task_ancestry, "_COLUMNS"))

    def test_the_service_answers_what_the_mcp_augment_answers(self):
        from origin.views.public.mcp import ancestry  # noqa: PLC0415

        parent = self.make("top", status="Closed")
        child = self.make("leaf", parent=parent, status="WIP")
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        ids = [parent.task_id, child.task_id]
        fields = task_ancestry.effective_fields(ids, ctx)
        self.assertEqual(fields[child.task_id]["effective_status"], "Closed")
        self.assertEqual(fields[child.task_id]["closed_by_ancestor"]["task_id"], parent.task_id)
        self.assertEqual(fields, ancestor_rule.row_fields(ids, ctx))
        row = {"task_id": child.task_id}
        self.assertEqual(
            ancestry.with_task_ancestry(row, {}, {}, ctx),
            {**row, **fields[child.task_id]},
        )
