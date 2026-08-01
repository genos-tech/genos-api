#!/bin/sh
# Start command for the `email-notify` Railway cron service.
#
# Railway runs this via the `startCommand` override in
# `railway-email-notify.toml`, which REPLACES the image's Dockerfile CMD.
# Unlike the agent-digest entrypoint there is NO Vertex SA decode here —
# this cron renders and sends email, it never calls a model. No
# migrations either: backend-django migrates on boot and a cron racing
# it would be worse than waiting one deploy.
set -e

# Forward any args (e.g. --limit 50) from the toml startCommand.
exec python /app/manage.py email_notify_tick "$@"
