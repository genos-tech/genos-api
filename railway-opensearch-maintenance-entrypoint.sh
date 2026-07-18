#!/bin/sh
# Start command for the `opensearch-maintenance` Railway cron service.
#
# Railway runs this via the `startCommand` override in
# `railway-opensearch-maintenance.toml`, which REPLACES the image's
# Dockerfile CMD — so the Vertex service-account-key decode the CMD does
# is repeated here ($GEMINI_SA_BASE64 -> /tmp/gemini-sa.json). The
# maintenance command itself makes no LLM/embedding calls today, but the
# decode is kept in sync with the Dockerfile and the other cron
# entrypoints (railway-reindexer-entrypoint.sh,
# railway-judge-sampler-entrypoint.sh) so a future import-time Vertex
# dependency can't silently break only this cron. No-op when unset.
#
# No `opensearch_setup` here, deliberately: maintenance must not create
# or mutate the index. When the alias is missing (e.g. OpenSearch
# restarted on ephemeral storage) the command logs a warning and exits 0
# — the reindexer cron owns recreation.
set -e

if [ -n "$GEMINI_SA_BASE64" ]; then
  mkdir -p /tmp
  echo "$GEMINI_SA_BASE64" | base64 -d > /tmp/gemini-sa.json
  chmod 600 /tmp/gemini-sa.json
fi

# Forward any args (e.g. --min-deleted-ratio 0.1) from the toml startCommand.
exec python /app/manage.py opensearch_maintain "$@"
