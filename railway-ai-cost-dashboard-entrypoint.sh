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

# --- Service account: decode, then PROVE it is usable -------------------- #
# Both checks below are here because both failures actually happened on the
# first deploy of these services, and both surfaced as a traceback from
# inside google-auth after Django had already booted — which tells you
# where the code looked, not which variable is wrong.
if [ -n "$GEMINI_SA_BASE64" ]; then
  mkdir -p /tmp
  if ! printf '%s' "$GEMINI_SA_BASE64" | base64 -d > /tmp/gemini-sa.json 2>/dev/null; then
    echo "GEMINI_SA_BASE64 is not valid base64 on this service." >&2
    exit 1
  fi
  chmod 600 /tmp/gemini-sa.json

  # A TRUNCATED paste is the dangerous case: it is still valid base64, so
  # the decode above succeeds and writes a fragment of the private key.
  # Nothing notices until google-auth tries to parse it as JSON.
  if ! python -c "
import json, sys
with open('/tmp/gemini-sa.json') as f:
    print(json.load(f)['client_email'])
" > /tmp/sa-email 2>/dev/null; then
    echo "GEMINI_SA_BASE64 decoded, but not to a service-account JSON key." >&2
    echo "  decoded $(wc -c < /tmp/gemini-sa.json) bytes starting with:" \
         "$(head -c 12 /tmp/gemini-sa.json)" >&2
    # Quoted delimiter: nothing in here is expanded, which matters because
    # POSIX `echo` would eat the backslash-n in the tr below.
    cat >&2 <<'HINT'
  A truncated paste decodes cleanly and looks exactly like this.
  Re-copy it from a service where it works, WITHOUT pasting:
    railway variables --service backend-django --kv \
      | sed -n 's/^GEMINI_SA_BASE64=//p' | tr -d '\n' \
      | railway variable set --stdin GEMINI_SA_BASE64 --service <this-service>
HINT
    exit 1
  fi
  echo "Service account: $(cat /tmp/sa-email)"
fi

# The path the app will actually read. Printed in [brackets] because the
# first failure here was a stray quote inside the value
# (/tmp/gemini-sa.json") which is invisible in an unquoted error message.
if [ -n "$GEMINI_SERVICE_ACCOUNT_FILE" ] && [ ! -f "$GEMINI_SERVICE_ACCOUNT_FILE" ]; then
  echo "GEMINI_SERVICE_ACCOUNT_FILE=[$GEMINI_SERVICE_ACCOUNT_FILE] does not exist." >&2
  cat >&2 <<'HINT'
  The decode above writes /tmp/gemini-sa.json — check the variable for a
  stray quote or trailing whitespace. The brackets above are there so you
  can see one.
HINT
  exit 1
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
