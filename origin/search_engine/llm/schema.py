"""Shared JSON-Schema normalization for the non-Gemini adapters.

Tool definitions are written in Gemini's protobuf-derived form, where
`type` values are UPPERCASE ("OBJECT", "STRING", ...). Both Anthropic
and OpenAI accept only standard JSON Schema, which is lowercase.

This lives in its own module — rather than in whichever adapter needed
it first — because `llm/__init__.py` imports adapters lazily so that "a
missing SDK for an unused provider doesn't break the app". Importing the
helper from `claude_client` would have made selecting a GPT model
require the `anthropic` package, quietly breaking that invariant.
"""

from __future__ import annotations

from typing import Any

# JSON Schema type names → lowercase.
_TYPE_MAP = {
    "OBJECT": "object",
    "STRING": "string",
    "INTEGER": "integer",
    "NUMBER": "number",
    "BOOLEAN": "boolean",
    "ARRAY": "array",
    "NULL": "null",
}


def normalize_schema(schema: Any) -> Any:
    """Recursively lowercase any uppercase JSON-Schema `type` values.

    Rewrites a copy; the shared tool definitions are left untouched.
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for k, v in schema.items():
            if k == "type" and isinstance(v, str) and v in _TYPE_MAP:
                out[k] = _TYPE_MAP[v]
            else:
                out[k] = normalize_schema(v)
        return out
    if isinstance(schema, list):
        return [normalize_schema(x) for x in schema]
    return schema
