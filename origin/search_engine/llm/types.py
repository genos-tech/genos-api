"""Provider-neutral types for the agent ↔ ModelClient boundary.

The agent controller (`agent/controller.py`) only knows about these
types — it never imports `google.genai` or `anthropic`. Each adapter
(`gemini_client.GeminiClient`, `claude_client.ClaudeClient`) is
responsible for translating these neutral shapes into its own SDK's
wire format on the way in, and translating the SDK's stream events
back into the (text, FunctionCall) yield pattern on the way out.

This is what makes the abstraction real: if a type leaks from a
specific SDK into the controller, the abstraction is fake. So the
controller's helpers (_user_turn, _assistant_function_call_turn,
_function_response_turn, _build_tool_declarations) build these
neutral shapes — and only these.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


class ModelRefusal(Exception):
    """The provider's safety layer declined to answer this request.

    NOT a failure of the provider and NOT a failure of ours: the call
    succeeded (HTTP 200) and the model or a classifier chose not to
    answer. Adapters raise this after the stream is drained and the
    call's usage is recorded, so the loop can end the run with a
    specific, retry-worthy message instead of a generic "LLM call
    failed" or, worse, an empty answer.

    `category` is the provider's policy category when it reports one
    ("cyber", "bio", ...); it may be None even on a refusal, so nothing
    may branch on it. It is for logs and the spend row only.
    """

    def __init__(self, provider: str, model: str, category: str | None = None):
        self.provider = provider
        self.model = model
        self.category = category
        super().__init__(f"{provider}/{model} declined the request (category={category or 'n/a'})")


#: Gemini finish reasons by which its safety layer ends a call (as opposed to the model
#: finishing). With nothing produced the Gemini adapter raises `ModelRefusal`; after some text the
#: text stays and the loop adds a notice (MIP-1525).
GEMINI_REFUSAL_FINISHES = frozenset(
    {
        "SAFETY",
        "RECITATION",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "IMAGE_SAFETY",
        "IMAGE_PROHIBITED_CONTENT",
    }
)

#: Every provider's refusal-type stop reason, as it lands in `CallUsage.stop_reason`: Gemini's
#: above and OpenAI's `content_filter`. (Claude's `refusal` always raises `ModelRefusal`.)
REFUSAL_STOP_REASONS = GEMINI_REFUSAL_FINISHES | {"content_filter"}

#: Gemini finish reasons for a function call the model wrote wrongly (or should not have made).
_MALFORMED_CALL_FINISHES = frozenset(
    {"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "TOO_MANY_TOOL_CALLS", "MALFORMED_RESPONSE"}
)


def empty_stop_kind(stop_reason: str, block_reason: str, truncated: bool) -> str:
    """Why a call that produced no text and no function call ended, as one of

      "prompt_blocked"  the prompt was blocked before an answer began (never retried)
      "context_full"    Claude `model_context_window_exceeded`: the window is full, not the output
      "thinking_cap"    cut off at the output cap with nothing written: thinking used it up
      "malformed_call"  the model wrote a function call that could not be used
      "other_stop"      the provider stopped for a reason it does not explain (Gemini `OTHER`, ...)
      ""                nothing specific: a plain empty end ("STOP", "end_turn", ...), or a
                        refusal-type stop (the adapter raised `ModelRefusal` for that)

    Called only for a call that yielded nothing (MIP-1525): before this each of them read as
    "Model returned an empty response". Gemini names its finish reasons in capitals; Claude's and
    OpenAI's are lower case, and only Gemini has stops of the last two kinds.
    """
    if block_reason:
        return "prompt_blocked"
    if stop_reason == "model_context_window_exceeded":
        return "context_full"
    if truncated:
        return "thinking_cap"
    if stop_reason in _MALFORMED_CALL_FINISHES:
        return "malformed_call"
    if stop_reason.isupper() and stop_reason not in REFUSAL_STOP_REASONS | {"STOP"}:
        return "other_stop"
    return ""


@dataclass(frozen=True)
class FunctionCall:
    """A function call the model wants to make.

    Yielded by `ModelClient.generate_step` and stored on
    `AgentMessage.function_call` when replaying assistant turns back
    to the model.
    """

    name: str
    args: dict[str, Any]
    # Gemini 3+ returns an opaque "thought signature" alongside each
    # functionCall part that captures the model's internal reasoning
    # state at the point of the call. The client MUST echo it back
    # when replaying the assistant turn (in the same /ask round-trip)
    # or Gemini 3 rejects the request with
    #   400 INVALID_ARGUMENT: Function call is missing a
    #   thought_signature in functionCall parts. ...
    # Older Gemini versions and Claude don't use it, so `None` is the
    # safe default. Stored as raw bytes; the adapter handles wire-
    # format encoding (Google's SDK serialises to base64 internally).
    # See https://ai.google.dev/gemini-api/docs/thought-signatures
    thought_signature: bytes | None = None


@dataclass
class AgentMessage:
    """One turn in the agent's conversation, provider-neutral.

    Exactly one of `text` / `function_call` / `function_response`
    is set, depending on `role`:

      * `role="user"`           → `text` set
      * `role="assistant"`      → `text` set OR `function_call` set
      * `role="tool_response"`  → `function_response_name`
                                  + `function_response` set

    `tool_response` is its own role rather than a flavor of `user`
    because Anthropic and Gemini both encode tool responses
    distinctly from a free-text user message, and adapters need a
    clean signal of which shape to emit.
    """

    role: Literal["user", "assistant", "tool_response"]
    text: str | None = None
    function_call: FunctionCall | None = None
    function_response_name: str | None = None
    function_response: dict[str, Any] | None = None


@dataclass
class CallUsage:
    """Mutable per-call sink the adapter fills at end-of-stream.

    The agent controller passes a FRESH instance into each
    `generate_step` call and reads it back after draining the stream —
    it is never shared across calls or threads, which is what keeps the
    module-singleton SDK clients (Gemini / Anthropic) thread-safe under
    concurrent runs. Fields are provider-neutral token counts; a given
    provider populates the ones it reports and leaves the rest at 0
    (e.g. Gemini has no explicit `cache_write_tokens`, Claude no
    `tool_prompt_tokens`). Raw counts only — cost is derived OFFLINE
    from `model` in the aggregation command, never on the request path.
    """

    provider: str = ""
    model: str = ""
    # Uncached prompt tokens actually sent this call.
    prompt_tokens: int = 0
    # Prompt prefix served from cache (Gemini implicit cache /
    # Anthropic cache_read) — billed at the cheap cached rate.
    cached_tokens: int = 0
    # Freshly-written cache (Anthropic cache_creation, ~1.25x); Gemini's
    # implicit cache has no separate write line so this stays 0 there.
    cache_write_tokens: int = 0
    # Generated output tokens.
    output_tokens: int = 0
    # Reasoning / "thinking" tokens billed separately (Gemini
    # thoughts_token_count); 0 for providers that fold these into output.
    thought_tokens: int = 0
    # Tool-declaration prompt overhead (Gemini tool_use_prompt_token_count).
    tool_prompt_tokens: int = 0
    # Provider-reported grand total when available (else 0; the
    # aggregator can sum the parts).
    total_tokens: int = 0
    # Why the call stopped, as the provider named it ("max_tokens" / "length" /
    # "MAX_TOKENS", ...); "" when the adapter saw none. Observational.
    stop_reason: str = ""
    # True when the provider ended the call because it hit the OUTPUT cap (Claude
    # `max_tokens`, Gemini `MAX_TOKENS`, OpenAI `length`). Whatever the call produced
    # then ends mid-sentence, and a tool call's arguments end mid-value: the controller
    # reads this to avoid putting a cut-off body on an approval card (MIP-905).
    truncated: bool = False
    # True when the call ended cut off AND its LAST function call may be incomplete: the SDK
    # hands back whatever JSON had arrived (Claude `max_tokens`, OpenAI `length` with arguments
    # that do not parse). The controller then treats that call like a cut-off write, whatever
    # tool it is, instead of running it with half its arguments (MIP-1479). Gemini hands function
    # calls over whole, so it never sets this.
    partial_call: bool = False
    # Why the PROMPT was blocked before any answer began (Gemini `prompt_feedback.block_reason`:
    # "SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "OTHER", ...); "" when it was not. The call
    # then has no candidates, so `stop_reason` is empty and the loop would have called it an
    # empty response (MIP-1525).
    block_reason: str = ""
    # The output cap the call actually ran under (tokens), as the adapter sent it; 0 when it
    # sent none and the provider's own model maximum applied (Gemini without a cap). The
    # controller sizes a retry of a cut-off write from this, never below it.
    output_cap: int = 0


@dataclass(frozen=True)
class GenerationParams:
    """Per-call generation knobs, threaded through `generate_step`.

    A dataclass rather than a bare int so future knobs (temperature,
    thinking budget) extend it without touching every adapter
    signature again. `None` anywhere — including the whole object —
    means "the adapter's existing default behavior", which keeps every
    legacy call site byte-identical: adapters MUST treat
    `params=None` exactly like the parameter not existing.

    `max_output_tokens`: per-call output ceiling. Providers differ on
    the default it replaces (claude/openai have env-configured caps;
    gemini is uncapped today), so adapters resolve
    `params.max_output_tokens or <their existing default>`.

    `thinking_budget`: max thinking tokens for this call. Thinking is
    billed at the OUTPUT rate and the anatomy report measured it at
    ~12% of a medium request's cost — with the latency to match. None =
    the provider's default behavior (dynamic thinking); 0 = thinking
    off. **Gemini only for now**: the Claude and OpenAI adapters ignore
    it deliberately — OpenAI already pins `reasoning_effort` on
    tool-carrying calls for API-compatibility reasons (see
    openai_client), and Claude's thinking is a separate experiment —
    so a profile carrying a budget stays safe to resolve on any
    provider.
    """

    max_output_tokens: int | None = None
    thinking_budget: int | None = None


@dataclass(frozen=True)
class ToolDeclaration:
    """A tool the model may call.

    `parameters_schema` is JSON Schema (the same shape both Gemini and
    Anthropic accept — neither needs a separate type). Adapters
    translate this into their SDK's tool-declaration object.
    """

    name: str
    description: str
    parameters_schema: dict[str, Any]
