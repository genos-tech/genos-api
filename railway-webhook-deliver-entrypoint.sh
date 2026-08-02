#!/bin/sh
# Start command for the `webhook-deliver` Railway cron service.
#
# Railway runs this via the `startCommand` override in
# `railway-webhook-deliver.toml`, which REPLACES the image's Dockerfile
# CMD — so anything that CMD did (Vertex SA decode, opensearch_setup) is
# skipped here, which is correct: this cron signs and POSTs, it never
# calls a model or touches the index. No migrations either; backend-django
# migrates on boot and a cron racing it would be worse than waiting one
# deploy.
set -e

# Forward any args (e.g. --limit 50) from the toml startCommand.
exec python /app/manage.py webhook_deliver_tick "$@"
