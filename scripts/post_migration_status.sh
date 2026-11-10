#!/usr/bin/env bash
# usage: post_migration_status.sh <sha> <exit code of check_migration_leaves.py> <its output>
# Posts the `migration-leaf` commit status for <sha> (MIP-1098).
set -u
sha="$1"; code="$2"; out="${3:-}"
case "$code" in
  0) state=success; desc="One leaf migration per app." ;;
  1) state=failure
     desc=$(printf '%s' "$out" | grep -m1 '^CONFLICT' || true)
     desc="${desc:-More than one leaf migration: rebase and renumber, or add a merge migration.}" ;;
  *) state=error; desc="Could not read the migrations; see the run." ;;
esac
# A status description is limited to 140 characters.
desc=$(printf '%s' "$desc" | cut -c1-140)
gh api "repos/${GITHUB_REPOSITORY}/statuses/${sha}" \
  -f state="$state" -f context="${CONTEXT:-migration-leaf}" -f description="$desc" \
  -f target_url="${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}" >/dev/null
echo "migration-leaf: $state on ${sha:0:9}: $desc"
