def get(first_line):
    if not isinstance(first_line, dict):
        return ""
    block_type = first_line.get("type")

    if block_type == "image":
        name = (first_line.get("props") or {}).get("name")
        return f"Image: {name}" if name else "Image"

    if block_type == "file":
        name = (first_line.get("props") or {}).get("name")
        return f"File: {name}" if name else "File"

    if block_type == "table":
        return "Table"

    if block_type == "divider":
        return ""

    # Content-bearing blocks (paragraph, heading, list items, quote,
    # codeBlock, …) carry an inline-content array. Unknown block types
    # fall through here too — if they happen to have a content array
    # we still extract something useful, otherwise the loop is a no-op
    # and we return "".
    parts = []
    for c in first_line.get("content") or []:
        if not isinstance(c, dict):
            continue
        c_type = c.get("type")
        if c_type == "text":
            text = str(c.get("text", "")).strip()
            if text:
                parts.append(text)
        elif c_type == "mention":
            user = (c.get("props") or {}).get("userName")
            if user:
                parts.append(f"@{user}")
        elif c_type == "link":
            inner = c.get("content") or []
            if inner and isinstance(inner[0], dict) and inner[0].get("text"):
                parts.append(inner[0]["text"])
        # Unknown inline types are skipped silently — they're forward-
        # compatibility cases, not bugs to warn about.

    joined = " ".join(parts)
    if joined:
        return joined

    # Last-resort labels for content-bearing blocks that turned out to
    # be empty (e.g. an empty code block).
    if block_type == "codeBlock":
        return "Code"
    return ""
