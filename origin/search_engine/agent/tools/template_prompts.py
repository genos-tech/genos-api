"""Template prompts nobody filled in are not content (MIP-825).

The frontend's body templates (`genos-frontend` `taskTemplates.ts`) are real
blocks: each prompt ("Why does this matter? What problem are we solving?") is
a paragraph or bullet in italic gray. A task whose author never wrote in a
section is stored with the prompt still in it, so `content_text` and
`content_markdown` carried instructions that read as content (KDS-332, -333,
-489 were mostly that).

`without_untouched_prompts` drops them from what a tool READS. Stored data is
not rewritten, so nothing can be lost, and a prompt the author is about to fill
in still shows in the editor.

A block is an untouched prompt only if its text is exactly one of the prompts
below AND it still has the prompt styling. Typing into a prompt replaces its
text; a line the author wrote that repeats a prompt has no such styling, so
both are kept. A heading left with nothing under it goes too.

KEEP THIS LIST IN STEP with `taskTemplates.ts` (a template gaining a prompt
needs its text here, or that prompt keeps being read as content; a prompt
that is missing here is merely shown, never wrongly hidden).
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.text_extraction import _top_level_blocks, extract_text

PROMPTS: frozenset[str] = frozenset(
    {
        # default
        "One or two lines on what this task delivers.",
        "Why does this matter? What problem are we solving?",
        "First condition that must be true when this is done.",
        "Second condition…",
        "Third condition…",
        "Anything else worth pinning here.",
        # bug
        "One-line description of the bug.",
        "Go to …",
        "Click on …",
        "Observe that …",
        "What should happen?",
        "What actually happens? Include error messages, screenshots.",
        "Browser, OS, app version, user, team, anything that narrows it down.",
        # spike
        "What are we trying to learn or decide?",
        "How long are we willing to spend on this?",
        "What do we currently believe is true?",
        "Where to look first…",
        "People to talk to…",
        "Experiments / prototypes to try…",
        "Fill in as you learn — link out to docs, PRs, threads.",
        "Explicitly things we are NOT answering here.",
        # milestone
        "What outcome does this milestone deliver, and for whom?",
        "Measurable signal #1 that we hit the goal.",
        "Measurable signal #2.",
        "Measurable signal #3.",
        "Workstream / feature 1",
        "Workstream / feature 2",
        "Thing we are explicitly NOT doing.",
        "What could derail this? Who/what are we waiting on?",
        "Which task do we take first, and what comes next?",
        "Step 1: the first task.",
        "Step 2: what it unblocks. Note tasks that can run in parallel.",
        "Gates, when each parent task closes, and the critical path.",
    }
)


def _is_untouched_prompt(block: Any) -> bool:
    if not isinstance(block, dict) or block.get("type") not in ("paragraph", "bulletListItem"):
        return False
    if block.get("children"):
        return False
    content = block.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return False
    run = content[0]
    if not isinstance(run, dict) or run.get("type") != "text":
        return False
    styles = run.get("styles") or {}
    return (
        styles.get("italic") is True
        and styles.get("textColor") == "gray"
        and run.get("text") in PROMPTS
    )


def _is_blank(block: Any) -> bool:
    if not isinstance(block, dict) or block.get("type") != "paragraph" or block.get("children"):
        return False
    content = block.get("content")
    if not isinstance(content, list):
        return True
    return all(
        isinstance(r, dict) and r.get("type") == "text" and not r.get("text") for r in content
    )


def without_untouched_prompts(body: Any) -> Any:
    """`body` with the untouched template prompts removed (and any heading
    they leave empty). A body that isn't a block list is returned unchanged."""
    blocks = _top_level_blocks(body)
    if not blocks or not any(_is_untouched_prompt(b) for b in blocks):
        return body

    out: list[Any] = []
    heading: Any = None
    section: list[Any] = []
    has_content = False

    def flush() -> None:
        nonlocal heading, section, has_content
        if heading is not None and not has_content:
            pass  # nothing under it: drop the heading and its blank separators
        else:
            if heading is not None:
                out.append(heading)
            out.extend(section)
        heading, section, has_content = None, [], False

    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "heading":
            flush()
            heading = block
            continue
        if _is_untouched_prompt(block):
            continue
        if heading is None:
            out.append(block)
            continue
        section.append(block)
        if not _is_blank(block):
            has_content = True
    flush()
    return out


def has_written_body(body: Any) -> bool:
    """True when `body` says something besides untouched template prompts.

    The test behind "does this milestone have a goal" (MIP-799): a body that
    is empty, or only the unmodified template, has no goal. Deliberately not
    a check of the "Goal" section, which breaks on custom templates and
    renamed headings.
    """
    return bool(extract_text(without_untouched_prompts(body)).strip())


def is_untouched_template(body: Any) -> bool:
    """True when `body` is the template with no prompt filled in: it has at
    least one untouched prompt and nothing else the author wrote. An empty
    body is not "a template", so it is not matched (nothing to clean)."""
    blocks = _top_level_blocks(body)
    if not blocks or not any(_is_untouched_prompt(b) for b in blocks):
        return False
    return not has_written_body(body)
