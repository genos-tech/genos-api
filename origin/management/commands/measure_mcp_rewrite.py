"""`python manage.py measure_mcp_rewrite`: A/B of the MCP `search_workspace` query rewrite (MIP-1378).

Question: should MCP skip the LLM query rewrite by default? For each query in
`origin/management/measure/mcp_rewrite_queries.json` this runs the SAME code the MCP tool runs
(`search_knowledge_base._run` with `ToolContext(surface="mcp")`: same flags, overlays, for_agent,
reranker) twice, once with the rewrite ON (the server default) and once with `rewrite: false`,
against the eval fixture (`agent_eval_setup`), and writes one JSON file:

    per query and arm: top-10 entity ids + titles, rank of each expected title, latency,
    model calls, tokens and cost (from the in-process usage capture), and a summary per arm
    (recall@10, MRR, hit@1/3, mean/median latency, total cost; for the cross-language subset too).

`--repeat N` runs each arm N times (retrieval is flaky run to run; the rewrite varies). Arm order
alternates by repeat so a warm cache does not favour one side.

`--stub-models` makes NO model call (the rewrite returns the query unchanged, embeddings are a
deterministic hash vector, the reranker is off): it only proves the plumbing. A real run needs the
Vertex credentials. Lives outside RETRIEVAL_PATHS on purpose: it must not start the billed CI job.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.test import override_settings

DEFAULT_QUERIES = Path(__file__).resolve().parent.parent / "measure" / "mcp_rewrite_queries.json"


class _HashEmbedder:
    """Stub embedder: a deterministic vector per text, no network.

    Name and dimensions come from the real configured adapter (built without any call), so the
    index mapping and the stored `embedding_model` stay what a real run would have.
    """

    def __init__(self, real: Any) -> None:
        self.model_name = real.model_name
        self.dimensions = real.dimensions

    def embed(self, texts: list[str], task_type: Any = None) -> list[list[float]]:
        out = []
        for t in texts:
            seed = hashlib.sha256(t.encode("utf-8")).digest()
            vec = [(seed[i % len(seed)] - 127.5) / 127.5 for i in range(self.dimensions)]
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out


def _rank(titles: list[str], needle: str) -> int | None:
    needle = needle.lower()
    for i, t in enumerate(titles):
        if needle in (t or "").lower():
            return i + 1
    return None


def _score(titles: list[str], expected: list[str]) -> dict[str, Any]:
    per = {s: _rank(titles, s) for s in expected}
    hits = [r for r in per.values() if r]
    best = min(hits) if hits else None
    return {
        "ranks": per,
        "best_rank": best,
        "hit_at_10": best is not None,
        "mrr": round(1.0 / best, 4) if best else 0.0,
    }


class Command(BaseCommand):
    help = "A/B the MCP search_workspace query rewrite on the eval fixture (MIP-1378)."

    def add_arguments(self, parser):
        parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
        parser.add_argument("--out", default="mcp_rewrite_result.json")
        parser.add_argument("--repeat", type=int, default=1)
        parser.add_argument("--limit", type=int, default=0, help="only the first N queries")
        parser.add_argument("--stub-models", action="store_true", help="no model call (plumbing)")
        parser.add_argument("--reseed", action="store_true", help="rebuild the eval fixture first")

    def handle(self, *args, **opts):
        from origin.search_engine.agent.evals.fixture import (  # noqa: PLC0415
            ensure_fixture,
            reseed_fixture,
        )

        data = json.loads(Path(opts["queries"]).read_text(encoding="utf-8"))
        queries = data["queries"][: opts["limit"] or None]
        if not queries:
            raise CommandError("no queries")

        with ExitStack() as stack:
            if opts["stub_models"]:
                from origin.search_engine import (
                    embeddings,  # noqa: PLC0415
                    query_rewriter,  # noqa: PLC0415
                )

                real_get = embeddings._get_embedder
                stack.enter_context(
                    mock.patch.object(
                        embeddings, "_get_embedder", lambda: _HashEmbedder(real_get())
                    )
                )
                stack.enter_context(
                    mock.patch.object(query_rewriter, "rewrite_query", lambda query, **kw: [query])
                )
            overrides = {
                **settings.SEARCH_ENGINE,
                "RAG_USE_QUERY_REWRITE": True,
                **({"RAG_USE_RERANKER": False} if opts["stub_models"] else {}),
            }
            stack.enter_context(override_settings(SEARCH_ENGINE=overrides))

            fixture = reseed_fixture() if opts["reseed"] else ensure_fixture()
            rows = self._measure(queries, fixture, opts["repeat"])

        summary = {arm: self._summary(rows, arm) for arm in ("rewrite_on", "rewrite_off")}
        summary["cross_language"] = {
            arm: self._summary([r for r in rows if r["cross_language"]], arm)
            for arm in ("rewrite_on", "rewrite_off")
        }
        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stub_models": opts["stub_models"],
            "repeat": opts["repeat"],
            "team_id": fixture["team_id"],
            "summary": summary,
            "queries": rows,
        }
        out = Path(opts["out"]).resolve()
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
        self.stdout.write(self.style.SUCCESS(f"result written to {out}"))

    def _measure(self, queries, fixture, repeat):
        from origin.search_engine.agent.evals.group import usage as group_usage  # noqa: PLC0415
        from origin.search_engine.agent.tools.base import ToolContext  # noqa: PLC0415
        from origin.search_engine.agent.tools.search_kb import _run  # noqa: PLC0415

        ctx = ToolContext(team_id=fixture["team_id"], user_id=fixture["user_id"], surface="mcp")
        rows = []
        for item in queries:
            row = {
                "id": item["id"],
                "query": item["query"],
                "language": item.get("language", ""),
                "cross_language": bool(item.get("cross_language")),
                "expected": item["expected_title_substrings"],
                "runs": {"rewrite_on": [], "rewrite_off": []},
            }
            for rep in range(repeat):
                arms = ["rewrite_on", "rewrite_off"]
                if rep % 2:
                    arms.reverse()
                for arm in arms:
                    args = {"query": item["query"], "limit": 10}
                    if arm == "rewrite_off":
                        args["rewrite"] = False
                    with group_usage.capture_usage() as cap:
                        started = time.monotonic()
                        try:
                            res = _run(args, ctx)
                            error = None
                        except Exception as e:  # noqa: BLE001
                            res, error = {"matches": []}, repr(e)
                        latency_ms = int((time.monotonic() - started) * 1000)
                    matches = (res.get("matches") or [])[:10]
                    titles = [m.get("title") or "" for m in matches]
                    row["runs"][arm].append(
                        {
                            "top10_entity_ids": [m.get("entity_id") for m in matches],
                            "top10_titles": titles,
                            "latency_ms": latency_ms,
                            "llm_calls": cap.calls,
                            "tokens": cap.input_tokens + cap.output_tokens,
                            "cost_usd": round(cap.cost_usd, 6),
                            "cost_jpy_milli": cap.cost_jpy_milli,
                            "error": error,
                            **_score(titles, item["expected_title_substrings"]),
                        }
                    )
            rows.append(row)
            self.stdout.write(f"  {item['id']}")
        return rows

    @staticmethod
    def _summary(rows, arm):
        runs = [r for row in rows for r in row["runs"][arm]]
        if not runs:
            return {}
        lat = [r["latency_ms"] for r in runs]
        n = len(runs)
        return {
            "runs": n,
            "recall_at_10": round(sum(r["hit_at_10"] for r in runs) / n, 4),
            "hit_at_1": round(sum(r["best_rank"] == 1 for r in runs) / n, 4),
            "hit_at_3": round(
                sum(bool(r["best_rank"] and r["best_rank"] <= 3) for r in runs) / n, 4
            ),
            "mrr": round(sum(r["mrr"] for r in runs) / n, 4),
            "latency_ms_mean": int(statistics.mean(lat)),
            "latency_ms_median": int(statistics.median(lat)),
            "llm_calls_total": sum(r["llm_calls"] for r in runs),
            "cost_usd_total": round(sum(r["cost_usd"] for r in runs), 6),
            "errors": sum(1 for r in runs if r["error"]),
        }
