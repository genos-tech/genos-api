#!/bin/sh
# Start command for the `opensearch-reindexer` Railway cron service.
#
# Railway runs this via the `startCommand` override in
# `railway-reindexer.toml`, which REPLACES the image's Dockerfile CMD.
# Two startup steps the CMD does for the web service therefore have to be
# repeated here, or the cron silently breaks indexing:
#
#   1. Vertex service-account-key decode ($GEMINI_SA_BASE64 ->
#      /tmp/gemini-sa.json). Without it, EMBEDDING_PROVIDER=vertex dies
#      with `FileNotFoundError: /tmp/gemini-sa.json`. No-op when unset
#      (the OpenAI / AI-Studio paths need no key file). Keep in sync with
#      the Dockerfile.
#
#   2. `opensearch_setup` BEFORE any write. After an OpenSearch restart
#      the index is gone (ephemeral storage); if the reindex bulk is the
#      first request to touch it, OpenSearch's `action.auto_create_index`
#      silently creates a concrete index (squatting the alias name) with a
#      DYNAMIC mapping — `embedding` as `float` instead of `knn_vector`,
#      keyword fields as `text` — which breaks vector search and ACL
#      filtering and makes every query return nothing. Running setup first
#      guarantees the correct index + alias exist, so the bulk writes into
#      the right place. Idempotent: a no-op once they exist. With `set -e`
#      a setup failure aborts before any write, rather than letting the
#      reindex auto-create a broken index.
set -e

if [ -n "$GEMINI_SA_BASE64" ]; then
  mkdir -p /tmp
  echo "$GEMINI_SA_BASE64" | base64 -d > /tmp/gemini-sa.json
  chmod 600 /tmp/gemini-sa.json
fi

# Ensure the index + alias exist with the correct mapping before writing.
python /app/manage.py opensearch_setup

# Forward any args (e.g. --since-minutes 11) from the toml startCommand.
exec python /app/manage.py opensearch_reindex "$@"
