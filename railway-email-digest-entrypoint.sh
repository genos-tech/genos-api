#!/bin/sh
# Start command for the `email-digest` Railway cron service.
#
# Railway runs this via the `startCommand` override in
# `railway-email-digest.toml`, which REPLACES the image's Dockerfile CMD.
# No Vertex SA decode (this digest never calls a model — it is the plain
# unread-summary email, not the agent digest) and no migrations
# (backend-django migrates on boot).
set -e

# Forward any args (e.g. --at-hour 8) from the toml startCommand.
exec python /app/manage.py email_digest "$@"
