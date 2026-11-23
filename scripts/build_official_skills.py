#!/usr/bin/env python3
"""Build the catalog of OFFICIAL Genos skills from the Skills v2 runbooks (MIP-2048).

    python scripts/build_official_skills.py --docs ~/git/genos-split/genos-docs --ref origin/main \\
        --release v0.5.3 --tools-dump tools.json

The runbooks (genos-docs `plugins/<pack>/skills/<name>/SKILL.md`, released as the Genos plugin) talk
to Claude Code and to the connector's tool names. A skill the in-app agent runs must talk to the
in-app agent, so each runbook is translated here by RULES (no model, no network):

  * the front matter becomes the skill's title and description (the part before "Use when");
  * the tool names the connector renames are put back to the in-app names (`get_task` -> `fetch_task`);
  * sentences that only make sense in Claude Code or over the connector ("Genos tools missing:
    suggest /genos:setup", "it is on the `analytics` address", `/genos-sales:call-follow-up`) are
    removed or turned into `/call-follow-up`;
  * the reference files a runbook points at (`references/task-body.md`) are appended to the body;
  * a one-line source note (link to the public release the text came from) closes the body, so a new
    release is a new text and a new version.

A runbook that cannot run in the app is NOT translated: it is listed under `excluded` with the reason
(it needs git or gh on the person's machine, writes Claude's memory file, or reads the connector-only
briefing). The result is written to `origin/services/skills/official_catalog/` (one `<slug>.md` per
skill and `manifest.json`) and `origin/services/skills/official_slugs.py`; both are committed, and
`manage.py sync_official_skills` reads them. The output is deterministic: a second run changes nothing.

The script fails (exit 1) when a translated body still mentions Claude Code, the connector, git or gh,
or a tool the in-app agent does not have (when `--tools-dump` is given). Fix the rule, not the output.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG_DIR = ROOT / "origin" / "services" / "skills" / "official_catalog"
SLUGS_FILE = ROOT / "origin" / "services" / "skills" / "official_slugs.py"

REPO_URL = "https://github.com/genos-tech/genos-claude-plugin"
BODY_MAX = 12_000
DESCRIPTION_MAX = 280

# plugin folder in genos-docs -> (folder in the public release repo, tag, icon)
PACKS: dict[str, tuple[str, str, str]] = {
    "genos-claude-plugin": ("genos", "general", "✨"),
    "genos-engineering": ("genos-engineering", "engineering", "🛠️"),
    "genos-product": ("genos-product", "product", "🧭"),
    "genos-designer": ("genos-designer", "design", "🎨"),
    "genos-sales": ("genos-sales", "sales", "🤝"),
    "genos-marketing": ("genos-marketing", "marketing", "📣"),
    "genos-support": ("genos-support", "support", "🎧"),
    "genos-research": ("genos-research", "research", "🔬"),
    "genos-teaching": ("genos-teaching", "teaching", "🍎"),
    "genos-student": ("genos-student", "student", "🎓"),
    "genos-founder": ("genos-founder", "founder", "🚀"),
}

# Runbooks that do not run in the app, with the reason. Everything else is translated.
EXCLUDED: dict[str, str] = {
    "setup": "writes the Genos rules into Claude's memory file and installs plugins: Claude Code only",
    "start-task": "branches and reads the code with git on the person's machine",
    "open-pr": "reads a pull request's checks with gh on the person's machine",
    "ready-to-merge": "reads a pull request's checks with gh on the person's machine",
    "close-out": "reads merged pull requests and checks with gh on the person's machine",
    "catchup": "built on the connector-only briefing (the in-app built-in `/catch-me-up` covers it)",
    "wrapup": "built on the connector-only briefing",
    "weekly-review": "built on the connector-only briefing",
    "weekly-priorities": "built on the connector-only briefing",
    "study-week": "built on the connector-only briefing",
    "design-review": "replies to and resolves note comments, which the in-app agent cannot do",
}

# connector name -> in-app name (`origin/views/public/mcp/tools.py` `_RENAMES`, inverted)
TOOL_RENAMES = {
    "get_task": "fetch_task",
    "list_my_tasks": "get_my_focus_tasks",
    "search_workspace": "search_knowledge_base",
    "get_note": "fetch_note",
    "get_note_history": "fetch_note_history",
    "get_pr": "fetch_pr",
    "get_chat_thread": "fetch_chat_thread",
}
_RENAME_RE = re.compile(
    r"(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, TOOL_RENAMES)) + r")(?![A-Za-z0-9_])"
)

# Sentences and clauses that only make sense in Claude Code or over the connector.
_CLEAN_RULES: list[tuple[re.Pattern[str], str]] = [
    # "Genos tools missing: say so, suggest /genos:setup, stop." and its variants
    (
        re.compile(
            r"\s*(?:If )?(?:the )?(?:Genos )?tools? (?:are )?missing[:,]? say so,? suggest "
            r"/genos:setup,? (?:and )?stop\.",
            re.IGNORECASE,
        ),
        "",
    ),
    # "(the `analytics` address; if missing, ...)" and "(it is on the `me` address)"
    (re.compile(r"\s*\((?:[^()]*?)`[a-z/-]+` address(?:es)?(?:[^()]*?)\)"), ""),
    # "Tool missing (...): fallback." / "If the tool is not on this address (...), skip it."
    (
        re.compile(
            r"\s*(?:A tool missing|Tools? missing|If the tool is not on this address)[^\n]*?\.(?=\s|$)"
        ),
        "",
    ),
    # the in-app search has no date filter: the runbook's own "in the period" tells the agent what to keep
    (re.compile(r" with `updated_after`"), ""),
    # a command of the plugin -> the in-app name of the skill: /genos-sales:call-follow-up -> /call-follow-up
    (re.compile(r"/genos(?:-[a-z]+)?:([a-z-]+)"), r"/\1"),
]

# Per-skill literal edits for the few places a rule cannot reach. A miss is an error: the runbook moved.
PATCHES: dict[str, list[tuple[str, str]]] = {
    "triage": [
        (
            "1. Call `get_my_briefing` and take `waiting_on_me` (requests and mentions with their source).\n"
            "2. Call `list_my_inbox` (category `requests`, then `activity`) and `list_my_mentions`.",
            "1. Call `list_my_inbox` (category `requests`, then `activity`).\n"
            "2. Call `list_my_mentions`: with the inbox, this is everything that waits on the person, "
            "with the source of each.",
        ),
    ],
    "record-decision": [(", or `edit_note_block` for one passage", "")],
    "account-brief": [
        (
            'then again with `order: "newest"` and `updated_after` 60 days back',
            "then keep the newest of what changed in the last 60 days",
        )
    ],
    "campaign-recap": [
        (
            "(`updated_after` the milestone's start)",
            "(only what changed since the milestone's start)",
        )
    ],
}

_FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_REF_RE = re.compile(r"`references/([a-z-]+)\.md`")

# What the translated text may not contain: it would send the in-app agent to something it lacks.
_FORBIDDEN = re.compile(
    r"`[a-z/-]+` address(?:es)?|this address|core address|\bMCP\b|connector|/genos|briefing|"
    r"\bgh [a-z]|\bgit [a-z]|CLAUDE\.md|Claude|memory file|plugin|worktree|`references/",
    re.IGNORECASE,
)
_TOOL_WORD = re.compile(r"`([a-z]+(?:_[a-z0-9]+)+)`")


class BuildError(Exception):
    pass


def _git(docs: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(docs), *args], text=True)


def _read(docs: Path, ref: str, path: str) -> str:
    return _git(docs, "show", f"{ref}:{path}")


def parse_runbook(text: str) -> tuple[dict[str, str], str]:
    m = _FRONT_RE.match(text)
    if not m:
        raise BuildError("no front matter")
    meta: dict[str, str] = {}
    for line in m.group(1).split("\n"):
        k, _, v = line.partition(":")
        if k.strip() in ("name", "description"):
            meta[k.strip()] = v.strip()
    return meta, text[m.end() :].strip("\n")


def build_description(raw: str) -> str:
    """What the skill does: the front matter's text before "Use when", cut at a sentence."""
    head = re.split(r"\s+Use when\b", raw, maxsplit=1)[0].strip()
    # the runbook's own list of the tools it calls ("Uses a, b, c.") is for the connector
    head = re.split(r"\s+Uses [a-z_]+", head, maxsplit=1)[0].strip()
    head = TOOL_PATTERN_FIX(head)
    if len(head) > DESCRIPTION_MAX:
        cut = head[:DESCRIPTION_MAX]
        end = max(cut.rfind(". "), cut.rfind("; "), cut.rfind(": "))
        head = cut[: end if end > 80 else cut.rfind(" ")].rstrip(" ,;:.") + "."
    return head


def TOOL_PATTERN_FIX(text: str) -> str:  # noqa: N802 - keeps the call above readable
    return _RENAME_RE.sub(lambda m: TOOL_RENAMES[m.group(1)], text)


def translate_body(slug: str, body: str, refs: dict[str, str]) -> str:
    for pattern, repl in _CLEAN_RULES:
        body = pattern.sub(repl, body)
    # after the rules, so a patch is written against the text the rules leave
    for old, new in PATCHES.get(slug, []):
        if old not in body:
            raise BuildError(f"{slug}: the patch text moved in the runbook: {old[:60]!r}")
        body = body.replace(old, new)
    body = _RENAME_RE.sub(lambda m: TOOL_RENAMES[m.group(1)], body)
    appended: list[str] = []

    def ref(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in refs:
            raise BuildError(f"{slug}: reference {name} not found")
        if name not in appended:
            appended.append(name)
        return f"the {name.replace('-', ' ')} below"

    body = _REF_RE.sub(ref, body)
    body = re.sub(r"[ \t]+\n", "\n", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    for name in appended:
        text = refs[name].strip("\n")
        # the reference's own H1 becomes a section of this skill
        text = re.sub(r"\A# (.+)", r"## Reference: \1", text, count=1)
        body += "\n\n" + text
    return body


def source_line(release: str, pack_dir: str, name: str) -> str:
    url = f"{REPO_URL}/blob/{release}/plugins/{pack_dir}/skills/{name}/SKILL.md"
    return f"Source: [Genos skills {release}]({url}), the runbook this skill was made from."


def build(docs: Path, ref: str, release: str, tools: dict | None) -> tuple[dict, dict[str, str]]:
    listing = _git(docs, "ls-tree", "-r", "--name-only", ref).split()
    skills: list[dict] = []
    bodies: dict[str, str] = {}
    excluded: dict[str, str] = {}
    errors: list[str] = []
    seen_excluded: set[str] = set()
    for path in sorted(listing):
        m = re.fullmatch(r"plugins/([^/]+)/skills/([^/]+)/SKILL\.md", path)
        if not m or m.group(1) not in PACKS:
            continue
        pack, name = m.groups()
        pack_dir, tag, icon = PACKS[pack]
        meta, body = parse_runbook(_read(docs, ref, path))
        if meta.get("name") != name:
            raise BuildError(f"{path}: name {meta.get('name')!r} differs from its folder")
        if name in EXCLUDED:
            excluded[name] = EXCLUDED[name]
            seen_excluded.add(name)
            continue
        refs = {
            Path(p).stem: _read(docs, ref, p)
            for p in listing
            if p.startswith(f"plugins/{pack}/skills/{name}/references/") and p.endswith(".md")
        }
        title_m = re.match(r"# (.+)", body)
        if not title_m:
            raise BuildError(f"{path}: no H1 title")
        out = translate_body(name, body, refs)
        problems = lint(name, out, tools)
        out += "\n\n" + source_line(release, pack_dir, name)
        if len(out) > BODY_MAX:
            problems.append(f"{name}: {len(out)} characters, the limit is {BODY_MAX}")
        errors += problems
        skills.append(
            {
                "slug": name,
                "title": title_m.group(1).strip(),
                "description": build_description(meta["description"]),
                "tags": [tag],
                "icon": icon,
                "tool_level": "act" if writes(out, tools) else "read",
                "pack": tag,
                "source_path": f"plugins/{pack_dir}/skills/{name}/SKILL.md",
            }
        )
        bodies[name] = out + "\n"
    missing = set(EXCLUDED) - seen_excluded
    if missing:
        raise BuildError(f"excluded runbooks not found in {ref}: {sorted(missing)}")
    if errors:
        raise BuildError("\n".join(errors))
    slugs = [s["slug"] for s in skills]
    if len(set(slugs)) != len(slugs):
        raise BuildError("two runbooks share a slug")
    manifest = {
        "release": release,
        "source": REPO_URL,
        "docs_ref": _git(docs, "rev-parse", ref).strip(),
        "skills": skills,
        "excluded": dict(sorted(excluded.items())),
    }
    return manifest, bodies


def lint(slug: str, body: str, tools: dict | None) -> list[str]:
    out: list[str] = []
    for line in body.split("\n"):
        if _FORBIDDEN.search(line):
            out.append(f"{slug}: still Claude Code or connector text: {line.strip()[:140]}")
    if tools:
        known = set(tools["registry"]) | {p for ps in tools["params"].values() for p in ps}
        for name in sorted(set(_TOOL_WORD.findall(body))):
            if name in known or name in FIELD_NAMES:
                continue
            out.append(f"{slug}: `{name}` is not an in-app tool or parameter")
    return out


def writes(body: str, tools: dict | None) -> bool:
    if not tools:
        return True
    return any(n in set(tools["write"]) for n in _TOOL_WORD.findall(body))


# Names of fields in tool results the runbooks read; not tools and not parameters.
FIELD_NAMES = {
    "last_progress_at",
    "effective_status",
    "backs_milestone_id",
    "closed_by_ancestor",
    "body_hash",
    "content_markdown",
    "content_text",
    "redacted_count",
    "task_id",
    "display_id",
    "milestone_id",
    "numeric_id",
    "project_id",
    "parent_task_id",
    "due_date",
    "start_date",
    "assignee_id",
    "note_id",
    "note_type",
    "pr_url",
    "display_id_warning",
    "webhook_warning",
    "stale_days",
    "target_date",
    "existing_milestone_id",
    "merged_pr",
    "blocked_by_indexes",
    "parent_index",
    "task_field_rules",
    "has_goal",
}


def write_outputs(manifest: dict, bodies: dict[str, str]) -> None:
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    keep = {f"{s}.md" for s in bodies} | {"manifest.json"}
    for old in CATALOG_DIR.glob("*.md"):
        if old.name not in keep:
            old.unlink()
    for slug, text in bodies.items():
        (CATALOG_DIR / f"{slug}.md").write_text(text, encoding="utf-8")
    (CATALOG_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    slugs = sorted(bodies)
    lines = ",\n".join(f'        "{s}"' for s in slugs)
    SLUGS_FILE.write_text(
        '"""The slugs of the official skills (MIP-2048): reserved, so nobody else can take the name\n'
        "after which an official skill runs. Written by `scripts/build_official_skills.py`; a test\n"
        'pins it to `official_catalog/manifest.json`."""\n\n'
        "OFFICIAL_SLUGS = frozenset(\n    {\n" + lines + ",\n    }\n)\n",
        encoding="utf-8",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--docs", required=True, type=Path, help="a genos-docs checkout")
    ap.add_argument("--ref", default="origin/main", help="the git ref of genos-docs to read")
    ap.add_argument("--release", required=True, help="the release tag of the public plugin, v0.5.3")
    ap.add_argument("--tools-dump", type=Path, help="JSON from scripts/dump_in_app_tools.py")
    args = ap.parse_args()
    tools = json.loads(args.tools_dump.read_text()) if args.tools_dump else None
    try:
        manifest, bodies = build(args.docs, args.ref, args.release, tools)
    except BuildError as exc:
        print(exc, file=sys.stderr)
        return 1
    write_outputs(manifest, bodies)
    print(f"{len(bodies)} skills written, {len(manifest['excluded'])} runbooks excluded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
