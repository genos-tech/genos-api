#!/usr/bin/env bash
# usage: retest_migration_prs.sh <base branch>
# Re-tests every open PR into <base>, merged into <base> as it is now, and posts the
# `migration-leaf` status on each PR's head (MIP-1098).
#
# A PR that adds or changes a migration is merged into <base> and its migration graph checked.
# A PR that touches none can't create a second leaf, so it is posted green without a merge: that
# also gives a PR that was opened before the guard existed (and so has no status) the one it
# needs, since the status is a required check.
set -u
base="$1"
git fetch -q origin "$base"
prs=$(gh pr list --base "$base" --state open --limit 200 \
  --json number,headRefOid,files \
  --jq '.[] | "\(.number) \(.headRefOid) \(any(.files[]?; .path | test("/migrations/[0-9]")))"')
if [ -z "$prs" ]; then echo "no open PR into $base"; exit 0; fi

status=0
while read -r number sha touches; do
  [ -z "$number" ] && continue
  if [ "$touches" != "true" ]; then
    printf '#%s %s\nno migrations in this PR\n' "$number" "${sha:0:9}"
    bash scripts/post_migration_status.sh "$sha" 0 "No migrations in this PR."
    continue
  fi
  work=$(mktemp -d)
  git worktree add -q --detach "$work" "origin/$base"
  if ! git fetch -q origin "pull/${number}/head"; then
    echo "#${number}: could not fetch its head"; git worktree remove --force "$work"; continue
  fi
  if ! git -C "$work" -c user.name=ci -c user.email=ci@example.invalid merge -q --no-commit --no-ff "$sha" >/dev/null 2>&1; then
    out="CONFLICT: #${number} no longer merges cleanly into ${base}; rebase it first."
    code=1
  else
    out=$(python3 scripts/check_migration_leaves.py "$work" 2>&1); code=$?
  fi
  printf '#%s %s\n%s\n' "$number" "${sha:0:9}" "$out"
  bash scripts/post_migration_status.sh "$sha" "$code" "$out"
  [ "$code" -ne 0 ] && status=1
  git worktree remove --force "$work"
done <<< "$prs"
# A red PR is reported through its status, not by failing the push run of an unrelated merge.
exit 0
