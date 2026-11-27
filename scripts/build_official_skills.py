#!/usr/bin/env python3
"""Build the catalog of OFFICIAL Genos skills from the Skills v2 runbooks (MIP-2048).

    python scripts/build_official_skills.py --docs ~/git/genos-split/genos-docs --ref origin/main \\
        --release v0.6.0 --tools-dump tools.json

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
or a tool the in-app agent does not have. Fix the rule, not the output.

`--tools-dump` is needed (MIP-2448): without the in-app agent's tool list nobody can tell a skill that
only reads from one that writes, or check the tool names. Make it first, where Django runs:
`python manage.py shell -c "exec(open('scripts/dump_in_app_tools.py').read())"` (writes tools.json).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INAPP_DIR = ROOT / "scripts" / "official_inapp"
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

# The tag of a skill that can make a dashboard (MIP-2418): the dashboard form lists only skills that
# carry it. An ordinary tag, so a member can put it on their own skill. A skill is here only when its
# result is a report of numbers or lists, built by READING (a dashboard run has no write tool, so a
# "save as a note" step never happens there), and it needs no figures the person must paste in.
DASHBOARD_TAG = "dashboard"
DASHBOARD_SKILLS: dict[str, str] = {
    "dashboard-page": "the starter template: answers with the dashboard data itself (MIP-2409)",
    "status-report": "what changed, what needs a decision and what is stuck, from the data",
    "weekly-review": "the week's closed, slipped and stuck work and a top five",
    "weekly-priorities": "five priorities, the blockers and the load per person as counts",
    "pipeline-review": "a table of deals with days quiet and the next step",
    "support-digest": "customer issue themes with counts, fixes and open escalations",
    "research-update": "what was done, found and stuck in the period",
    "content-week": "this week's publishing list by day and what waits for approval",
    "study-week": "the week's deadlines, classes and to-dos by day",
}

# Runbooks that do not run in the app, with the reason. Everything else is translated.
EXCLUDED: dict[str, str] = {
    "setup": "writes the Genos rules into Claude's memory file and installs plugins: Claude Code only",
    "start-task": "branches and reads the code with git on the person's machine",
    "open-pr": "reads a pull request's checks with gh on the person's machine",
    "ready-to-merge": "reads a pull request's checks with gh on the person's machine",
    "close-out": "reads merged pull requests and checks with gh on the person's machine",
    "catchup": "built on the connector-only briefing; the in-app built-in `/catch-me-up` already is the catch-up (MIP-2203)",
    "html-page": "writes an HTML note with `save_html_note`, a connector-only tool the in-app agent does not have",
    "design-review": "replies to and resolves note comments, which the in-app agent cannot do",
}

# Runbooks built on the connector-only briefing cannot be translated by rules. Their in-app versions
# are written by hand in `scripts/official_inapp/<slug>.md` (front matter: name, description, pack
# folder of genos-docs) from the in-app reads, and replace the runbook's text. The runbook must still
# exist in genos-docs: its release link closes the body, and a new release re-checks it (MIP-2203).

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
        if k.strip() in ("name", "description", "pack"):
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
        # the shared checklists name connector tools too: the in-app names apply to them as well
        text = _RENAME_RE.sub(lambda m: TOOL_RENAMES[m.group(1)], refs[name].strip("\n"))
        # the reference's own H1 becomes a section of this skill
        text = re.sub(r"\A# (.+)", r"## Reference: \1", text, count=1)
        body += "\n\n" + text
    return body


def source_line(release: str, pack_dir: str, name: str) -> str:
    url = f"{REPO_URL}/blob/{release}/plugins/{pack_dir}/skills/{name}/SKILL.md"
    return f"Source: [Genos skills {release}]({url}), the runbook this skill was made from."


def build(docs: Path, ref: str, release: str, tools: dict) -> tuple[dict, dict[str, str]]:
    listing = _git(docs, "ls-tree", "-r", "--name-only", ref).split()
    skills: list[dict] = []
    bodies: dict[str, str] = {}
    excluded: dict[str, str] = {}
    errors: list[str] = []
    seen_excluded: set[str] = set()
    used_handwritten: set[str] = set()
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
        handwritten = INAPP_DIR / f"{name}.md"
        if handwritten.exists():
            meta, body = parse_runbook(handwritten.read_text(encoding="utf-8"))
            if meta.get("name") != name or meta.get("pack") != pack:
                raise BuildError(f"{handwritten}: name or pack differs from the runbook")
            used_handwritten.add(name)
        refs = {
            Path(p).stem: _read(docs, ref, p)
            for p in listing
            if p.startswith(f"plugins/{pack}/skills/{name}/references/") and p.endswith(".md")
        }
        title_m = re.match(r"# (.+)", body)
        if not title_m:
            raise BuildError(f"{path}: no H1 title")
        out = body.strip("\n") if handwritten.exists() else translate_body(name, body, refs)
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
                "tags": [tag, DASHBOARD_TAG] if name in DASHBOARD_SKILLS else [tag],
                "icon": icon,
                "tool_level": "act" if writes(out, tools) else "read",
                "pack": tag,
                "source_path": f"plugins/{pack_dir}/skills/{name}/SKILL.md",
            }
        )
        bodies[name] = out + "\n"
    unknown = set(DASHBOARD_SKILLS) - {s["slug"] for s in skills}
    if unknown:
        raise BuildError(f"dashboard skills that are not in the catalog: {sorted(unknown)}")
    missing = set(EXCLUDED) - seen_excluded
    if missing:
        raise BuildError(f"excluded runbooks not found in {ref}: {sorted(missing)}")
    stray = {p.stem for p in INAPP_DIR.glob("*.md")} - used_handwritten
    if stray:
        raise BuildError(
            f"hand-written skills with no runbook (or an excluded one): {sorted(stray)}"
        )
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


def lint(slug: str, body: str, tools: dict) -> list[str]:
    out: list[str] = []
    for line in body.split("\n"):
        if _FORBIDDEN.search(line):
            out.append(f"{slug}: still Claude Code or connector text: {line.strip()[:140]}")
    known = set(tools["registry"]) | {p for ps in tools["params"].values() for p in ps}
    for name in sorted(set(_TOOL_WORD.findall(body))):
        if name in known or name in FIELD_NAMES:
            continue
        out.append(f"{slug}: `{name}` is not an in-app tool or parameter")
    return out


def writes(body: str, tools: dict) -> bool:
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
    ap.add_argument("--release", required=True, help="the release tag of the public plugin, v0.6.0")
    ap.add_argument("--tools-dump", type=Path, help="JSON from scripts/dump_in_app_tools.py")
    args = ap.parse_args()
    if not args.tools_dump:
        print(
            "Nothing written: --tools-dump is needed, or every skill would be marked as one that "
            "writes. Make tools.json where Django runs with scripts/dump_in_app_tools.py (its "
            "docstring has the command), then pass --tools-dump tools.json.",
            file=sys.stderr,
        )
        return 2
    tools = json.loads(args.tools_dump.read_text())
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
