import uuid

from django.db import models


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


class AgentRun(models.Model):
    """One row per `/api/v2/agent/ask/` invocation.

    Status values:
        running             — loop is still in flight
        done                — clean exit, model produced a final answer
        error               — fatal mid-stream (Gemini failure, etc.)
        step_cap            — hit MAX_STEPS without a final answer
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
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        indexes = [
            models.Index(fields=["team_id", "user_id", "-started_at"]),
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["run", "step_index"]),
        ]
