#!/bin/sh
# Start command for the `agent-digest` Railway cron service.
#
# Railway runs this via the `startCommand` override in
# `railway-agent-digest.toml`, which REPLACES the image's Dockerfile CMD.
# The CMD does a startup step the digest's agent run needs that plain
# `manage.py agent_digest` does NOT repeat, or generation silently
# mis-authenticates:
#
#   Vertex service-account-key decode ($GEMINI_SA_BASE64 ->
#   /tmp/gemini-sa.json). Without it the Gemini SDK falls back to the
#   AI-Studio API-key path and every call 400s with `API_KEY_INVALID`.
#   No-op when unset. Keep in sync with the Dockerfile and the other
#   railway-*-entrypoint.sh scripts.
#
# The digest reads OpenSearch through the agent's read tools but must
# NEVER create the index — only the reindexer may (see the note in
# railway-opensearch-maintenance-entrypoint.sh). No opensearch_setup here.
set -e

if [ -n "$GEMINI_SA_BASE64" ]; then
  mkdir -p /tmp
  echo "$GEMINI_SA_BASE64" | base64 -d > /tmp/gemini-sa.json
  chmod 600 /tmp/gemini-sa.json
fi

# Forward any args (e.g. --at-hour 8 --limit 200) from the toml startCommand.
exec python /app/manage.py agent_digest "$@"
