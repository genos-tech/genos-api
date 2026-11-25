# python:3.11-slim, pinned by digest (MIP-2269): a re-tagged image cannot change
# what we build on. Dependabot (docker) moves the digest when the tag is rebuilt.
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIPENV_VENV_IN_PROJECT=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Pinned (MIP-727): an unpinned pipenv meant a new release could change
# how the lock installs, or break the build, with no change in this repo.
# Same version as CI and the dev image.
RUN pip install pipenv==2026.7.1

WORKDIR /app

COPY Pipfile Pipfile.lock ./
RUN pipenv sync

COPY . .

EXPOSE 8000

# Vertex AI service-account decoding.
#
# Railway has no host-volume mounts, so we can't reuse the
# docker-compose pattern of mounting a JSON key at /run/secrets/.
# Instead the operator base64-encodes the SA JSON, sets it as
# `GEMINI_SA_BASE64`, and we decode it on container start. The
# `GEMINI_SERVICE_ACCOUNT_FILE` env var should then point at the same
# path (see docs/RAILWAY_DEPLOY.md). Missing var = no-op (the
# AI-Studio-API-key code path runs instead).
#
# `migrate` and `opensearch_setup` are NOT here: on Railway they run in
# backend-django's pre-deploy step (genos-platform `.railway/railway.ts`,
# MIP-938), before the swap. Every second of this CMD is API downtime,
# because the uploads volume makes the old container stop first (MIP-933).
#
# Gunicorn concurrency is env-tunable so it can be sized to the Railway
# instance's memory without a code change. Defaults raised from the old
# 1 worker / 4 threads / 30s: gthread workers are I/O-bound (Postgres +
# OpenSearch), so the app-boot request burst was serializing behind only
# 4 slots. `--threads` is memory-cheap (shared process); raise
# `WEB_CONCURRENCY` (each worker ≈ a full Django process) only if RAM
# headroom allows. `GUNICORN_TIMEOUT` is bumped so a request queued
# behind a slow one during the boot burst isn't killed mid-flight.
# `[::]` listens on IPv6 and IPv4 both: Railway's private network is
# IPv6-only on legacy environments, and genos-collab reaches this service
# there (MIP-621). Quoted so sh doesn't read the brackets as a glob.
#
# `--preload` (MIP-944): the app is imported once, in the master, and a worker is
# a fork. With one worker, `--max-requests` retiring it meant nothing answered
# until a fresh worker had imported Django and every view (about 7 s on Railway,
# a hang rather than an error: the master keeps the socket open). A fork is up in
# a fraction of a second, and `apis/wsgi.py` imports the URLconf in the master so
# the first request does not have to. Costs a few tens of MB more, because the
# master now holds the imported app (the service was capped at 500 MB, 1 GB since
# 2026-11-23). Not removing `--max-requests`: memory climbs from ~150 MB to 400-500 MB
# over 1000 requests (about 0.3 MB a request). MIP-2092: a worker SIGKILLed for memory
# dropped every request in flight (502 under four parallel briefings), so the limit is
# 300: ~150 MB + 300 x 0.3 MB = ~240 MB at the recycle, well under 400 MB, and a recycle is
# cheap with `--preload` (a fork). With the jitter a worker serves 300 to 400 requests.
# MIP-2393: the MIP-2092 change had edited only this comment, the CMD still said 1000. The
# value now lives in ONE place, the two `ENV` lines just below (a Railway variable of the
# same name overrides them), and `origin/tests/test_gunicorn_max_requests.py` fails when the
# CMD, this comment or a doc disagrees with them.
ENV GUNICORN_MAX_REQUESTS=300 \
    GUNICORN_MAX_REQUESTS_JITTER=100
#
# `--keep-alive 0` (MIP-944, second half): a retiring gthread worker stops
# accepting, then waits for every open connection before it exits, and the
# replacement is only started after it has exited. An idle keep-alive connection
# (Railway's proxy holds a pool of them) keeps it waiting: its timeout is only
# checked when some socket event wakes the loop, so locally eight idle connections
# held a retiring worker for the full 30 s `--graceful-timeout`, and in production
# it waited about 4 s until the proxy closed them, with nothing answering new
# connections. With 0 every response closes its connection, so there is nothing
# idle to wait for. A connection is a cheap hop on Railway's private network.
CMD ["sh", "-c", "\
  if [ -n \"$GEMINI_SA_BASE64\" ]; then \
    mkdir -p /tmp && echo \"$GEMINI_SA_BASE64\" | base64 -d > /tmp/gemini-sa.json && chmod 600 /tmp/gemini-sa.json; \
  fi && \
  python manage.py collectstatic --noinput && \
  gunicorn apis.wsgi:application \
    --bind \"[::]:$PORT\" \
    --workers ${WEB_CONCURRENCY:-1} \
    --worker-class gthread \
    --threads ${GUNICORN_THREADS:-8} \
    --worker-tmp-dir /dev/shm \
    --timeout ${GUNICORN_TIMEOUT:-60} \
    --graceful-timeout 30 \
    --keep-alive 0 \
    --preload \
    --max-requests ${GUNICORN_MAX_REQUESTS} \
    --max-requests-jitter ${GUNICORN_MAX_REQUESTS_JITTER}"]
