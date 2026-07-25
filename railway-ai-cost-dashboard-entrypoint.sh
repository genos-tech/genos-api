#!/bin/sh
# Start command for the three `ai-cost-dashboard-*` Railway cron services.
#
# Railway runs this via the `startCommand` override in
# `railway-ai-cost-dashboard-{daily,weekly,monthly}.toml`, which REPLACES
# the image's Dockerfile CMD — so the service-account decode the CMD does
# for the web service has to be repeated here, exactly like the
# reindexer and judge-sampler entrypoints.
#
# ONE script for all three cadences: they differ only in the reporting
# window, which the toml passes as arguments ("$@"). Three near-identical
# scripts would be three places to forget a fix.
#
# Why the SA key matters here: Railway has no GCS FUSE, so the command
# uploads to gs:// in-process and needs credentials. It reuses the SAME
# service account Vertex uses (GEMINI_SERVICE_ACCOUNT_FILE), which must
# hold `roles/storage.objectAdmin` on the target bucket.
set -e

if [ -n "$GEMINI_SA_BASE64" ]; then
  mkdir -p /tmp
  echo "$GEMINI_SA_BASE64" | base64 -d > /tmp/gemini-sa.json
  chmod 600 /tmp/gemini-sa.json
fi

# The destination lives in the environment, not in the toml: the bucket
# differs per environment, and config-as-code is committed to the repo.
# Fail LOUDLY rather than defaulting — a cron that silently writes its
# only output somewhere unexpected is worse than one that does not run.
if [ -z "$AI_COST_ARCHIVE_URI" ]; then
  echo "AI_COST_ARCHIVE_URI is not set on this service." >&2
  echo "Set it to this cadence's destination, e.g." >&2
  echo "  gs://<project>-ai-cost-reports/daily" >&2
  exit 1
fi

exec python /app/manage.py ai_cost_dashboard \
  "$@" --by-user --archive-dir "$AI_COST_ARCHIVE_URI"
