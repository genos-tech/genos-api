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
| `DATABASE_URL`          | **Yes**  | Postgres URL. Without it, settings fall back to an unreachable host. |
| `DJANGO_SETTINGS_MODULE`| —        | `apis.settings` (prod, default) or `apis.settings_test` (tests).   |
| `DJANGO_SECRET_KEY`     | prod     | Django secret key.                                                 |
| `JWT_SECRET_KEY`        | prod     | Shared with genos-sockets / genos-collab for token verification.   |
| `REDIS_URL`             | —        | Cache backend.                                                     |
| `OPENSEARCH_HOST` / `OPENSEARCH_PORT` / `OPENSEARCH_INDEX` | — | Search engine. |
| `EMBEDDING_PROVIDER`    | —        | `openai` / `vertex` — selects the embedding backend.               |
| `LLM_PROVIDER`          | —        | `claude` / `gemini` / `openai` for the agent + judge.              |
| `OPENAI_API_KEY`, `GEMINI_*` / `CLAUDE_API_KEY`, `TAVILY_API_KEY` | — | Provider creds (see `docs/` and the compose env in genos-platform). |

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
export DJANGO_SECRET_KEY=dev-only JWT_SECRET_KEY=dev-only

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

Three additional cron services share the same image, each with its own config
and `startCommand`:

| Config                        | Schedule       | Job                                             |
| ----------------------------- | -------------- | ----------------------------------------------- |
| `railway-reindexer.toml`      | every 10 min   | Incremental OpenSearch reindex                  |
| `railway-judge-sampler.toml`  | hourly         | Online LLM-judge quality sampling               |
| `railway-demo-cleanup.toml`   | daily 03:00 UTC| Delete expired demo users + their data          |

See `docs/RAILWAY_DEPLOY.md` (in genos-platform) for the operator runbook.

## CI

`.github/workflows/ci.yml` runs two jobs on push / PR:

- **backend-django** (required) — system check, migration-drift check, and
  `coverage run manage.py test origin.tests` against a Postgres 15 service.
- **backend-django-lint** (report-only) — `ruff check origin apis`.

<!-- CI skip-path probe: docs-only change; PR closed after verification -->
