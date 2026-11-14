# genos-api

The Django + DRF backend for Genos: REST API, auth, chat/task/note/project data,
and the **Spotlight** agentic-AI + OpenSearch search engine (the `origin` and
`origin.search_engine` apps). Serves the HTTP API consumed by
[genos-frontend](https://github.com/genos-tech/genos-frontend); real-time
messaging and editor collaboration live in the sibling
[genos-sockets](https://github.com/genos-tech/genos-sockets) and
[genos-collab](https://github.com/genos-tech/genos-collab) services.

- **Runtime:** Python 3.11, Django 5, Django REST Framework
- **Server:** gunicorn (`apis.wsgi:application`), gthread workers
- **Data stores:** Postgres, OpenSearch (vector + BM25 search), Redis (cache)
- **AI:** pluggable LLM/embedding providers — Gemini (AI Studio or Vertex),
  Anthropic Claude, OpenAI; Tavily for web search
- **Deps:** `Pipfile` / `Pipfile.lock` (pipenv)

## Layout

| Path                     | What it is                                              |
| ------------------------ | ------------------------------------------------------ |
| `apis/`                  | Django project — `settings.py`, `urls.py`, `wsgi.py`   |
| `apis/settings_test.py`  | Test settings (used by CI and local test runs)         |
| `origin/`                | Main app — models, views, serializers, services, agent |
| `origin/search_engine/`  | Spotlight OpenSearch search engine app                 |
| `origin/agent/`          | Agentic-AI orchestration                               |
| `manage.py`              | Django management entry point                          |
| `Dockerfile`             | `python:3.11-slim` production image                    |
| `railway*.toml`          | Railway service configs (web + three cron services)    |

## Environment variables

`apis/settings.py` reads configuration from the environment. The essentials:

| Variable                | Required | Notes                                                              |
| ----------------------- | -------- | ------------------------------------------------------------------ |
| `DJANGO_DEBUG`          | dev      | Off unless exactly `true`. Local dev sets it; production leaves it unset or `false`. |
| `DATABASE_URL`          | **Yes**  | Postgres URL. With DEBUG off the process refuses to start without it. Outside DEBUG it connects with `sslmode=require` unless the URL names a stricter mode, and refuses `disable` / `allow` / `prefer`; a loopback host is exempt. |
| `DJANGO_SETTINGS_MODULE`| —        | `apis.settings` (prod, default) or `apis.settings_test` (tests).   |
| `DJANGO_SECRET_KEY`     | prod     | Django secret key.                                                 |
| `JWT_SECRET_KEY`        | prod     | Shared with genos-collab (as `JWT_SECRET`) for token verification. genos-sockets has none: it forwards the token here. |
| `ALLOWED_HOSTS`         | web      | The API's public host. Not `*`, not just `localhost`.              |
| `CORS_ALLOWED_ORIGINS`  | web      | The frontend's exact origin(s). No wildcards.                      |
| `FIELD_ENCRYPTION_KEYS` | web, push/webhook crons | Comma-separated Fernet keys, newest first, for OAuth tokens, webhook secrets and push subscription keys at rest. The first encrypts; all decrypt. To rotate: put a new key first, redeploy, run `manage.py rotate_field_keys`, drop the old key. The old single-key name `OAUTH_TOKEN_ENCRYPTION_KEY` is still read when this is unset. reminder-tick, agent-digest (with VAPID set) and webhook-deliver refuse to run without it. |
| `GITHUB_WEBHOOK_SECRET` | legacy   | Verifies only GitHub hooks registered before each hook got its own secret. `manage.py rekey_github_webhooks` moves the rest over and fails while any remain; unset this once it reports none. |
| `REDIS_URL`             | —        | Cache backend. Outside DEBUG, plain `redis://` only to a private host (`*.railway.internal`, a private IP, a compose service name); anything else needs `rediss://`. |
| `COLLAB_INTERNAL_URL`   | —        | genos-collab's http(s) base URL for the task/note body push. Plain `http://` only to a private host. Empty = no push. |
| `COLLAB_INTERNAL_SECRET`| with the above | Signs that push. Same value on genos-collab; must differ from `JWT_SECRET_KEY`. |
| `SOCKETS_INTERNAL_URL`   | —        | genos-sockets' http(s) base URL for the task-comment push (MIP-1348): after a comment written outside the sockets `task_comment` handler commits (agent / MCP comment tools, undo, plain REST), sockets announces it to the project room so other people's open tasks update live. Plain `http://` only to a private host. Empty = no push. |
| `SOCKETS_INTERNAL_SECRET`| with the above | Signs that push (HMAC-SHA256). Same value on genos-sockets; must differ from `JWT_SECRET_KEY` and `COLLAB_INTERNAL_SECRET`. |
| `OPENSEARCH_HOST` / `OPENSEARCH_PORT` / `OPENSEARCH_INDEX` | — | Search engine. |
| `EMBEDDING_PROVIDER`    | —        | `openai` / `vertex` — selects the embedding backend.               |
| `LLM_PROVIDER`          | —        | `claude` / `gemini` / `openai` for the agent + judge.              |
| `OPENAI_API_KEY`, `GEMINI_*` / `CLAUDE_API_KEY`, `TAVILY_API_KEY` | — | Provider creds (see `docs/` and the compose env in genos-platform). |

With `DJANGO_DEBUG` off, every process (web and crons) refuses to start
without real `DJANGO_SECRET_KEY`, `JWT_SECRET_KEY` and `DATABASE_URL`, or on a
plaintext hop the transport rules above refuse, and the web server
(`apis/wsgi.py`) also refuses to start without the four **web** rows. The
crons share the settings but never load wsgi, so they don't need those,
except the crons that decrypt, which check `FIELD_ENCRYPTION_KEYS` themselves.

For the full local set, see the `backend` service env in
`genos-platform/docker/docker-compose.yml`.

## Running locally

The easiest path is the docker-compose stack in
[genos-platform](https://github.com/genos-tech/genos-platform) (brings up
Postgres, OpenSearch, Redis, and this API together). To run the API directly:

```bash
pipenv sync                       # install locked deps
pipenv run pip install -r requirements-dev.txt   # test tooling

export DATABASE_URL=postgres://postgres:postgres@localhost:5432/origin
export DJANGO_DEBUG=true DJANGO_SECRET_KEY=dev-only JWT_SECRET_KEY=dev-only

pipenv run python manage.py migrate
pipenv run python manage.py opensearch_setup      # create index + alias
pipenv run python manage.py runserver 0.0.0.0:8890
```

## Tests, lint

```bash
pipenv run coverage run manage.py test origin.tests --settings=apis.settings_test
pipenv run coverage report
ruff check origin apis            # import sort + pyflakes/pycodestyle (see ruff.toml)
```

## Deployment (Railway)

Built from the root `Dockerfile` (`railway.toml`, `builder = "DOCKERFILE"`). The
web container boots in this order (see the Dockerfile `CMD` — preserve the
sequence): decode the optional Vertex service-account key →
`collectstatic` → `migrate` → `opensearch_setup` → `gunicorn apis.wsgi`.

Additional cron services share the same image, each with its own config and
`startCommand`:

| Config                              | Schedule        | Job                                            |
| ----------------------------------- | --------------- | ---------------------------------------------- |
| `railway-reminder-tick.toml`        | every minute    | Fire due message reminders (push + inbox)      |
| `railway-webhook-deliver.toml`      | every minute    | Drain the outbound webhook outbox              |
| `railway-email-notify.toml`         | every 5 min     | Drain the email notification outbox            |
| `railway-reindexer.toml`            | every 10 min    | Incremental OpenSearch reindex                 |
| `railway-email-digest.toml`         | hourly          | Per-user email digest                          |
| `railway-agent-digest.toml`         | hourly          | Proactive Genos digest (fires at 08:00 local)   |
| `railway-judge-sampler.toml`        | hourly          | Online LLM-judge quality sampling              |
| `railway-demo-cleanup.toml`         | daily 03:00 UTC | Delete expired demo users + their data         |
| `railway-opensearch-maintenance.toml`| daily 05:00 UTC| Index maintenance                              |
| `railway-ai-cost-dashboard-*.toml`  | daily/weekly/monthly | AI cost rollups                           |

The minutely pair is minutely on purpose: a reminder or a webhook that lands
five minutes late has missed the moment it was for.

⚠️ **Each of these is a service somebody has to create** (Railway reads one
config per service), and a cron that was never created fails silently — the
`reminder-tick` omission shipped that way and reminders simply never
arrived. Every row above has a numbered section in the runbook:
`operations/RAILWAY_DEPLOY.md` in **genos-docs** §4.7–§4.19.

## CI

`.github/workflows/ci.yml` runs two jobs on push / PR:

- **backend-django** (required) — system check, migration-drift check, and
  `coverage run manage.py test origin.tests` against a Postgres 15 service.
- **backend-django-lint** (report-only) — `ruff check origin apis`.

### agent-evals: the retrieval gate, and when its billed Vertex run starts

`.github/workflows/agent-evals.yml` runs the **retrieval** suite only (`agent_eval --retrieval`: re-index
the seeded fixture, 75 `search()` cases, one query-rewrite call each; about 5 minutes, a few cents). It
cannot see the agent's prompts, tool declarations, tool results or loop, so those do not start it (a
change to them is checked by the free tests and, for behaviour, by the grouped paid run). It starts:

- on a PR that changes a retrieval file (the list is `RETRIEVAL_PATHS` in `scripts/agent_eval_gate.py`,
  mirrored by the workflow's `paths:`: search, the rewriter, reranker, chunkers, embeddings, ingestion,
  the model client the rewriter calls, the retrieval cases / fixture / runner, `demo_seeder`, `Pipfile*`)
  or on the workflow file itself (an edit of the workflow alone skips the Vertex steps);
- weekly on `main` (Tuesday 02:00 JST), which covers what no path rule does (`origin/models/**`,
  `apis/settings.py`, a model behaving differently);
- by hand (`workflow_dispatch`; `dry_run: true` only prints the decision), or on a PR labelled `run-evals`.

There is no run on a push to `main` and no behaviour smoke. `skip-evals` forces a skip. A cheap `gate` job
(no secrets, no Vertex, no Python dependencies) lists the PR's changed files and runs
`scripts/agent_eval_gate.py`: a changed retrieval file, the `run-evals` label, a dispatch or the weekly
run start the billed steps; a PR that only edits the workflow does not (that is how the workflow is
tested for free). Any failure in the gate fails open (the evals run). Scheduled and manual runs need the
repo variable `RUN_AGENT_EVALS` (or `DEPLOY_TO_GCP`) to be `true`.

What the model reads (prompts, tool declarations, tool results) is guarded by a free test in the normal
CI run instead: `origin.tests.test_agent_fingerprint_golden` fails when any of it changes (re-record
with `manage.py agent_fingerprint --write-golden`).

The pass / mrr / recall floors live only in `scripts/retrieval_gate.py`.
Tests: `origin.tests.test_agent_eval_gate`, `origin.tests.test_retrieval_gate`.
