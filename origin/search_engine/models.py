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
