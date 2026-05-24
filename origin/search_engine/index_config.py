from origin.search_engine.embeddings import get_active_embedding_dimensions

INDEX_SCHEMA_VERSION = "v1"


def build_index_settings():
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_shards": 1,
                "number_of_replicas": 0,
            }
        },
        "mappings": build_mappings(),
    }


def build_mappings():
    dims = get_active_embedding_dimensions()
    return {
        "properties": {
            "chunk_id": {"type": "keyword"},
            "entity_type": {"type": "keyword"},
            "entity_id": {"type": "keyword"},
            "chunk_type": {"type": "keyword"},
            # Tenant + access control
            "team_id": {"type": "keyword"},
            "acl_user_ids": {"type": "keyword"},
            # Chat-specific identifiers (nullable for non-chat chunks).
            # Stored as keywords so the API can filter/group by them.
            "chat_type": {"type": "keyword"},
            "chat_id": {"type": "keyword"},
            "thread_id": {"type": "keyword"},
            # Task / Note identifiers (nullable per chunk type).
            "task_id": {"type": "keyword"},
            "note_id": {"type": "keyword"},
            "note_type": {"type": "keyword"},
            "project_id": {"type": "keyword"},
            # Searchable text fields
            "title": {"type": "text"},
            "search_text": {"type": "text"},
            "snippet_text": {"type": "text"},
            # Vector for k-NN
            "embedding": {
                "type": "knn_vector",
                "dimension": dims,
                "method": {
                    "name": "hnsw",
                    "space_type": "cosinesimil",
                    "engine": "lucene",
                },
            },
            # Cross-entity relations for future RAG
            "related_entity_ids": {"type": "keyword"},
            # Bookkeeping
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "source_version": {"type": "long"},
            "text_hash": {"type": "keyword"},
            "embedding_model": {"type": "keyword"},
            "index_schema_version": {"type": "keyword"},
        }
    }
