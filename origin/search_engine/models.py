import uuid

from django.db import models
from django.utils import timezone


class RagChunk(models.Model):
    """Per-chunk tracking record.

    One row per OpenSearch document. Lets the indexer detect:
      - changed chunks (text_hash differs → re-embed + upsert)
      - stale chunks (chunk_id exists here but not in current
        regeneration of its parent entity → delete from OpenSearch)
      - re-embed needs after model upgrades (embedding_model differs)
    """

    chunk_id = models.CharField(primary_key=True, max_length=255)
    entity_type = models.CharField(max_length=32, db_index=True)
    entity_id = models.CharField(max_length=128, db_index=True)
    chunk_type = models.CharField(max_length=64)
    team_id = models.UUIDField(db_index=True)

    text_hash = models.CharField(max_length=64)
    source_version = models.BigIntegerField(blank=True, null=True)
    embedding_model = models.CharField(max_length=64)
    index_schema_version = models.CharField(max_length=16, default="v1")

    indexed_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["entity_type", "entity_id"]),
        ]


class AgentSession(models.Model):
    """Groups a sequence of /ask/ calls into one conversation.

    Phase 8 — conversation memory. When the frontend sends
    `session_id` with /ask/, the controller prepends the last
    SESSION_MAX_PRIOR_TURNS (query, final_answer) pairs into the
    model's context window before the current query. This allows
    follow-up references like "show me more about that task".

    TTL is enforced at load time via `last_active_at`. Sessions
    older than SESSION_TTL_MINUTES are silently retired and a new
    one is created. `last_active_at` is updated manually each time
    the session is successfully loaded.
    """

    session_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team_id = models.CharField(max_length=64, db_index=True)
    user_id = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(default=timezone.now)
    # ----- Thread Q&A scope (nullable; only set for sessions bound to
    # a specific chat thread via the "Ask about this thread" feature) -----
    # When all three are populated, the lookup in `_get_or_create_session`
    # treats this as a long-lived per-thread session, ignoring TTL — a
    # user might come back days later and reasonably expect their prior
    # Q&A to still be there. For regular Spotlight sessions, these stay
    # null and the existing TTL window applies.
    chat_type = models.IntegerField(blank=True, null=True)
    # v3 unified chat identity: `chat_id` is the `Channel.id` UUID and
    # `thread_id` is the thread-root `Message.id` UUID (was the legacy
    # integer chat_id / per-channel seq).
    chat_id = models.UUIDField(blank=True, null=True)
    thread_id = models.UUIDField(blank=True, null=True)
    # ----- Note Q&A scope (nullable; mirrors the thread scope above) -----
    # Set when the session is bound to a specific note via the
    # "Ask about this note" feature. Mutually exclusive with the chat
    # scope at the request layer — `_get_or_create_session` rejects
    # both. Same TTL bypass logic as the thread scope (line 132): a
    # user reopening a note Q&A days later should still see their prior
    # turns.
    note_type = models.IntegerField(blank=True, null=True)
    note_id = models.IntegerField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["team_id", "user_id", "-last_active_at"]),
            # Per-thread lookup: "do I have an existing thread session
            # for this user on this thread?" hits this index directly.
            models.Index(fields=["user_id", "chat_type", "chat_id", "thread_id"]),
            # Per-note lookup: same shape as the thread one.
            models.Index(fields=["user_id", "note_type", "note_id"]),
        ]


class AgentRun(models.Model):
    """One row per `/api/v2/agent/ask/` invocation.

    Status values:
        running             — loop is still in flight
        done                — clean exit, model produced a final answer
        error               — fatal mid-stream (Gemini failure, etc.)
        step_cap            — hit MAX_STEPS without a final answer
        cancelled           — the client disconnected mid-stream. NOT an
                              error: nothing failed, the reader left.
                              Kept distinct so cost-per-completed-run can
                              exclude abandoned work instead of counting
                              it as a success. Before this status existed
                              such runs stayed `running` with a NULL
                              finished_at forever, and the worker thread
                              kept billing tokens to a stream nobody was
                              reading.
        awaiting_approval   — Phase 7: paused on a requires_approval
                              tool; resume via POST /api/v2/agent/decide/
        rejected            — Phase 7: user rejected the pending tool
                              call; loop resumed and produced a final
                              answer (terminal state, not a separate
                              flavor of `done`)

    `pending_approval_token` is a one-shot UUID emitted with the
    `tool_call_pending_approval` event and required (along with run_id)
    on the decide endpoint. The server clears it the moment the run
    leaves `awaiting_approval`, so a stale token can't be replayed.
    """

    run_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team_id = models.CharField(max_length=64, db_index=True)
    user_id = models.CharField(max_length=64, db_index=True)
    query = models.TextField()
    status = models.CharField(max_length=20, default="running")
    final_answer_text = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    # Phase 7 — write-tool approval flow.
    pending_approval_token = models.UUIDField(blank=True, null=True)
    # Phase 8 — conversation memory.
    session = models.ForeignKey(
        AgentSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="runs",
    )
    # Structured @/# mentions the user attached to this ask, AFTER
    # DB + ACL resolution (see agent/mentions.py — client labels are
    # never stored). Observational only: debugging, eval sampling,
    # future analytics. Empty list for runs without mentions.
    mentions = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["team_id", "user_id", "-started_at"]),
        ]


class ThreadSummary(models.Model):
    """Cached LLM-generated summary of a chat thread, shared by all members.

    One row per (chat_type, chat_id, thread_id). The "Ask about this thread"
    feature checks the stored fingerprint against the live message fingerprint
    on every request: if they match, the cached summary is returned without
    re-invoking the LLM; if they differ (new message, edit, or delete), the
    summary is regenerated and the row updated in place.

    The summary is also indexed in OpenSearch via the thread_summary chunker
    so that the workspace-wide agent (Spotlight) can retrieve it.

    Fingerprint is `f"{max_thread_message_id}:{count_non_deleted}:{max_ts_updated_at}"`.
    Single-field timestamp keys miss edits and deletes; the three together catch
    inserts (bumps id+count), edits (bumps last_edit_ts), and deletes (drops count).
    """

    id = models.BigAutoField(primary_key=True)
    team_id = models.CharField(max_length=64, db_index=True)
    chat_type = models.IntegerField()  # 1=DM 2=GM 3=PM 4=MDM
    # v3 unified chat identity: `chat_id` is the `Channel.id` UUID and
    # `thread_id` is the thread-root `Message.id` UUID. `last_message_id`
    # stays an integer — it's the max per-channel `Message.seq` (still a
    # monotonic int) used in the fingerprint.
    chat_id = models.UUIDField()
    thread_id = models.UUIDField()
    summary_text = models.TextField()
    last_message_id = models.IntegerField(default=0)
    message_count = models.IntegerField(default=0)
    last_edit_ts = models.DateTimeField(blank=True, null=True)
    model_used = models.CharField(max_length=64, blank=True, default="")
    generated_by_user_id = models.CharField(max_length=64, blank=True, default="")
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["chat_type", "chat_id", "thread_id"],
                name="uq_thread_summary_scope",
            ),
        ]
        indexes = [
            models.Index(fields=["team_id", "ts_updated_at"]),
        ]


class NoteSummary(models.Model):
    """Cached LLM-generated summary of a note, shared by all users with ACL.

    One row per (note_type, note_id). The "Ask about this note" feature
    checks the stored fingerprint against the live note row on every
    request: if they match, the cached summary is returned without
    re-invoking the LLM; if they differ (body edit, title edit), the
    summary is regenerated and the row updated in place.

    The summary is also indexed in OpenSearch via the note_summary
    chunker so that the workspace-wide agent (Spotlight) can retrieve
    it alongside the per-section note_chunker output.

    Fingerprint is `f"{ts_updated.isoformat()}:{body_length}:{title}"`.
    A pure `ts_updated` key would catch any save, but the body-length
    and title are cheap signals that defend against clock skew (clock
    going backwards on a master swap) — they both come from the live
    note row at fingerprint-compute time, so a stale cached row will
    only match when the title AND the body length AND the timestamp
    all match.
    """

    id = models.BigAutoField(primary_key=True)
    team_id = models.CharField(max_length=64, db_index=True)
    note_type = models.IntegerField()  # 1=Personal, 2=Task, 3=Chat
    note_id = models.IntegerField()
    summary_text = models.TextField()
    # Fingerprint inputs (see compute_fingerprint in note_summary.py).
    last_edit_ts = models.DateTimeField(blank=True, null=True)
    body_length = models.IntegerField(default=0)
    title_at_gen = models.CharField(max_length=512, blank=True, default="")
    model_used = models.CharField(max_length=64, blank=True, default="")
    generated_by_user_id = models.CharField(max_length=64, blank=True, default="")
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["note_type", "note_id"],
                name="uq_note_summary_scope",
            ),
        ]
        indexes = [
            models.Index(fields=["team_id", "ts_updated_at"]),
        ]


class AgentStep(models.Model):
    """One row per step within an `AgentRun`.

    A step is either a tool-call (tool_name + arguments_json + result_json
    populated) or a text-only model turn (answer_text populated). The
    `result_json` field holds the full tool output and is intentionally
    server-side only — only `summary` ever reaches the client.
    """

    step_id = models.AutoField(primary_key=True)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="steps")
    step_index = models.IntegerField()
    tool_name = models.CharField(max_length=64, blank=True, default="")
    arguments_json = models.JSONField(blank=True, null=True)
    summary = models.TextField(blank=True, default="")
    result_json = models.JSONField(blank=True, null=True)
    answer_text = models.TextField(blank=True, default="")
    error = models.TextField(blank=True, default="")
    # Tool-execution wall time in milliseconds, for tool-side bottleneck
    # attribution (F-perf). Only meaningful on tool-call steps; 0 on
    # text-only / approval / step-cap / unknown-tool rows where no tool
    # actually ran. LLM-call latency lives on `AgentLlmCall`, not here,
    # because one loop step can issue two model calls (the B3 planning
    # split).
    latency_ms = models.IntegerField(default=0)
    # Opaque Gemini 3+ "thought signature" bytes captured alongside a
    # function_call part. Must be echoed back when the assistant turn is
    # replayed (e.g. after a write-tool approval resume) or Gemini 3
    # rejects with `400 INVALID_ARGUMENT: Function call is missing a
    # thought_signature in functionCall parts.` See
    # `FunctionCall.thought_signature` in llm/types.py.
    thought_signature = models.BinaryField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["run", "step_index"]),
        ]


class AgentLlmCall(models.Model):
    """One row per LLM API call inside an `AgentRun` (F-perf telemetry).

    The gap this fills: `AgentRun.started_at/finished_at` already give
    end-to-end elapsed, and `AgentStep` records which tools ran — but
    nothing captured per-model-call latency or token usage, so we
    couldn't say which LLM round-trip (or which model) drove a run's
    latency and cost. This does, with one row per call.

    Granularity is per-CALL, not per-step, on purpose: the B3 planning
    split fires two calls in a single loop step (a fast planning pass +
    the user-model synthesis), so `step_index` is denormalized here and
    a step can own several of these rows, each with its own `purpose`.

    Capture is cheap (numbers the provider already returns in
    `usage_metadata` + a `monotonic` delta) and happens on the worker
    thread alongside `AgentStep` writes — NEVER on a hot aggregation
    path. Rollups (latency percentiles, token sums, DERIVED cost) are
    computed OFFLINE by the `agent_run_metrics` management command;
    prices are intentionally NOT stored here — only ground-truth tokens
    — because per-token cost changes with the price sheet and the fixed
    infra floor dominates the bill (see LLM_SPEND_ANATOMY).
    """

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="llm_calls")
    # Denormalized from the run so per-team rollups don't need a join
    # (mirrors AgentRunJudgement.team_id / AgentRunFeedback.team_id).
    team_id = models.CharField(max_length=64, db_index=True)
    # The loop step this call belongs to (matches AgentStep.step_index).
    step_index = models.IntegerField(default=0)
    # Role of the call within the loop: "loop" (single-model step),
    # "planning" (B3 fast pass) or "synthesis" (B3 user-model final).
    # Free-form so new call kinds don't need a migration.
    purpose = models.CharField(max_length=32, blank=True, default="")
    provider = models.CharField(max_length=32, blank=True, default="")
    model = models.CharField(max_length=64, blank=True, default="")
    # Wall time of the call as the controller sees it — INCLUDING
    # streaming delivery (the per-chunk emit for loop/synthesis; the
    # planning pass buffers, so if `emit` ever blocks on client
    # backpressure a planning call could read slightly faster than a
    # synthesis call on the same model). emit is a queue append, so this
    # is normally negligible — noted so a bottleneck read isn't misled.
    latency_ms = models.IntegerField(default=0)
    # Provider-neutral token counts (see llm/types.CallUsage). Raw
    # ground truth; cost is derived offline from `model`.
    prompt_tokens = models.IntegerField(default=0)
    cached_tokens = models.IntegerField(default=0)
    cache_write_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    thought_tokens = models.IntegerField(default=0)
    tool_prompt_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["team_id", "-created_at"], name="se_llmcall_team_created_idx"),
            models.Index(fields=["run"], name="se_llmcall_run_idx"),
        ]


class AgentRunJudgement(models.Model):
    """Online quality sample (F2 — SPOTLIGHT_QUALITY_ARCHITECTURE.md §F2).

    One LLM-judge scoring of a completed `AgentRun`, produced
    ASYNCHRONOUSLY by the `agent_judge_sample` management command — never
    on the user request path. Lets us trend *production* faithfulness /
    citation_precision / completeness and alert on drift, which the fixed
    offline eval suite (37 cases) can't see.

    One run is judged at most once: the sampler excludes runs that
    already have a judgement, so re-running the cron is idempotent.
    `error` is non-empty when the judge call itself failed; the three
    scores are 0.0 in that case (mirrors `judge._error_scores`) so a
    failed judgement is recorded rather than silently dropped, and the
    `--report` aggregator filters `error=""` so failures don't drag the
    mean.
    """

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="judgements")
    # Denormalized from the run so per-team rollups don't need a join.
    team_id = models.CharField(max_length=64, db_index=True)
    faithfulness = models.FloatField(default=0.0)
    citation_precision = models.FloatField(default=0.0)
    completeness = models.FloatField(default=0.0)
    # D5 (§4.6) — truthfulness of `[prose](type:id)` link text vs the
    # cited source. NULLABLE, unlike the three axes above: an answer
    # with no link-form citations has nothing to score, and coercing
    # that to 0.0 would poison the production trend. Aggregators must
    # use Avg (skips NULLs), never treat None as zero.
    prose_faithfulness = models.FloatField(blank=True, default=None, null=True)
    notes = models.TextField(blank=True, default="")
    # Which model produced this score — recorded for reproducibility when
    # the active provider/model changes between sampling passes.
    judge_model = models.CharField(max_length=64, blank=True, default="")
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            # Explicit name (not Django's auto hash) so the hand-authored
            # migration is deterministically verifiable via
            # `makemigrations --check`.
            models.Index(fields=["team_id", "-created_at"], name="se_judge_team_created_idx"),
        ]


class AgentRunFeedback(models.Model):
    """Human 👍/👎 on an agent answer (F1 — SPOTLIGHT_QUALITY_ARCHITECTURE.md
    §Q0). The doc flags this signal as "genuinely absent — no model, no
    endpoint, no UI"; it is the reward signal D5 (inline-citation preference),
    D4 (RLHF/DPO), and F3 (bandit config selection) all gate on.

    One row per (run, user): a given user's verdict on a given answer.
    Recorded on POST /api/v2/agent/runs/<run_id>/feedback/ — NEVER on the
    answer path. `rating` is +1 (👍) / -1 (👎); 0 is the explicit "cleared"
    state (the UI toggles a vote back off). `comment` is optional free text
    for a future "tell us why" affordance.
    """

    RATING_UP = 1
    RATING_DOWN = -1
    RATING_CLEARED = 0

    id = models.BigAutoField(primary_key=True)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="feedback")
    # Denormalized from the run so per-team rollups don't need a join
    # (mirrors AgentRunJudgement.team_id).
    team_id = models.CharField(max_length=64, db_index=True)
    user_id = models.CharField(max_length=64, db_index=True)
    rating = models.SmallIntegerField()  # -1 | 0 | +1
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["run", "user_id"], name="uq_agent_feedback_run_user"),
        ]
        indexes = [
            models.Index(fields=["team_id", "-created_at"], name="se_feedback_team_created_idx"),
        ]


# --------------------------------------------------------------------------- #
# Internal AI cost meter — the accounting ledger                              #
# --------------------------------------------------------------------------- #
#
# Two tables, deliberately separate (AI_USAGE_METERING_STRATEGY_REVIEW §2.1):
#
#   AiSpendEvent  — one row per PROVIDER CALL. Cost only, never credits.
#                   This is accounting infrastructure.
#   AiRequestCost — one row per LOGICAL REQUEST. Cost AND the customer
#                   credit charge. This is commercial policy layered on top.
#
# The split is what makes the customer-protection rules expressible rather
# than merely asserted: "charge one logical request, not every provider
# call" means the rollup is the unit; "failed requests are not charged" is a
# constraint on one row; "retries and fallbacks don't increase the charge"
# follows from deriving credits from the capped rollup instead of summing
# events. With one table each of those would be re-derived in every query
# forever.
#
# RELATIONSHIP TO AgentLlmCall: they overlap and neither supersedes the
# other. AgentLlmCall is ask-path LATENCY telemetry, read by
# `agent_run_metrics`; it exists only for runs and only for the agent loop.
# This ledger is the COST source of truth and covers every paid call on
# every surface. A report must never sum both — they describe the same
# calls from two tables.


class AiSpendEvent(models.Model):
    """One paid provider call — LLM, embedding, web search, rerank.

    Written from `llm/spend.py`'s recorder, which the adapters call from
    a `finally`, so a call that streamed output and then died still lands
    here. Best-effort: a failure to record must never break generation.

    Cost is MATERIALIZED here rather than derived at read time, together
    with the rate card that produced it. That is the whole point of the
    accounting layer — a later price change or FX move must never
    retroactively restate what a past request cost. Reports GROUP BY
    `rate_card_version` rather than summing across a mid-period change.

    Money is integer micros/millis, never float: this ledger's stated
    purpose is to reconcile against provider invoices, and float
    summation over a growing table drifts unboundedly and unauditably.
    USD is what invoices are in (reconcile there, exactly); JPY is the
    internal normalization (report there).
    """

    id = models.BigAutoField(primary_key=True)

    # --- attribution -------------------------------------------------
    # The primary key of attribution. Minted by the entry point before
    # anything happens; NOT the AgentRun id, which does not exist yet
    # when the first LLM call of an ask fires and never exists at all
    # for search, summaries, crons or evals.
    request_id = models.UUIDField(db_index=True)
    # Nullable reference, late-bound when there is a run at all.
    run_id = models.UUIDField(blank=True, null=True)
    user_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # Nullable, not blank-default: team is legitimately absent on eval
    # and cron paths, and an empty string silently buckets those with
    # real teams in a group-by.
    team_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    # Where the request came from: ask / search / thread_summary /
    # note_summary / judge / eval / unattributed.
    surface = models.CharField(max_length=32, blank=True, default="")
    # What the call was for within that surface: loop / planning /
    # synthesis / rewrite / rerank / summary / critique / judge.
    purpose = models.CharField(max_length=32, blank=True, default="")
    # Denormalized once per request (never per row — that would put a
    # tier lookup on the request path per call).
    plan = models.CharField(max_length=32, blank=True, default="")
    effort = models.CharField(max_length=16, blank=True, default="")

    # --- what was called ---------------------------------------------
    provider = models.CharField(max_length=32, blank=True, default="")
    model = models.CharField(max_length=64, blank=True, default="")
    # 1 for a first attempt. Providers do not bill rejected 429/5xx
    # retries, so this is expected to stay 1 — it exists so that
    # invariant is demonstrable from the data rather than assumed.
    attempt_no = models.PositiveSmallIntegerField(default=1)

    # --- what it consumed --------------------------------------------
    # Token buckets mirror CallUsage exactly (see llm/types.py).
    prompt_tokens = models.IntegerField(default=0)
    cached_tokens = models.IntegerField(default=0)
    cache_write_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    thought_tokens = models.IntegerField(default=0)
    tool_prompt_tokens = models.IntegerField(default=0)
    # Non-token spend: "search" (Tavily credits), "embed", "rerank".
    # Empty means this row is measured in tokens.
    unit_kind = models.CharField(max_length=16, blank=True, default="")
    units = models.IntegerField(default=0)
    # The provider's OWN stated billable quantity, when it is neither
    # tokens nor `units`. Vertex embeddings report
    # `billable_character_count` while also reporting a token count, and
    # only the invoice settles which one is charged. Recorded so a
    # change of billing unit is recoverable by re-deriving from these
    # rows rather than by making the calls again — the same reason
    # `quoted_max_jpy_milli` is written up front. 0 means "not reported".
    billable_units = models.BigIntegerField(default=0)

    # --- what it cost -------------------------------------------------
    cost_usd_micro = models.BigIntegerField(default=0)
    cost_jpy_milli = models.BigIntegerField(default=0)
    # priced      — a rate existed and cost is real
    # unpriced    — no rate for this model/unit; cost is 0 AND MEANINGLESS.
    #               Must be reported as its own line, never folded into a
    #               total, or a whole provider can vanish from a figure
    #               that still looks about right.
    # incomplete  — the call died before the provider reported usage.
    cost_basis = models.CharField(max_length=16, blank=True, default="priced", db_index=True)
    rate_card_version = models.CharField(max_length=64, blank=True, default="")
    fx_jpy_per_usd = models.FloatField(default=0.0)

    # --- outcome ------------------------------------------------------
    latency_ms = models.IntegerField(default=0)
    error = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["request_id"], name="se_spend_request_idx"),
            models.Index(fields=["user_id", "-created_at"], name="se_spend_user_created_idx"),
            # Invoice reconciliation walks per provider per period.
            models.Index(fields=["provider", "-created_at"], name="se_spend_provider_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.provider}/{self.model} {self.purpose} {self.cost_jpy_milli}m¥"


class AiRequestCost(models.Model):
    """One logical user request — the rollup, and the charging unit.

    Everything a customer is ever told about cost comes from this row,
    never from `AiSpendEvent` directly. That is what makes the
    customer-protection rules structural:

      * charged for ONE logical request, not each provider call
      * a failed request is not charged (`result != success` ⇒ 0)
      * internal retries and provider fallbacks cannot inflate the
        charge, because the charge derives from the capped rollup
      * the charge cannot exceed the maximum quoted BEFORE the request
        started; the excess is `absorbed_jpy_milli`, our cost

    `quoted_max_jpy_milli` is written at request START. It is cheap now
    and retroactively unprovable later — there is no way to reconstruct
    what we would have quoted.

    Credits are SHADOW ONLY at this stage: computed and stored, never
    enforced, never shown. The credit unit is a placeholder until real
    per-request cost is measured.
    """

    RESULT_SUCCESS = "success"
    RESULT_USER_CANCELLATION = "user_cancellation"
    RESULT_PROVIDER_FAILURE = "provider_failure"
    RESULT_APPLICATION_FAILURE = "application_failure"
    RESULT_SAFETY_REFUSAL = "safety_refusal"

    id = models.BigAutoField(primary_key=True)
    request_id = models.UUIDField(unique=True)
    run_id = models.UUIDField(blank=True, null=True)
    user_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    team_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    surface = models.CharField(max_length=32, blank=True, default="")
    plan = models.CharField(max_length=32, blank=True, default="")
    effort = models.CharField(max_length=16, blank=True, default="")
    result = models.CharField(max_length=32, blank=True, default="", db_index=True)

    # --- money --------------------------------------------------------
    # Quoted before any spend; the ceiling we promised not to exceed.
    quoted_max_jpy_milli = models.BigIntegerField(default=0)
    # What it actually cost us (sum of this request's events).
    computed_jpy_milli = models.BigIntegerField(default=0)
    computed_usd_micro = models.BigIntegerField(default=0)
    # V2 layer 3 — the customer-billable slice of `computed`: 0 for a
    # non-billable surface or any non-success result, capped at the
    # policy's per-request max. Derived by the PURE functions in
    # `credits.py` under the policy this row's `credit_policy_version`
    # names, so it can be re-derived — unlike a posted credit charge,
    # which is immutable and lives in its own ledger.
    eligible_jpy_milli = models.BigIntegerField(default=0)
    # What the customer would be charged (0 on a failure; never above
    # the quote).
    charged_jpy_milli = models.BigIntegerField(default=0)
    # computed - charged. Our cost, deliberately taken on.
    absorbed_jpy_milli = models.BigIntegerField(default=0)
    # Shadow only. Milli-credits so a small request is not rounded up
    # to a whole credit — fractional support is a stated requirement.
    shadow_credits_milli = models.BigIntegerField(default=0)
    # The two commercial regimes this row was computed under. Separate
    # on purpose (V2 §5.3): changing how a credit is computed and
    # changing what a plan grants are different commercial acts, and a
    # row must say which of EACH it saw.
    credit_policy_version = models.CharField(max_length=64, blank=True, default="")
    plan_entitlement_version = models.CharField(max_length=64, blank=True, default="")
    rate_card_version = models.CharField(max_length=64, blank=True, default="")

    call_count = models.IntegerField(default=0)
    # True when any event on this request was unpriced — the rollup is
    # then a LOWER BOUND, and the report must not present it as exact.
    has_unpriced = models.BooleanField(default=False)

    started_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["user_id", "-started_at"], name="se_reqcost_user_started_idx"),
            models.Index(fields=["surface", "-started_at"], name="se_reqcost_surface_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.request_id} {self.surface} {self.computed_jpy_milli}m¥"
