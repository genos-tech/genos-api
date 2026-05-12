#!/usr/bin/env python3
"""
Generate a modern PostgreSQL table relation network as a standalone HTML file.

What it does:
- Connects to PostgreSQL using psycopg v3.
- Introspects tables, columns, primary keys, and foreign keys from pg_catalog.
- Writes one HTML file containing the graph data and a browser-side filter UI.
- No table data is exported; only schema metadata is embedded.

Install:
    pip install "psycopg[binary]"

Run:
    export PG_DSN="postgresql://user:password@host:5432/database"
    python pg_relation_network.py --output pg_relation_network.html

Then open pg_relation_network.html in a browser.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

FREEZE_LAYOUT_VERSION = "2026-05-11-freeze-layout-v2"


ACTION_MAP = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}

MATCH_MAP = {
    "s": "SIMPLE",
    "f": "FULL",
    "p": "PARTIAL",
}


def parse_csvish(values: Optional[Iterable[str]]) -> List[str]:
    """Allow repeated args and comma-separated lists."""
    out: List[str] = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def compile_regex(expr: Optional[str], label: str) -> Optional[re.Pattern[str]]:
    if not expr:
        return None
    try:
        return re.compile(expr, re.IGNORECASE)
    except re.error as exc:
        raise SystemExit(f"Invalid {label} regex {expr!r}: {exc}") from exc


def schema_predicate(alias: str, include_system_schemas: bool) -> str:
    if include_system_schemas:
        return ""
    return (
        f"AND {alias}.nspname <> 'information_schema'\n" f"AND {alias}.nspname NOT LIKE 'pg_%%'\n"
    )


def fetch_all(
    conn: Any, sql: str, params: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    with conn.cursor() as cur:
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def table_sql(include_system_schemas: bool) -> str:
    return f"""
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    c.oid::text AS table_oid,
    CASE c.relkind
        WHEN 'r' THEN 'table'
        WHEN 'p' THEN 'partitioned_table'
        WHEN 'f' THEN 'foreign_table'
        ELSE c.relkind::text
    END AS table_type,
    obj_description(c.oid, 'pg_class') AS comment
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'f')
{schema_predicate('n', include_system_schemas)}
ORDER BY n.nspname, c.relname;
"""


def column_sql(include_system_schemas: bool) -> str:
    return f"""
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    a.attnum AS ordinal_position,
    a.attname AS column_name,
    pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
    a.attnotnull AS not_null,
    col_description(c.oid, a.attnum) AS comment
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p', 'f')
{schema_predicate('n', include_system_schemas)}
ORDER BY n.nspname, c.relname, a.attnum;
"""


def pk_sql(include_system_schemas: bool) -> str:
    return f"""
SELECT
    n.nspname AS schema_name,
    c.relname AS table_name,
    con.oid::text AS constraint_oid,
    con.conname AS constraint_name,
    array_agg(a.attname ORDER BY k.ord) AS columns,
    pg_get_constraintdef(con.oid, true) AS definition
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE con.contype = 'p'
  AND c.relkind IN ('r', 'p', 'f')
{schema_predicate('n', include_system_schemas)}
GROUP BY n.nspname, c.relname, con.oid, con.conname
ORDER BY n.nspname, c.relname, con.conname;
"""


def fk_sql(include_system_schemas: bool) -> str:
    return f"""
SELECT
    con.oid::text AS constraint_oid,
    con.conname AS constraint_name,

    src_ns.nspname AS source_schema,
    src.relname AS source_table,
    dst_ns.nspname AS target_schema,
    dst.relname AS target_table,

    array_agg(src_att.attname ORDER BY src_cols.ord) AS source_columns,
    array_agg(dst_att.attname ORDER BY src_cols.ord) AS target_columns,

    con.confupdtype AS on_update_code,
    con.confdeltype AS on_delete_code,
    con.confmatchtype AS match_type_code,
    con.condeferrable AS deferrable,
    con.condeferred AS initially_deferred,
    pg_get_constraintdef(con.oid, true) AS definition
FROM pg_constraint con
JOIN pg_class src ON src.oid = con.conrelid
JOIN pg_namespace src_ns ON src_ns.oid = src.relnamespace
JOIN pg_class dst ON dst.oid = con.confrelid
JOIN pg_namespace dst_ns ON dst_ns.oid = dst.relnamespace
JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS src_cols(attnum, ord) ON true
JOIN LATERAL unnest(con.confkey) WITH ORDINALITY AS dst_cols(attnum, ord) ON dst_cols.ord = src_cols.ord
JOIN pg_attribute src_att ON src_att.attrelid = src.oid AND src_att.attnum = src_cols.attnum
JOIN pg_attribute dst_att ON dst_att.attrelid = dst.oid AND dst_att.attnum = dst_cols.attnum
WHERE con.contype = 'f'
  AND src.relkind IN ('r', 'p', 'f')
  AND dst.relkind IN ('r', 'p', 'f')
{schema_predicate('src_ns', include_system_schemas)}
{schema_predicate('dst_ns', include_system_schemas)}
GROUP BY
    con.oid,
    con.conname,
    src_ns.nspname,
    src.relname,
    dst_ns.nspname,
    dst.relname,
    con.confupdtype,
    con.confdeltype,
    con.confmatchtype,
    con.condeferrable,
    con.condeferred
ORDER BY src_ns.nspname, src.relname, con.conname;
"""


def metadata_sql() -> str:
    return """
SELECT
    current_database() AS database_name,
    current_user AS introspected_as,
    current_setting('server_version') AS server_version;
"""


def qualify(schema_name: str, table_name: str) -> str:
    return f"{schema_name}.{table_name}"


def passes_generation_filters(
    row: Dict[str, Any],
    include_schemas: set[str],
    exclude_schemas: set[str],
    table_re: Optional[re.Pattern[str]],
    exclude_table_re: Optional[re.Pattern[str]],
) -> bool:
    schema_name = str(row["schema_name"])
    table_name = str(row["table_name"])
    qualified = qualify(schema_name, table_name)

    if include_schemas and schema_name not in include_schemas:
        return False

    if schema_name in exclude_schemas:
        return False

    if table_re and not (table_re.search(qualified) or table_re.search(table_name)):
        return False

    if exclude_table_re and (
        exclude_table_re.search(qualified) or exclude_table_re.search(table_name)
    ):
        return False

    return True


def build_graph(
    tables: List[Dict[str, Any]],
    columns: List[Dict[str, Any]],
    pks: List[Dict[str, Any]],
    fks: List[Dict[str, Any]],
    include_schemas: set[str],
    exclude_schemas: set[str],
    table_re: Optional[re.Pattern[str]],
    exclude_table_re: Optional[re.Pattern[str]],
) -> Dict[str, Any]:
    nodes_by_id: Dict[str, Dict[str, Any]] = {}

    for table in tables:
        if not passes_generation_filters(
            table, include_schemas, exclude_schemas, table_re, exclude_table_re
        ):
            continue

        node_id = qualify(table["schema_name"], table["table_name"])
        nodes_by_id[node_id] = {
            "id": node_id,
            "schema": table["schema_name"],
            "table": table["table_name"],
            "qualified_name": node_id,
            "type": table.get("table_type") or "table",
            "comment": table.get("comment"),
            "columns": [],
            "primary_key": None,
        }

    for column in columns:
        node_id = qualify(column["schema_name"], column["table_name"])
        node = nodes_by_id.get(node_id)
        if not node:
            continue

        node["columns"].append(
            {
                "name": column["column_name"],
                "type": column["data_type"],
                "not_null": bool(column["not_null"]),
                "comment": column.get("comment"),
                "is_pk": False,
            }
        )

    for pk in pks:
        node_id = qualify(pk["schema_name"], pk["table_name"])
        node = nodes_by_id.get(node_id)
        if not node:
            continue

        pk_columns = list(pk.get("columns") or [])
        node["primary_key"] = {
            "name": pk["constraint_name"],
            "columns": pk_columns,
            "definition": pk.get("definition"),
        }

        pk_set = set(pk_columns)
        for column in node["columns"]:
            column["is_pk"] = column["name"] in pk_set

    edges: List[Dict[str, Any]] = []
    for fk in fks:
        source_id = qualify(fk["source_schema"], fk["source_table"])
        target_id = qualify(fk["target_schema"], fk["target_table"])

        if source_id not in nodes_by_id or target_id not in nodes_by_id:
            continue

        edges.append(
            {
                "id": fk["constraint_oid"],
                "constraint_name": fk["constraint_name"],
                "from": source_id,
                "to": target_id,
                "source_schema": fk["source_schema"],
                "source_table": fk["source_table"],
                "target_schema": fk["target_schema"],
                "target_table": fk["target_table"],
                "source_columns": list(fk.get("source_columns") or []),
                "target_columns": list(fk.get("target_columns") or []),
                "on_update": ACTION_MAP.get(fk.get("on_update_code"), fk.get("on_update_code")),
                "on_delete": ACTION_MAP.get(fk.get("on_delete_code"), fk.get("on_delete_code")),
                "match_type": MATCH_MAP.get(fk.get("match_type_code"), fk.get("match_type_code")),
                "deferrable": bool(fk.get("deferrable")),
                "initially_deferred": bool(fk.get("initially_deferred")),
                "definition": fk.get("definition"),
            }
        )

    nodes = list(nodes_by_id.values())
    nodes.sort(key=lambda n: (str(n["schema"]), str(n["table"])))
    edges.sort(key=lambda e: (str(e["from"]), str(e["to"]), str(e["constraint_name"])))

    return {
        "nodes": nodes,
        "edges": edges,
        "schemas": sorted({node["schema"] for node in nodes}),
        "stats": {
            "table_count": len(nodes),
            "fk_count": len(edges),
            "pk_count": sum(1 for node in nodes if node.get("primary_key")),
            "schema_count": len({node["schema"] for node in nodes}),
        },
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>PostgreSQL Table Relation Network - Freeze Layout v2</title>
  <link rel="preconnect" href="https://unpkg.com" />
  <link rel="stylesheet" href="https://unpkg.com/vis-network@9.1.9/styles/vis-network.min.css" />
  <style>
    :root {
      --bg: #0f172a;
      --panel: #111827;
      --panel-2: #0b1220;
      --card: #ffffff;
      --muted: #64748b;
      --text: #0f172a;
      --soft: #f8fafc;
      --border: #e2e8f0;
      --brand: #2563eb;
      --brand-dark: #1d4ed8;
      --good: #059669;
      --warn: #d97706;
      --danger: #dc2626;
      --shadow: 0 18px 45px rgba(15, 23, 42, 0.12);
      --radius: 18px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      --sans: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      font-family: var(--sans);
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(37, 99, 235, 0.18), transparent 30rem),
        radial-gradient(circle at bottom right, rgba(14, 165, 233, 0.14), transparent 30rem),
        #f1f5f9;
      min-height: 100vh;
    }

    header {
      height: 76px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      padding: 16px 22px;
      color: #fff;
      background: linear-gradient(135deg, #0f172a, #1e293b);
      box-shadow: 0 12px 28px rgba(15, 23, 42, 0.22);
    }

    header h1 {
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      letter-spacing: -0.02em;
    }

    header .sub {
      margin-top: 4px;
      color: #cbd5e1;
      font-size: 12px;
    }

    .header-stats {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .stat-pill {
      background: rgba(255,255,255,0.09);
      border: 1px solid rgba(255,255,255,0.14);
      color: #e2e8f0;
      border-radius: 999px;
      padding: 7px 10px;
      font-size: 12px;
      white-space: nowrap;
    }

    .layout {
      display: grid;
      grid-template-columns: 360px minmax(360px, 1fr) 390px;
      gap: 16px;
      padding: 16px;
      height: calc(100vh - 76px);
    }

    .panel {
      background: rgba(255,255,255,0.92);
      border: 1px solid rgba(226, 232, 240, 0.92);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
      min-height: 0;
    }

    .sidebar, .inspector {
      display: flex;
      flex-direction: column;
      min-height: 0;
    }

    .panel-title {
      padding: 15px 16px;
      border-bottom: 1px solid var(--border);
      background: rgba(248, 250, 252, 0.78);
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }

    .panel-body {
      padding: 14px 16px 18px;
      overflow: auto;
    }

    .field { margin-bottom: 14px; }

    label.field-label {
      display: block;
      font-size: 12px;
      font-weight: 700;
      color: #334155;
      margin-bottom: 6px;
    }

    input[type="text"],
    input[type="number"],
    textarea,
    select {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 11px;
      font: inherit;
      font-size: 13px;
      background: #fff;
      color: var(--text);
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }

    textarea {
      min-height: 68px;
      resize: vertical;
      font-family: var(--mono);
      line-height: 1.45;
    }

    input:focus,
    textarea:focus,
    select:focus {
      border-color: #93c5fd;
      box-shadow: 0 0 0 4px rgba(147,197,253,0.25);
    }

    .two-col {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }

    .check {
      display: flex;
      align-items: flex-start;
      gap: 8px;
      font-size: 13px;
      color: #334155;
      line-height: 1.35;
      margin: 8px 0;
    }

    .check input { margin-top: 2px; }

    .hint {
      font-size: 11px;
      color: var(--muted);
      margin-top: 5px;
      line-height: 1.35;
    }

    .schema-toolbar {
      display: flex;
      gap: 8px;
      margin-bottom: 8px;
    }

    button {
      border: 0;
      border-radius: 12px;
      background: #e2e8f0;
      color: #0f172a;
      padding: 9px 11px;
      font-weight: 700;
      font-size: 12px;
      cursor: pointer;
      transition: transform 0.1s, background 0.15s, box-shadow 0.15s;
    }

    button:hover {
      transform: translateY(-1px);
      box-shadow: 0 8px 18px rgba(15,23,42,0.12);
    }

    button.primary {
      background: var(--brand);
      color: #fff;
    }

    button.primary:hover { background: var(--brand-dark); }

    button.ghost {
      background: #f8fafc;
      border: 1px solid var(--border);
      color: #334155;
      box-shadow: none;
    }

    .button-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 9px;
      margin-top: 12px;
    }

    .schema-list {
      display: flex;
      flex-wrap: wrap;
      gap: 7px;
      max-height: 140px;
      overflow: auto;
      padding: 8px;
      border: 1px solid var(--border);
      border-radius: 14px;
      background: #f8fafc;
    }

    .schema-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      background: #fff;
      border: 1px solid var(--border);
      padding: 6px 9px;
      font-size: 12px;
      cursor: pointer;
      user-select: none;
    }

    .schema-pill input { margin: 0; }

    .graph-panel {
      display: flex;
      flex-direction: column;
      min-width: 0;
      min-height: 0;
      position: relative;
    }

    .graph-topbar {
      min-height: 54px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--border);
      background: rgba(248,250,252,0.86);
    }

    .status {
      font-size: 13px;
      color: #334155;
      font-weight: 700;
    }

    .status small {
      display: block;
      font-weight: 500;
      color: var(--muted);
      margin-top: 2px;
    }

    .graph-actions {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
    }

    #network {
      flex: 1;
      min-height: 0;
      background:
        linear-gradient(rgba(15,23,42,0.025) 1px, transparent 1px),
        linear-gradient(90deg, rgba(15,23,42,0.025) 1px, transparent 1px),
        #ffffff;
      background-size: 28px 28px;
    }

    .warning {
      display: none;
      margin: 0 14px 12px;
      border: 1px solid #fed7aa;
      background: #fffbeb;
      color: #92400e;
      padding: 10px 12px;
      border-radius: 12px;
      font-size: 12px;
      line-height: 1.4;
    }

    .warning.show { display: block; }

    .empty {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      background: #f8fafc;
      border: 1px dashed #cbd5e1;
      padding: 14px;
      border-radius: 14px;
    }

    .detail-section {
      margin-bottom: 16px;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border);
    }

    .detail-section:last-child {
      border-bottom: 0;
      margin-bottom: 0;
      padding-bottom: 0;
    }

    .detail-section h3 {
      font-size: 14px;
      margin: 0 0 8px;
      letter-spacing: -0.01em;
    }

    .mono {
      font-family: var(--mono);
      font-size: 12px;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 7px;
      background: #eff6ff;
      color: #1d4ed8;
      font-size: 11px;
      font-weight: 800;
      margin: 2px 4px 2px 0;
    }

    .badge.green {
      background: #ecfdf5;
      color: #047857;
    }

    .badge.gray {
      background: #f1f5f9;
      color: #475569;
    }

    .badge.orange {
      background: #fff7ed;
      color: #c2410c;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      overflow: hidden;
      border-radius: 12px;
    }

    th, td {
      text-align: left;
      padding: 8px 7px;
      border-bottom: 1px solid #e2e8f0;
      vertical-align: top;
    }

    th {
      color: #475569;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      background: #f8fafc;
    }

    tr:last-child td { border-bottom: 0; }

    .fk-card {
      border: 1px solid var(--border);
      background: #fff;
      border-radius: 13px;
      padding: 10px;
      margin: 8px 0;
    }

    .fk-card .name {
      font-weight: 800;
      font-size: 12px;
      margin-bottom: 5px;
    }

    .fk-card .line {
      font-family: var(--mono);
      font-size: 11px;
      color: #334155;
      overflow-wrap: anywhere;
    }

    .visible-list {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      max-height: 120px;
      overflow: auto;
      padding: 8px;
      background: #f8fafc;
      border: 1px solid var(--border);
      border-radius: 13px;
    }

    .table-chip {
      border-radius: 999px;
      background: #fff;
      border: 1px solid var(--border);
      color: #334155;
      padding: 5px 8px;
      font-size: 11px;
      font-family: var(--mono);
      cursor: pointer;
    }

    .table-chip:hover {
      border-color: #93c5fd;
      background: #eff6ff;
    }

    .toast {
      position: fixed;
      right: 18px;
      bottom: 18px;
      z-index: 20;
      background: #0f172a;
      color: #fff;
      padding: 11px 13px;
      border-radius: 12px;
      box-shadow: var(--shadow);
      font-size: 13px;
      opacity: 0;
      transform: translateY(10px);
      pointer-events: none;
      transition: 0.18s ease;
    }

    .toast.show {
      opacity: 1;
      transform: translateY(0);
    }

    @media (max-width: 1180px) {
      .layout {
        grid-template-columns: 330px minmax(320px, 1fr);
      }
      .inspector {
        display: none;
      }
    }

    @media (max-width: 820px) {
      header {
        height: auto;
        align-items: flex-start;
        flex-direction: column;
      }
      .layout {
        height: auto;
        grid-template-columns: 1fr;
      }
      #network {
        height: 65vh;
      }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>PostgreSQL Table Relation Network <span style="font-size:12px;color:#93c5fd;">Freeze Layout v2</span></h1>
      <div class="sub" id="subtitle">Generated __GENERATED_AT__</div>
    </div>
    <div class="header-stats" id="headerStats"></div>
  </header>

  <main class="layout">
    <aside class="panel sidebar">
      <div class="panel-title">
        <span>Filters</span>
        <button class="ghost" id="resetFilters" type="button">Reset</button>
      </div>
      <div class="panel-body">
        <div class="field">
          <label class="field-label" for="tableSearch">Table search</label>
          <input id="tableSearch" type="text" placeholder="orders, customer, ^fact_.*" />
          <label class="check">
            <input id="regexMode" type="checkbox" />
            Treat table search as JavaScript regex
          </label>
          <div class="hint">Matches schema.table and table name.</div>
        </div>

        <div class="field">
          <label class="field-label" for="includePatterns">Explicit include patterns</label>
          <textarea id="includePatterns" placeholder="public.orders&#10;analytics.fact_*"></textarea>
          <div class="hint">Optional. One per line or comma-separated. Supports <span class="mono">*</span>, <span class="mono">?</span>, and <span class="mono">/regex/</span>.</div>
        </div>

        <div class="field">
          <label class="field-label" for="excludePatterns">Exclude patterns</label>
          <textarea id="excludePatterns" placeholder="tmp_*&#10;archive.*"></textarea>
        </div>

        <div class="field">
          <label class="field-label" for="columnSearch">Column / type search</label>
          <input id="columnSearch" type="text" placeholder="customer_id, uuid, timestamp" />
          <div class="hint">Used only for the starting table set; neighbor expansion can still bring related tables in.</div>
        </div>

        <div class="two-col">
          <div class="field">
            <label class="field-label" for="relationDepth">Relation depth</label>
            <select id="relationDepth">
              <option value="0">0 - matched tables only</option>
              <option value="1" selected>1 - direct neighbors</option>
              <option value="2">2 - second-degree neighbors</option>
              <option value="3">3 - third-degree neighbors</option>
              <option value="all">All connected</option>
            </select>
          </div>

          <div class="field">
            <label class="field-label" for="relationDirection">Direction</label>
            <select id="relationDirection">
              <option value="both" selected>Incoming + outgoing</option>
              <option value="outgoing">Outgoing FKs only</option>
              <option value="incoming">Incoming references only</option>
            </select>
          </div>
        </div>

        <div class="two-col">
          <div class="field">
            <label class="field-label" for="pkFilter">Primary key</label>
            <select id="pkFilter">
              <option value="all" selected>All</option>
              <option value="with">With PK</option>
              <option value="without">Without PK</option>
            </select>
          </div>

          <div class="field">
            <label class="field-label" for="relationKind">Relation kind</label>
            <select id="relationKind">
              <option value="all" selected>All</option>
              <option value="any">Has any FK relation</option>
              <option value="outgoing">Has outgoing FK</option>
              <option value="incoming">Is referenced</option>
              <option value="isolated">No FK relation</option>
            </select>
          </div>
        </div>

        <div class="field">
          <label class="check">
            <input id="hideIsolated" type="checkbox" />
            Hide isolated visible tables
          </label>
          <label class="check">
            <input id="showEdgeLabels" type="checkbox" checked />
            Show FK labels on graph
          </label>
          <label class="check">
            <input id="strictSchemaNeighbors" type="checkbox" />
            Apply schema filter to expanded neighbors
          </label>
          <label class="check">
            <input id="freezeAfterLayout" type="checkbox" checked />
            Freeze node positions after layout
          </label>
          <label class="check">
            <input id="requireFilterFirst" type="checkbox" />
            Require a table/column/relation filter before drawing
          </label>
        </div>

        <div class="field">
          <label class="field-label">Schemas</label>
          <div class="schema-toolbar">
            <button class="ghost" id="selectAllSchemas" type="button">All</button>
            <button class="ghost" id="selectNoSchemas" type="button">None</button>
          </div>
          <div class="schema-list" id="schemaFilters"></div>
        </div>

        <div class="button-row">
          <button class="primary" id="applyFilters" type="button">Apply + fit</button>
          <button class="ghost" id="copyTables" type="button">Copy visible tables</button>
        </div>

        <div class="field" style="margin-top: 16px;">
          <label class="field-label">Visible tables</label>
          <div class="visible-list" id="visibleList"></div>
          <div class="hint">Click a table chip to focus that table exactly.</div>
        </div>
      </div>
    </aside>

    <section class="panel graph-panel">
      <div class="graph-topbar">
        <div class="status" id="status">Loading graph...</div>
        <div class="graph-actions">
          <button class="ghost" id="fitGraph" type="button">Fit</button>
          <button class="ghost" id="freezeGraph" type="button">Freeze</button>
          <button class="ghost" id="unfreezeGraph" type="button">Unfreeze</button>
          <button class="ghost" id="stabilizeGraph" type="button">Stabilize</button>
        </div>
      </div>
      <div class="warning" id="warning"></div>
      <div id="network"></div>
    </section>

    <aside class="panel inspector">
      <div class="panel-title">Inspector</div>
      <div class="panel-body" id="inspector">
        <div class="empty">Click a table or FK edge to inspect PK/FK details.</div>
      </div>
    </aside>
  </main>

  <div class="toast" id="toast"></div>

  <script id="graph-data" type="application/json">__GRAPH_JSON__</script>
  <script src="https://unpkg.com/vis-network@9.1.9/dist/vis-network.min.js"></script>
  <script>
    const graph = JSON.parse(document.getElementById("graph-data").textContent);
    const rawNodes = graph.nodes || [];
    const rawEdges = graph.edges || [];
    const nodesById = new Map(rawNodes.map((node) => [node.id, node]));
    const edgesById = new Map(rawEdges.map((edge) => [edge.id, edge]));
    const schemas = graph.schemas || [];
    const degreeById = new Map(rawNodes.map((node) => [node.id, 0]));
    const outgoingById = new Map(rawNodes.map((node) => [node.id, []]));
    const incomingById = new Map(rawNodes.map((node) => [node.id, []]));

    for (const edge of rawEdges) {
      degreeById.set(edge.from, (degreeById.get(edge.from) || 0) + 1);
      degreeById.set(edge.to, (degreeById.get(edge.to) || 0) + 1);
      outgoingById.get(edge.from)?.push(edge);
      incomingById.get(edge.to)?.push(edge);
    }

    const tableNameCounts = new Map();
    for (const node of rawNodes) {
      tableNameCounts.set(node.table, (tableNameCounts.get(node.table) || 0) + 1);
    }

    let lastVisibleIds = [];

    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function escAttr(value) {
      return esc(value).replace(/`/g, "&#96;");
    }

    function hashCode(value) {
      let hash = 0;
      for (let i = 0; i < value.length; i++) {
        hash = ((hash << 5) - hash) + value.charCodeAt(i);
        hash |= 0;
      }
      return Math.abs(hash);
    }

    function colorForSchema(schema) {
      const h = hashCode(schema) % 360;
      return {
        background: `hsl(${h}, 78%, 97%)`,
        border: `hsl(${h}, 60%, 44%)`,
        highlight: {
          background: `hsl(${h}, 86%, 92%)`,
          border: `hsl(${h}, 72%, 32%)`
        },
        hover: {
          background: `hsl(${h}, 86%, 94%)`,
          border: `hsl(${h}, 72%, 36%)`
        }
      };
    }

    function splitPatterns(text) {
      return String(text || "")
        .split(/[\n,]+/)
        .map((part) => part.trim())
        .filter(Boolean);
    }

    function wildcardToRegex(pattern) {
      const escaped = pattern
        .replace(/[.+^${}()|[\]\\]/g, "\\$&")
        .replace(/\*/g, ".*")
        .replace(/\?/g, ".");
      return new RegExp(`^${escaped}$`, "i");
    }

    function patternMatchesNode(pattern, node) {
      const text = pattern.trim();
      if (!text) return false;

      if (text.startsWith("/") && text.lastIndexOf("/") > 0) {
        const lastSlash = text.lastIndexOf("/");
        const body = text.slice(1, lastSlash);
        const flags = text.slice(lastSlash + 1) || "i";
        try {
          const re = new RegExp(body, flags.includes("i") ? flags : `${flags}i`);
          return re.test(node.qualified_name) || re.test(node.table) || re.test(node.schema);
        } catch {
          return false;
        }
      }

      const re = wildcardToRegex(text.toLowerCase());
      return re.test(node.qualified_name.toLowerCase())
        || re.test(node.table.toLowerCase())
        || re.test(node.schema.toLowerCase());
    }

    function selectedSchemas() {
      return new Set(
        [...document.querySelectorAll("#schemaFilters input[type='checkbox']:checked")]
          .map((input) => input.value)
      );
    }

    function hasFocusFilter() {
      return Boolean(
        document.getElementById("tableSearch").value.trim()
        || document.getElementById("includePatterns").value.trim()
        || document.getElementById("columnSearch").value.trim()
        || document.getElementById("excludePatterns").value.trim()
        || document.getElementById("pkFilter").value !== "all"
        || document.getElementById("relationKind").value !== "all"
      );
    }

    function showWarning(message) {
      const warning = document.getElementById("warning");
      if (!message) {
        warning.classList.remove("show");
        warning.textContent = "";
        return;
      }
      warning.textContent = message;
      warning.classList.add("show");
    }

    function tableSearchMatches(node) {
      const query = document.getElementById("tableSearch").value.trim();
      if (!query) return true;

      const haystack = `${node.qualified_name} ${node.table}`;
      if (document.getElementById("regexMode").checked) {
        try {
          return new RegExp(query, "i").test(haystack);
        } catch (err) {
          showWarning(`Invalid table-search regex: ${err.message}`);
          return true;
        }
      }

      return haystack.toLowerCase().includes(query.toLowerCase());
    }

    function columnSearchMatches(node) {
      const query = document.getElementById("columnSearch").value.trim().toLowerCase();
      if (!query) return true;

      return (node.columns || []).some((column) => {
        return String(column.name || "").toLowerCase().includes(query)
          || String(column.type || "").toLowerCase().includes(query)
          || String(column.comment || "").toLowerCase().includes(query);
      });
    }

    function relationKindMatches(node) {
      const kind = document.getElementById("relationKind").value;
      const outgoing = outgoingById.get(node.id)?.length || 0;
      const incoming = incomingById.get(node.id)?.length || 0;
      const degree = outgoing + incoming;

      if (kind === "any") return degree > 0;
      if (kind === "outgoing") return outgoing > 0;
      if (kind === "incoming") return incoming > 0;
      if (kind === "isolated") return degree === 0;
      return true;
    }

    function pkFilterMatches(node) {
      const pkFilter = document.getElementById("pkFilter").value;
      const hasPk = Boolean(node.primary_key);
      if (pkFilter === "with") return hasPk;
      if (pkFilter === "without") return !hasPk;
      return true;
    }

    function excludedByPattern(node) {
      const excludes = splitPatterns(document.getElementById("excludePatterns").value);
      return excludes.some((pattern) => patternMatchesNode(pattern, node));
    }

    function baseMatches(node) {
      const schemas = selectedSchemas();
      if (!schemas.has(node.schema)) return false;

      if (document.getElementById("requireFilterFirst").checked && !hasFocusFilter()) {
        return false;
      }

      const includes = splitPatterns(document.getElementById("includePatterns").value);
      if (includes.length && !includes.some((pattern) => patternMatchesNode(pattern, node))) {
        return false;
      }

      if (excludedByPattern(node)) return false;
      if (!tableSearchMatches(node)) return false;
      if (!columnSearchMatches(node)) return false;
      if (!pkFilterMatches(node)) return false;
      if (!relationKindMatches(node)) return false;

      return true;
    }

    function canShowNeighbor(node) {
      if (!node) return false;
      if (excludedByPattern(node)) return false;

      if (document.getElementById("strictSchemaNeighbors").checked) {
        return selectedSchemas().has(node.schema);
      }

      return true;
    }

    function displayNode(node) {
      const duplicateName = (tableNameCounts.get(node.table) || 0) > 1;
      const baseLabel = duplicateName ? node.qualified_name : node.table;
      const pk = node.primary_key?.columns?.length
        ? `\nPK: ${node.primary_key.columns.join(", ")}`
        : "";

      return {
        id: node.id,
        label: `${baseLabel}${pk}`,
        title: nodeTooltip(node),
        shape: "box",
        margin: 10,
        color: colorForSchema(node.schema),
        borderWidth: node.primary_key ? 2 : 1,
        value: Math.max(1, degreeById.get(node.id) || 1),
        font: {
          face: "Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif",
          size: 13,
          multi: true,
          color: "#0f172a"
        },
        widthConstraint: { maximum: 260 },
        shadow: {
          enabled: true,
          color: "rgba(15,23,42,0.12)",
          size: 10,
          x: 0,
          y: 4
        }
      };
    }

    function displayEdge(edge) {
      const showLabel = document.getElementById("showEdgeLabels").checked;
      const label = showLabel
        ? `${edge.constraint_name}\n${edge.source_columns.join(", ")} → ${edge.target_columns.join(", ")}`
        : "";

      return {
        id: edge.id,
        from: edge.from,
        to: edge.to,
        label,
        title: edgeTooltip(edge),
        arrows: "to",
        width: 1.4,
        color: { color: "#94a3b8", highlight: "#1d4ed8", hover: "#2563eb" },
        font: {
          size: 10,
          color: "#334155",
          strokeWidth: 4,
          strokeColor: "#ffffff",
          align: "middle"
        },
        smooth: {
          enabled: true,
          type: "dynamic",
          roundness: 0.35
        }
      };
    }

    function nodeTooltip(node) {
      const pk = node.primary_key?.columns?.length
        ? node.primary_key.columns.join(", ")
        : "No primary key";
      const outCount = outgoingById.get(node.id)?.length || 0;
      const inCount = incomingById.get(node.id)?.length || 0;

      return `
        <div style="max-width: 360px;">
          <strong>${esc(node.qualified_name)}</strong><br/>
          <span>Type: ${esc(node.type)}</span><br/>
          <span>PK: ${esc(pk)}</span><br/>
          <span>Outgoing FKs: ${outCount}; Incoming references: ${inCount}</span>
        </div>
      `;
    }

    function edgeTooltip(edge) {
      return `
        <div style="max-width: 420px;">
          <strong>${esc(edge.constraint_name)}</strong><br/>
          <span>${esc(edge.from)}.${esc(edge.source_columns.join(", "))}</span><br/>
          <span>→ ${esc(edge.to)}.${esc(edge.target_columns.join(", "))}</span><br/>
          <span>ON DELETE ${esc(edge.on_delete)}; ON UPDATE ${esc(edge.on_update)}</span>
        </div>
      `;
    }

    function renderSchemas() {
      const counts = new Map();
      for (const node of rawNodes) {
        counts.set(node.schema, (counts.get(node.schema) || 0) + 1);
      }

      const container = document.getElementById("schemaFilters");
      container.innerHTML = schemas.map((schema) => `
        <label class="schema-pill" title="${escAttr(schema)}">
          <input type="checkbox" value="${escAttr(schema)}" checked />
          <span>${esc(schema)}</span>
          <span class="badge gray">${counts.get(schema) || 0}</span>
        </label>
      `).join("");
    }

    function renderHeader() {
      const stats = graph.stats || {};
      const meta = graph.metadata || {};
      document.getElementById("headerStats").innerHTML = [
        ["Database", meta.database_name || "unknown"],
        ["Schemas", stats.schema_count ?? schemas.length],
        ["Tables", stats.table_count ?? rawNodes.length],
        ["PKs", stats.pk_count ?? 0],
        ["FKs", stats.fk_count ?? rawEdges.length]
      ].map(([label, value]) => `<span class="stat-pill">${esc(label)}: <strong>${esc(value)}</strong></span>`).join("");

      const generated = graph.generated_at ? `Generated ${esc(graph.generated_at)}` : "Generated";
      const server = meta.server_version ? ` · PostgreSQL ${esc(meta.server_version)}` : "";
      document.getElementById("subtitle").innerHTML = `${generated}${server}`;
    }

    function updateVisibleList(visibleNodes) {
      const list = document.getElementById("visibleList");
      const max = 120;
      const chips = visibleNodes.slice(0, max).map((node) => `
        <button class="table-chip" type="button" data-table="${escAttr(node.qualified_name)}">${esc(node.qualified_name)}</button>
      `);

      if (visibleNodes.length > max) {
        chips.push(`<span class="badge gray">+${visibleNodes.length - max} more</span>`);
      }

      list.innerHTML = chips.length ? chips.join("") : `<span class="hint">No visible tables.</span>`;

      for (const chip of list.querySelectorAll(".table-chip")) {
        chip.addEventListener("click", () => {
          document.getElementById("includePatterns").value = chip.dataset.table;
          document.getElementById("tableSearch").value = "";
          document.getElementById("relationDepth").value = "1";
          applyFilters({ fit: true });
        });
      }
    }

    function renderNodeInspector(node) {
      const pk = node.primary_key;
      const columns = node.columns || [];
      const outgoing = outgoingById.get(node.id) || [];
      const incoming = incomingById.get(node.id) || [];

      document.getElementById("inspector").innerHTML = `
        <div class="detail-section">
          <h3>${esc(node.qualified_name)}</h3>
          <span class="badge">${esc(node.schema)}</span>
          <span class="badge gray">${esc(node.type)}</span>
          ${pk ? `<span class="badge green">PK</span>` : `<span class="badge orange">No PK</span>`}
          ${node.comment ? `<p>${esc(node.comment)}</p>` : ""}
        </div>

        <div class="detail-section">
          <h3>Primary key</h3>
          ${pk ? `
            <div class="mono">${esc(pk.name)}</div>
            <div>${pk.columns.map((col) => `<span class="badge green">${esc(col)}</span>`).join("")}</div>
            <div class="hint mono">${esc(pk.definition || "")}</div>
          ` : `<div class="empty">No primary key constraint found.</div>`}
        </div>

        <div class="detail-section">
          <h3>Columns (${columns.length})</h3>
          <table>
            <thead><tr><th>Name</th><th>Type</th><th>Flags</th></tr></thead>
            <tbody>
              ${columns.map((column) => `
                <tr>
                  <td class="mono">${esc(column.name)}</td>
                  <td class="mono">${esc(column.type)}</td>
                  <td>
                    ${column.is_pk ? `<span class="badge green">PK</span>` : ""}
                    ${column.not_null ? `<span class="badge gray">NOT NULL</span>` : ""}
                  </td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        </div>

        <div class="detail-section">
          <h3>Outgoing foreign keys (${outgoing.length})</h3>
          ${outgoing.length ? outgoing.map(renderFkCard).join("") : `<div class="empty">No outgoing foreign keys.</div>`}
        </div>

        <div class="detail-section">
          <h3>Incoming references (${incoming.length})</h3>
          ${incoming.length ? incoming.map(renderFkCard).join("") : `<div class="empty">No incoming references.</div>`}
        </div>
      `;
    }

    function renderFkCard(edge) {
      return `
        <div class="fk-card">
          <div class="name">${esc(edge.constraint_name)}</div>
          <div class="line">${esc(edge.from)}.${esc(edge.source_columns.join(", "))}</div>
          <div class="line">→ ${esc(edge.to)}.${esc(edge.target_columns.join(", "))}</div>
          <div style="margin-top: 7px;">
            <span class="badge gray">ON DELETE ${esc(edge.on_delete)}</span>
            <span class="badge gray">ON UPDATE ${esc(edge.on_update)}</span>
            <span class="badge gray">MATCH ${esc(edge.match_type)}</span>
            ${edge.deferrable ? `<span class="badge orange">DEFERRABLE</span>` : ""}
            ${edge.initially_deferred ? `<span class="badge orange">INITIALLY DEFERRED</span>` : ""}
          </div>
          <div class="hint mono">${esc(edge.definition || "")}</div>
        </div>
      `;
    }

    function renderEdgeInspector(edge) {
      document.getElementById("inspector").innerHTML = `
        <div class="detail-section">
          <h3>${esc(edge.constraint_name)}</h3>
          <span class="badge">Foreign key</span>
          <span class="badge gray">MATCH ${esc(edge.match_type)}</span>
        </div>
        <div class="detail-section">
          <h3>Relation</h3>
          <div class="mono">${esc(edge.from)}.${esc(edge.source_columns.join(", "))}</div>
          <div class="mono">→ ${esc(edge.to)}.${esc(edge.target_columns.join(", "))}</div>
        </div>
        <div class="detail-section">
          <h3>Actions</h3>
          <span class="badge gray">ON DELETE ${esc(edge.on_delete)}</span>
          <span class="badge gray">ON UPDATE ${esc(edge.on_update)}</span>
          ${edge.deferrable ? `<span class="badge orange">DEFERRABLE</span>` : ""}
          ${edge.initially_deferred ? `<span class="badge orange">INITIALLY DEFERRED</span>` : ""}
        </div>
        <div class="detail-section">
          <h3>Definition</h3>
          <div class="mono">${esc(edge.definition || "")}</div>
        </div>
      `;
    }

    function calculateVisibleSet() {
      if (document.getElementById("requireFilterFirst").checked && !hasFocusFilter()) {
        showWarning("Large schema mode is enabled: add a table, column, PK, or relation filter, or uncheck 'Require a table/column/relation filter before drawing'.");
      } else {
        showWarning("");
      }

      const baseIds = new Set(rawNodes.filter(baseMatches).map((node) => node.id));
      const visible = new Set(baseIds);
      let frontier = new Set(baseIds);

      const depthRaw = document.getElementById("relationDepth").value;
      const maxDepth = depthRaw === "all" ? rawNodes.length : Number(depthRaw || 0);
      const direction = document.getElementById("relationDirection").value;

      for (let depth = 0; depth < maxDepth && frontier.size > 0; depth++) {
        const next = new Set();

        for (const edge of rawEdges) {
          if ((direction === "both" || direction === "outgoing") && frontier.has(edge.from)) {
            const target = nodesById.get(edge.to);
            if (!visible.has(edge.to) && canShowNeighbor(target)) {
              next.add(edge.to);
            }
          }

          if ((direction === "both" || direction === "incoming") && frontier.has(edge.to)) {
            const source = nodesById.get(edge.from);
            if (!visible.has(edge.from) && canShowNeighbor(source)) {
              next.add(edge.from);
            }
          }
        }

        if (!next.size) break;
        for (const id of next) visible.add(id);
        frontier = next;
      }

      let visibleEdges = rawEdges.filter((edge) => visible.has(edge.from) && visible.has(edge.to));

      if (document.getElementById("hideIsolated").checked) {
        const connectedIds = new Set();
        for (const edge of visibleEdges) {
          connectedIds.add(edge.from);
          connectedIds.add(edge.to);
        }
        for (const id of [...visible]) {
          if (!connectedIds.has(id)) visible.delete(id);
        }
        visibleEdges = rawEdges.filter((edge) => visible.has(edge.from) && visible.has(edge.to));
      }

      return { visible, visibleEdges };
    }

    let network;
    let nodeDataSet;
    let edgeDataSet;

    function applyFilters(options = {}) {
      const { visible, visibleEdges } = calculateVisibleSet();
      const visibleNodes = [...visible]
        .map((id) => nodesById.get(id))
        .filter(Boolean)
        .sort((a, b) => a.qualified_name.localeCompare(b.qualified_name));

      lastVisibleIds = visibleNodes.map((node) => node.id);

      nodeDataSet.clear();
      edgeDataSet.clear();
      nodeDataSet.add(visibleNodes.map(displayNode));
      edgeDataSet.add(visibleEdges.map(displayEdge));

      document.getElementById("status").innerHTML = `
        ${visibleNodes.length} / ${rawNodes.length} tables visible · ${visibleEdges.length} / ${rawEdges.length} FKs visible
        <small>Use table patterns, schema toggles, column search, and relation depth to refine the graph.</small>
      `;

      updateVisibleList(visibleNodes);

      if (document.getElementById("freezeAfterLayout")?.checked) {
        stabilizeThenMaybeFreeze(120);
      } else {
        network.setOptions({ physics: { enabled: true } });
      }

      if (options.fit) {
        window.setTimeout(() => {
          network.fit({ animation: { duration: 300, easingFunction: "easeInOutQuad" } });
        }, 80);
      }
    }

    function debounce(fn, delay) {
      let timer;
      return (...args) => {
        clearTimeout(timer);
        timer = setTimeout(() => fn(...args), delay);
      };
    }

    function freezeGraphPositions() {
      if (!network) return;
      network.storePositions();
      network.setOptions({ physics: { enabled: false } });
      toast("Graph frozen. Nodes can still be dragged manually.");
    }

    function unfreezeGraphPositions() {
      if (!network) return;
      network.setOptions({ physics: { enabled: true } });
      network.stabilize(80);
      toast("Graph physics enabled temporarily.");
    }

    function stabilizeThenMaybeFreeze(iterations = 120) {
      if (!network) return;

      network.setOptions({ physics: { enabled: true } });
      network.stabilize(iterations);

      if (document.getElementById("freezeAfterLayout")?.checked) {
        network.once("stabilized", () => {
          freezeGraphPositions();
        });
      }
    }

    function resetFilters() {
      document.getElementById("tableSearch").value = "";
      document.getElementById("regexMode").checked = false;
      document.getElementById("includePatterns").value = "";
      document.getElementById("excludePatterns").value = "";
      document.getElementById("columnSearch").value = "";
      document.getElementById("relationDepth").value = "1";
      document.getElementById("relationDirection").value = "both";
      document.getElementById("pkFilter").value = "all";
      document.getElementById("relationKind").value = "all";
      document.getElementById("hideIsolated").checked = false;
      document.getElementById("showEdgeLabels").checked = true;
      document.getElementById("strictSchemaNeighbors").checked = false;
      document.getElementById("freezeAfterLayout").checked = true;
      document.getElementById("requireFilterFirst").checked = rawNodes.length > 350;
      for (const input of document.querySelectorAll("#schemaFilters input[type='checkbox']")) {
        input.checked = true;
      }
      applyFilters({ fit: true });
    }

    function copyVisibleTables() {
      const text = lastVisibleIds.slice().sort().join("\n");
      if (!text) {
        toast("No visible tables to copy.");
        return;
      }

      const fallback = () => {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        document.body.removeChild(textarea);
        toast("Copied visible table list.");
      };

      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(text).then(
          () => toast("Copied visible table list."),
          fallback
        );
      } else {
        fallback();
      }
    }

    function toast(message) {
      const element = document.getElementById("toast");
      element.textContent = message;
      element.classList.add("show");
      window.setTimeout(() => element.classList.remove("show"), 1800);
    }

    function initialize() {
      renderHeader();
      renderSchemas();

      if (!window.vis) {
        document.getElementById("network").innerHTML = `
          <div style="padding: 20px;">
            <div class="empty">
              vis-network could not be loaded from the CDN. Connect to the internet, or download vis-network locally and update the script/link tags in this HTML.
            </div>
          </div>
        `;
        return;
      }

      nodeDataSet = new vis.DataSet([]);
      edgeDataSet = new vis.DataSet([]);

      network = new vis.Network(
        document.getElementById("network"),
        { nodes: nodeDataSet, edges: edgeDataSet },
        {
          interaction: {
            hover: true,
            tooltipDelay: 120,
            navigationButtons: true,
            keyboard: true,
            multiselect: true
          },
          layout: {
            improvedLayout: true
          },
          physics: {
            enabled: true,
            solver: "forceAtlas2Based",
            forceAtlas2Based: {
              gravitationalConstant: -36,
              centralGravity: 0.008,
              springLength: 190,
              springConstant: 0.045,
              damping: 0.75,
              avoidOverlap: 0.85
            },
            minVelocity: 1.6,
            stabilization: {
              enabled: true,
              iterations: 120,
              updateInterval: 25,
              fit: false
            }
          },
          nodes: {
            chosen: true
          },
          edges: {
            selectionWidth: 2.5,
            hoverWidth: 2
          }
        }
      );

      network.on("click", (params) => {
        if (params.nodes?.length) {
          const node = nodesById.get(params.nodes[0]);
          if (node) renderNodeInspector(node);
          return;
        }
        if (params.edges?.length) {
          const edge = edgesById.get(params.edges[0]);
          if (edge) renderEdgeInspector(edge);
          return;
        }
      });

      network.on("doubleClick", (params) => {
        if (params.nodes?.length) {
          const node = nodesById.get(params.nodes[0]);
          if (!node) return;
          document.getElementById("includePatterns").value = node.qualified_name;
          document.getElementById("relationDepth").value = "1";
          applyFilters({ fit: true });
        }
      });

      const debouncedApply = debounce(() => applyFilters({ fit: false }), 220);
      for (const element of document.querySelectorAll("input, textarea, select")) {
        element.addEventListener("input", debouncedApply);
        element.addEventListener("change", debouncedApply);
      }

      document.getElementById("applyFilters").addEventListener("click", () => applyFilters({ fit: true }));
      document.getElementById("resetFilters").addEventListener("click", resetFilters);
      document.getElementById("copyTables").addEventListener("click", copyVisibleTables);
      document.getElementById("fitGraph").addEventListener("click", () => network.fit({ animation: true }));
      document.getElementById("freezeGraph").addEventListener("click", freezeGraphPositions);
      document.getElementById("unfreezeGraph").addEventListener("click", unfreezeGraphPositions);
      document.getElementById("stabilizeGraph").addEventListener("click", () => stabilizeThenMaybeFreeze(120));

      document.getElementById("selectAllSchemas").addEventListener("click", () => {
        for (const input of document.querySelectorAll("#schemaFilters input[type='checkbox']")) {
          input.checked = true;
        }
        applyFilters({ fit: true });
      });

      document.getElementById("selectNoSchemas").addEventListener("click", () => {
        for (const input of document.querySelectorAll("#schemaFilters input[type='checkbox']")) {
          input.checked = false;
        }
        applyFilters({ fit: true });
      });

      if (rawNodes.length > 350) {
        document.getElementById("requireFilterFirst").checked = true;
      }

      applyFilters({ fit: true });
    }

    initialize();
  </script>
</body>
</html>
"""


def render_html(graph: Dict[str, Any]) -> str:
    json_text = json.dumps(graph, ensure_ascii=False, separators=(",", ":"))
    # Prevent accidental script termination if comments contain the string </script>.
    json_text = json_text.replace("</", "<\\/")
    return HTML_TEMPLATE.replace("__GENERATED_AT__", graph.get("generated_at", "")).replace(
        "__GRAPH_JSON__", json_text
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a browser-openable PostgreSQL PK/FK table relation network.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("PG_DSN") or os.environ.get("DATABASE_URL"),
        help="PostgreSQL DSN. Can also be supplied via PG_DSN or DATABASE_URL.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="pg_relation_network.html",
        help="Output HTML path.",
    )
    parser.add_argument(
        "--include-schema",
        action="append",
        default=[],
        help="Schema to include at generation time. Repeat or comma-separate. If omitted, all non-system schemas are included.",
    )
    parser.add_argument(
        "--exclude-schema",
        action="append",
        default=[],
        help="Schema to exclude at generation time. Repeat or comma-separate.",
    )
    parser.add_argument(
        "--table-regex",
        default=None,
        help="Optional generation-time regex matched against schema.table and table.",
    )
    parser.add_argument(
        "--exclude-table-regex",
        default=None,
        help="Optional generation-time regex for tables to exclude.",
    )
    parser.add_argument(
        "--include-system-schemas",
        action="store_true",
        help="Include pg_* and information_schema objects.",
    )
    parser.add_argument(
        "--statement-timeout-ms",
        type=int,
        default=120_000,
        help="Statement timeout for metadata queries.",
    )
    parser.add_argument(
        "--connect-timeout",
        type=int,
        default=15,
        help="Connection timeout in seconds.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.dsn:
        parser.error("Provide --dsn or set PG_DSN / DATABASE_URL.")

    include_schemas = set(parse_csvish(args.include_schema))
    exclude_schemas = set(parse_csvish(args.exclude_schema))
    table_re = compile_regex(args.table_regex, "--table-regex")
    exclude_table_re = compile_regex(args.exclude_table_regex, "--exclude-table-regex")

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: psycopg. Install it with:\n" '    pip install "psycopg[binary]"'
        ) from exc

    connect_kwargs = {
        "row_factory": dict_row,
        "connect_timeout": args.connect_timeout,
        "application_name": "pg_relation_network_generator",
    }

    print("Connecting to PostgreSQL and reading schema metadata...", file=sys.stderr)

    with psycopg.connect(args.dsn, **connect_kwargs) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (str(args.statement_timeout_ms),),
            )

        metadata_rows = fetch_all(conn, metadata_sql())
        tables = fetch_all(conn, table_sql(args.include_system_schemas))
        columns = fetch_all(conn, column_sql(args.include_system_schemas))
        pks = fetch_all(conn, pk_sql(args.include_system_schemas))
        fks = fetch_all(conn, fk_sql(args.include_system_schemas))

    graph = build_graph(
        tables=tables,
        columns=columns,
        pks=pks,
        fks=fks,
        include_schemas=include_schemas,
        exclude_schemas=exclude_schemas,
        table_re=table_re,
        exclude_table_re=exclude_table_re,
    )

    graph["metadata"] = metadata_rows[0] if metadata_rows else {}
    graph["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    graph["generator_version"] = FREEZE_LAYOUT_VERSION
    graph["generation_filters"] = {
        "include_schemas": sorted(include_schemas),
        "exclude_schemas": sorted(exclude_schemas),
        "table_regex": args.table_regex,
        "exclude_table_regex": args.exclude_table_regex,
        "include_system_schemas": bool(args.include_system_schemas),
    }

    output_path = Path(args.output).expanduser().resolve()
    output_path.write_text(render_html(graph), encoding="utf-8")

    stats = graph["stats"]
    print(
        f"Wrote {output_path}\n"
        f"Tables: {stats['table_count']} | PKs: {stats['pk_count']} | FKs: {stats['fk_count']} | Schemas: {stats['schema_count']}\n"
        f"Open it in a browser and use the left-side filters.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
