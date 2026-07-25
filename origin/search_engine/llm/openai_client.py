"""OpenAI (GPT) adapter for the `ModelClient` interface.

Translates between provider-neutral types (`AgentMessage`,
`FunctionCall`, `ToolDeclaration`) and OpenAI's Chat Completions API.

**Billing note.** Unlike the gemini and claude adapters, this one does
NOT route through Vertex — there is no GPT path on Vertex Model Garden.
It calls api.openai.com directly with `OPENAI_API_KEY` (the same key the
embedder already uses), so GPT asks land on a third billable line
OUTSIDE GCP. See genos-docs operations/LLM_SPEND_MAP.md §1.

Shape differences vs. the other two adapters:

1. **Roles**: OpenAI has `system` / `user` / `assistant` / `tool`. The
   system instruction is the FIRST message rather than a top-level
   parameter (Anthropic) or a `system_instruction` field (Gemini).

2. **Tool-call IDs**: every `tool_calls` entry carries an `id`, and the
   matching `role: "tool"` message must reference it via `tool_call_id`
   — the same correlation problem `claude_client` solves. Our neutral
   `AgentMessage` carries no id, so we synthesize sequential ones
   (`call_0`, `call_1`, ...) by walking history in order: each
   `assistant.function_call` takes the next id and the immediately
   following `tool_response` reuses it. The controller always appends
   those two adjacent, so order-based correlation is reliable.

3. **Streamed tool calls arrive in fragments**: `function.arguments` is
   delivered as a series of partial JSON strings keyed by `index`, so we
   accumulate per index and only emit a `FunctionCall` once the stream
   completes. (Anthropic hands us the assembled block; OpenAI does not.)

4. **JSON Schema types**: tool definitions use Gemini's UPPERCASE form
   ("OBJECT", "STRING", ...). OpenAI accepts only standard lowercase
   JSON Schema, so we share `llm/schema.normalize_schema` with the
   Claude adapter. It lives in its own module rather than in
   `claude_client` so that selecting GPT doesn't drag in `anthropic` —
   `llm/__init__.py` imports adapters lazily precisely so a missing SDK
   for an unused provider can't break the app.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterator

from django.conf import settings
from openai import OpenAI

from origin.search_engine.llm import spend
from origin.search_engine.llm.schema import normalize_schema
from origin.search_engine.llm.types import (
    AgentMessage,
    CallUsage,
    FunctionCall,
    GenerationParams,
    ToolDeclaration,
)

log = logging.getLogger(__name__)

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """Singleton accessor for the OpenAI SDK client."""
    global _client
    if _client is None:
        api_key = settings.SEARCH_ENGINE.get("OPENAI_API_KEY") or ""
        if not api_key:
            raise RuntimeError(
                "An OpenAI model was selected but OPENAI_API_KEY is not set. "
                "Get a key from https://platform.openai.com/api-keys and set "
                "OPENAI_API_KEY in the environment. Note GPT does NOT route "
                "through Vertex — this is a separate OpenAI account and bill."
            )
        _client = OpenAI(api_key=api_key)
    return _client


# --------------------------------------------------------------------------- #
# Usage / cache observability                                                 #
# --------------------------------------------------------------------------- #


def _usage_parts(usage: Any) -> tuple[int, int, int, int]:
    """`(prompt, cached, output, reasoning)` from an OpenAI usage object.

    OpenAI reports `prompt_tokens` as the FULL prompt including any
    cached prefix — the opposite of Anthropic, where `input_tokens` is
    the uncached remainder. The cached count lives in
    `prompt_tokens_details.cached_tokens`. We subtract so the neutral
    sink's `prompt_tokens` means "uncached prompt" on every provider;
    otherwise the offline cost aggregator would double-count the cached
    span at the full input rate.
    """
    total_prompt = int(getattr(usage, "prompt_tokens", None) or 0)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cached = int(getattr(prompt_details, "cached_tokens", None) or 0)
    output = int(getattr(usage, "completion_tokens", None) or 0)
    completion_details = getattr(usage, "completion_tokens_details", None)
    reasoning = int(getattr(completion_details, "reasoning_tokens", None) or 0)
    # Clamp: a provider that ever reports cached > prompt must not make
    # the uncached remainder negative.
    uncached = max(total_prompt - cached, 0)
    return uncached, cached, output, reasoning


def _log_usage(usage: Any, model: str) -> None:
    """Log OpenAI response usage for per-model cost attribution.

    Mirrors `gemini_client._log_usage` / `claude_client._log_usage`,
    including the same `LLM_LOG_USAGE_METADATA` gate (default off) so
    production logs stay quiet unless an operator flips it on.
    """
    if usage is None:
        return
    if not settings.SEARCH_ENGINE.get("LLM_LOG_USAGE_METADATA", False):
        return
    uncached, cached, output, reasoning = _usage_parts(usage)
    billed_input = uncached + cached
    cache_pct = round((cached / billed_input) * 100) if billed_input else 0
    log.info(
        "openai usage model=%s input=%d cached=%d (%d%% cached) output=%d reasoning=%d",
        model,
        uncached,
        cached,
        cache_pct,
        output,
        reasoning,
    )


def _fill_usage_sink(sink: CallUsage, usage: Any, model: str) -> None:
    """Copy OpenAI usage into the neutral per-call sink. Never raises.

    `reasoning_tokens` is a SUBSET of `completion_tokens` on OpenAI (not
    an additional line), so it goes to `thought_tokens` for visibility
    while `output_tokens` keeps the full billed figure. Summing the two
    would over-count output — the priciest term.
    """
    sink.provider = "openai"
    sink.model = model
    if usage is None:
        return
    try:
        uncached, cached, output, reasoning = _usage_parts(usage)
        sink.prompt_tokens = uncached
        sink.cached_tokens = cached
        # OpenAI bills cache reads at a discount but reports no separate
        # "cache write" line, so this stays 0 (same as Gemini).
        sink.cache_write_tokens = 0
        sink.output_tokens = output
        sink.thought_tokens = reasoning
        sink.total_tokens = int(getattr(usage, "total_tokens", None) or 0) or (
            uncached + cached + output
        )
    except Exception:  # noqa: BLE001 — metrics must not break generation
        log.debug("OpenAI usage sink fill failed", exc_info=True)


class OpenAIClient:
    """`ModelClient` adapter backed by OpenAI's Chat Completions API."""

    def generate_step(
        self,
        messages: list[AgentMessage],
        tools: list[ToolDeclaration],
        system_instruction: str,
        *,
        model_override: str | None = None,
        usage_sink: CallUsage | None = None,
        params: GenerationParams | None = None,
    ) -> Iterator[tuple[str | None, FunctionCall | None]]:
        """Stream one model turn against the given history.

        Same contract as `GeminiClient.generate_step` /
        `ClaudeClient.generate_step` — yields `(text_chunk, None)` for
        incremental text and `(None, FunctionCall)` for each tool call.
        """
        sdk_messages = _messages_to_openai(messages, system_instruction)
        sdk_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": normalize_schema(t.parameters_schema),
                },
            }
            for t in tools
        ]

        model = model_override or settings.SEARCH_ENGINE["OPENAI_MODEL"]
        # Cost accounting — see the note in gemini_client. Recorded in a
        # `finally` so a stream that dies part-way through still lands
        # in the ledger. Identity is stamped now, not at fill time: an
        # aborted call never reaches `_fill_usage_sink`, and a row with
        # no provider or model cannot be reconciled against an invoice.
        sink = usage_sink if usage_sink is not None else CallUsage()
        sink.provider = "openai"
        sink.model = model
        started = time.monotonic()
        call_error = ""
        # Per-call cap wins; the env cap stays the fallback so a
        # params-less call is byte-identical to pre-GenerationParams.
        max_tokens = (params.max_output_tokens if params else None) or int(
            settings.SEARCH_ENGINE.get("OPENAI_MAX_TOKENS", 4096)
        )

        create_kwargs: dict[str, Any] = {
            "model": model,
            "messages": sdk_messages,
            # GPT-5 family models are reasoning models: the output cap is
            # `max_completion_tokens` and it covers reasoning tokens too.
            # The older `max_tokens` field is rejected on them.
            "max_completion_tokens": max_tokens,
            "stream": True,
            # Usage is omitted from streamed responses unless asked for.
            # Without this the telemetry sink stays empty and the offline
            # cost report silently under-reports every GPT ask.
            "stream_options": {"include_usage": True},
        }
        if sdk_tools:
            create_kwargs["tools"] = sdk_tools

        # Accumulates streamed tool-call fragments, keyed by the `index`
        # OpenAI assigns within this response. `arguments` arrives as a
        # sequence of partial JSON strings that only parse once joined.
        pending_calls: dict[int, dict[str, str]] = {}
        final_usage: Any = None

        try:
            stream = _get_client().chat.completions.create(**create_kwargs)
            for chunk in stream:
                # The final chunk carries usage and has no choices.
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    final_usage = chunk_usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                if delta is None:
                    continue

                text = getattr(delta, "content", None)
                if text:
                    yield (text, None)

                for tc in getattr(delta, "tool_calls", None) or []:
                    idx = getattr(tc, "index", 0) or 0
                    slot = pending_calls.setdefault(idx, {"name": "", "arguments": ""})
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        if getattr(fn, "name", None):
                            slot["name"] = fn.name
                        if getattr(fn, "arguments", None):
                            slot["arguments"] += fn.arguments

            # Tool calls are emitted only after the stream drains — the
            # argument JSON is not parseable until every fragment has
            # arrived. Ordered by index so multi-call turns keep the
            # order the model produced.
            for idx in sorted(pending_calls):
                slot = pending_calls[idx]
                if not slot["name"]:
                    continue
                yield (None, FunctionCall(name=slot["name"], args=_parse_args(slot["arguments"])))

            # Usage telemetry. Observability only — never break generation.
            try:
                _log_usage(final_usage, model)
                _fill_usage_sink(sink, final_usage, model)
            except Exception:
                log.debug("OpenAI usage logging failed", exc_info=True)
        except BaseException as exc:
            # BaseException so a client disconnect (GeneratorExit) is
            # recorded too — that call was billed like any other.
            call_error = f"{type(exc).__name__}: {exc}"[:200]
            if isinstance(exc, Exception):
                log.exception("OpenAI generate_step failed")
            raise
        finally:
            try:
                spend.record_llm_call(
                    sink,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error=call_error,
                )
            except Exception:  # noqa: BLE001 — accounting never breaks generation
                log.debug("OpenAI spend capture failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Translation helpers                                                         #
# --------------------------------------------------------------------------- #


def _parse_args(raw: str) -> dict[str, Any]:
    """Parse accumulated tool-call argument JSON into a dict.

    Returns `{}` rather than raising on malformed or empty JSON: a
    truncated stream would otherwise take down the whole agent turn, and
    the controller handles a no-arg call far better than an exception.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("OpenAI tool-call arguments were not valid JSON: %r", raw[:200])
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _messages_to_openai(
    messages: list[AgentMessage], system_instruction: str
) -> list[dict[str, Any]]:
    """Translate neutral `AgentMessage`s into Chat Completions turns.

    The system instruction leads the list (OpenAI has no top-level
    system field). Synthesizes `tool_call` ids (`call_0`, `call_1`, ...)
    by walking function-call assistant turns in order and reusing the
    same id for the immediately-following `tool_response` — see the
    module docstring.
    """
    out: list[dict[str, Any]] = []
    if system_instruction:
        out.append({"role": "system", "content": system_instruction})

    next_call_index = 0
    pending_tool_call_id: str | None = None

    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text or ""})
            pending_tool_call_id = None
            continue

        if m.role == "assistant":
            if m.function_call is not None:
                tool_call_id = f"call_{next_call_index}"
                next_call_index += 1
                out.append(
                    {
                        "role": "assistant",
                        # OpenAI requires the key to be present even when
                        # the turn is a pure tool call.
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": m.function_call.name,
                                    "arguments": json.dumps(
                                        dict(m.function_call.args), ensure_ascii=False
                                    ),
                                },
                            }
                        ],
                    }
                )
                pending_tool_call_id = tool_call_id
                continue
            out.append({"role": "assistant", "content": m.text or ""})
            pending_tool_call_id = None
            continue

        if m.role == "tool_response":
            if pending_tool_call_id is None:
                # Defensive: the controller always pairs function-call +
                # tool-response. Synthesize rather than let the SDK
                # reject the whole request, and log so misuse surfaces.
                log.warning(
                    "tool_response without a preceding assistant function_call; "
                    "synthesizing fresh id"
                )
                pending_tool_call_id = f"call_{next_call_index}"
                next_call_index += 1
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": pending_tool_call_id,
                    "content": json.dumps(m.function_response or {}, ensure_ascii=False),
                }
            )
            pending_tool_call_id = None
            continue

        raise ValueError(f"Unknown AgentMessage role: {m.role!r}")

    return out
