"""How to write a page the viewer can show: said once, used by the tool description and the prompt.

The tool answers every save with warnings and a fix for each, but a model that has read this first
writes a page that needs none. ChatGPT and other clients never see Claude skills, so this lives in
the places every MCP client does see: `save_html_note`'s description and the `make_html_note`
prompt (MIP-2214, plan section 7).
"""

from origin.services.html_note_limits import HTML_NOTE_LIMITS

# The size rule quotes the plan table, never a number of its own (MIP-2216).
_CORE = HTML_NOTE_LIMITS["core"]

#: The numbered rules, as plain sentences. The viewer shows the page in a sandboxed frame with no
#: network, no storage and no way to navigate, so each rule is "what to do instead".
GENERATION_RULES: tuple[str, ...] = (
    'Write ONE self-contained file: <!doctype html>, <html lang>, <meta charset="utf-8">, '
    '<meta name="viewport" content="width=device-width, initial-scale=1"> and a <title>.',
    "Use NO network: no <script src>, no CDN libraries (Chart.js, Mermaid, Tailwind, D3), no "
    "Google Fonts or other web fonts, no remote images, and no fetch, XMLHttpRequest or "
    "WebSocket. The viewer blocks all of them and the page breaks.",
    "Draw charts and diagrams with inline SVG, or <canvas> and plain JavaScript. Put small "
    "images in as data: URIs. Use the system font stack.",
    "Do not submit forms, call window.open, change location, or use localStorage, "
    "sessionStorage or cookies.",
    "Embed the data in the page (a JSON object in a <script> tag).",
    "Support light and dark themes (prefers-color-scheme), work down to 360 px wide, and give "
    "charts ARIA labels.",
    "Respect prefers-reduced-motion.",
    "Stay well under the plan's page limit "
    f"({_CORE.max_bytes // 1024} KB on Core); 200 KB is a better target.",
    "Pass a 1 to 3 sentence `summary`: it is what a search for the page finds.",
)


def rules_text() -> str:
    """The rules as one numbered paragraph block."""
    return " ".join(f"{i}. {rule}" for i, rule in enumerate(GENERATION_RULES, start=1))
