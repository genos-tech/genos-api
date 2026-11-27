# `.railway/`: the cron services, as code (MIP-857)

Railway deprecated Config as Code. A service created after 2026-08-28 cannot use
a `railway-<name>.toml`, and **existing config files stop working on
2026-12-01**. The schedule, start command and restart policy of every cron
service live in `railway.ts` now. The old `railway-<name>.toml` files stay in the
repo until each service has been cut over (below); they are still what Railway
reads until then.

## What this file does and does not own

- It is a **named partial** (`export const partial = "crons"`). A partial owns
  only the resources it declares, so applying it cannot delete the databases or
  the web services. Do not remove that line while the file lists only part of
  the project: an unnamed whole-project file **deletes every resource it omits**.
- It does **not** manage variables. Every variable is `preserve()`: the existing
  value is kept and no secret is written to source. Rotate secrets in Railway as
  before (`genos-docs` `operations/RAILWAY_DEPLOY.md`, MIP-791). A variable you
  **add** to a cron must also be listed in `railway.ts`, or `plan` refuses.

## Using it

```bash
cd .railway && npm ci --ignore-scripts      # installs the pinned SDK (railway@3.16.0)
cd ..                                       # the repo root must be the linked Railway project
railway config plan                         # read-only: what would change
railway config apply                        # shows the plan again and asks you to confirm
```

**"requires Railway CLI 5.42.1 or newer" with a new CLI** is not about the
version. The SDK (3.16.0, and still 3.19.1) runs `$_ --version`, and under a
wrapper such as `timeout` that is the wrapper. `cli-path.mjs`, imported first by
`railway.ts`, drops such a `$_` so the SDK uses `railway` from `PATH` (MIP-2480).
Run it from the repo root: inside `.railway/` the CLI loads the SDK before
`railway.ts`, and a wrapped command still fails there.

`plan` is always safe. Read it for `to destroy`: it should say **0**.
`apply` marks destructive changes before it asks.

## Moving a service off its config file

Do **apply first, cutover second**, one service at a time:

1. `railway config apply` stores each cron's schedule, command and restart policy
   on the service. Until the cutover the config file still wins, with the same
   values, so nothing changes yet.
2. `railway config migrate cutover --service <name>` stops the service reading its
   config file. It saves a snapshot, so `railway config migrate undo` restores it.
3. Check it: `railway cron list --service <name>` (same cadence, a fresh run), and
   `railway logs --service <name>`.

`ops-alert-tick` and `security-event-prune` never had their own file, so they fall
back to the repo's root `railway.toml` (a web server with a healthcheck). They
need step 2 for the stored `NEVER` restart policy and cron settings to take
effect.

## Adding a cron

Add a `service(...)` block and put it in the `project(...)` list, `plan`, review,
`apply`. Do **not** add a `railway-<name>.toml`: a new service cannot use one.
`retention-purge`, `housekeeping-prune`, `tombstone-prune` and `member-memory-sweep`
(MIP-2251) were added this way. `retention-purge` keeps every period "forever" until
Ken sets `RETENTION_AGENT_RUNS_DAYS` (180), `RETENTION_EMAIL_NOTIFICATION_EVENTS_DAYS` (90)
and `RETENTION_SECURITY_EVENTS_HIGH_VOLUME_DAYS` (90) on the service. Run each command
without `--apply` first and post the counts.

**Not a cron: commands that delete uploaded files** (`purge_deleted_tasks`,
`purge_orphaned_task_files`, `purge_detached_note_attachments`). The media volume is
mounted on `backend-django` only; a cron container has an empty media root, so it
would delete the rows and leave the files behind. Run them by `railway ssh --service
backend-django` until uploads move to object storage.
