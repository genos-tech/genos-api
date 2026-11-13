#!/bin/sh
# Start command for the `schedule-tick` Railway cron service (MIP-1293).
#
# Railway runs this via the `startCommand` in `.railway/railway.ts`, which REPLACES the image's
# Dockerfile CMD. The CMD does a startup step the AI part of a scheduled report needs and plain
# `manage.py schedule_tick` does NOT repeat:
#
#   Vertex service-account-key decode ($GEMINI_SA_BASE64 -> /tmp/gemini-sa.json). A Railway
#   variable cannot hold a file, so the key is stored as text and written to the file the model
#   client reads ($GEMINI_SERVICE_ACCOUNT_FILE). Without it the model is unreachable and a
#   scheduled report that would use AI is a plain one (`last_result.ai_skipped = "model_unavailable"`).
#   No-op when unset. Keep in sync with the Dockerfile and railway-agent-digest-entrypoint.sh.
#
# No migrations and no opensearch_setup here: backend-django migrates on boot, and a cron racing
# it would be worse than waiting one deploy. The tick reads the database; the report's AI step
# reads and calls the model only when REPORT_AI_SENTENCES is on (operations/SCHEDULER.md).
set -e

if [ -n "$GEMINI_SA_BASE64" ]; then
  mkdir -p /tmp
  echo "$GEMINI_SA_BASE64" | base64 -d > /tmp/gemini-sa.json
  chmod 600 /tmp/gemini-sa.json
fi

# Forward any args (--heartbeat schedule-tick) from the startCommand.
exec python /app/manage.py schedule_tick "$@"
