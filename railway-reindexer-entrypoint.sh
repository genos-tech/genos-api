#!/bin/sh
# Start command for the `opensearch-reindexer` Railway cron service.
#
# Railway runs this via the `startCommand` override in
# `railway-reindexer.toml`, which REPLACES the image's Dockerfile CMD.
# The CMD is where the web service decodes the Vertex AI service-account
# key from $GEMINI_SA_BASE64 to /tmp/gemini-sa.json — so a cron that
# overrides the CMD never gets that file, and EMBEDDING_PROVIDER=vertex
# then dies with `FileNotFoundError: /tmp/gemini-sa.json`. Repeat the
# decode here so Vertex embedding works in the cron too.
#
# Keep this decode in sync with backend_django/Dockerfile. It is a no-op
# when $GEMINI_SA_BASE64 is unset (the OpenAI / AI-Studio paths need no
# key file), so it is safe regardless of EMBEDDING_PROVIDER.
set -e

if [ -n "$GEMINI_SA_BASE64" ]; then
  mkdir -p /tmp
  echo "$GEMINI_SA_BASE64" | base64 -d > /tmp/gemini-sa.json
  chmod 600 /tmp/gemini-sa.json
fi

# Forward any args (e.g. --since-minutes 11) from the toml startCommand.
exec python /app/backend_django/manage.py opensearch_reindex "$@"
