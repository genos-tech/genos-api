#!/bin/sh
# Start command for the `agent-judge-sampler` Railway cron service.
#
# Railway runs this via the `startCommand` override in
# `railway-judge-sampler.toml`, which REPLACES the image's Dockerfile CMD.
# The CMD does a startup step the judge needs that the plain
# `manage.py agent_judge_sample` command does NOT repeat, so it has to be
# done here or the judge silently mis-authenticates:
#
#   Vertex service-account-key decode ($GEMINI_SA_BASE64 ->
#   /tmp/gemini-sa.json). backend-django runs Gemini via Vertex
#   (GEMINI_USE_VERTEX=true, GEMINI_SERVICE_ACCOUNT_FILE=/tmp/gemini-sa.json),
#   and the file is written by the Dockerfile CMD. Without this decode the
#   file is absent; the SDK then falls back to the AI-Studio API-key path
#   and every judge call 400s with `API_KEY_INVALID`. No-op when unset
#   (an operator using a plain GEMINI_API_KEY needs no key file). Keep in
#   sync with the Dockerfile and railway-reindexer-entrypoint.sh.
#
# Unlike the reindexer this job does NOT touch OpenSearch (sources are
# reconstructed from persisted AgentStep rows), so there is no
# opensearch_setup step here.
set -e

if [ -n "$GEMINI_SA_BASE64" ]; then
  mkdir -p /tmp
  echo "$GEMINI_SA_BASE64" | base64 -d > /tmp/gemini-sa.json
  chmod 600 /tmp/gemini-sa.json
fi

# Forward any args (e.g. --limit 50) from the toml startCommand.
exec python /app/manage.py agent_judge_sample "$@"
