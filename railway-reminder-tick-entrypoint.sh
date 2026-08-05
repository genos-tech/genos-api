#!/bin/sh
# Start command for the `reminder-tick` Railway cron service.
#
# Railway runs this via the `startCommand` override in
# `railway-reminder-tick.toml`, which REPLACES the image's Dockerfile CMD —
# so anything that CMD did (Vertex SA decode, opensearch_setup) is skipped
# here, which is correct: this cron reads one table and sends push, it never
# calls a model or touches the index. No migrations either; backend-django
# migrates on boot and a cron racing it would be worse than waiting one
# deploy.
set -e

# Forward any args (e.g. --limit 100) from the toml startCommand.
exec python /app/manage.py message_reminder_tick "$@"
