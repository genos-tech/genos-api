#!/bin/sh
# Entrypoint for the `ops-alert-tick` cron service (railway-ops-alert-tick.toml).
set -e
exec python /app/manage.py ops_alert_tick "$@"
