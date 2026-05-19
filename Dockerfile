FROM python:3.11-slim

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

RUN pip install --upgrade pip pipenv

WORKDIR /app

COPY Pipfile Pipfile.lock ./
RUN pipenv sync
# AI / search packages that are not yet in the root Pipfile.lock.
# These mirror docs/requirements.txt — install them directly until
# the lock file is regenerated to include them.
RUN pip install \
    opensearch-py==3.0.0 \
    openai==2.5.0 \
    google-genai==2.3.0 \
    anthropic==0.102.0 \
    PyYAML==6.0.2 \
    tavily-python \
    'django-anymail[resend]'

COPY backend_django ./backend_django

WORKDIR /app/backend_django

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
CMD ["sh", "-c", "\
  if [ -n \"$GEMINI_SA_BASE64\" ]; then \
    mkdir -p /tmp && echo \"$GEMINI_SA_BASE64\" | base64 -d > /tmp/gemini-sa.json && chmod 600 /tmp/gemini-sa.json; \
  fi && \
  python manage.py collectstatic --noinput && \
  python manage.py migrate --noinput && \
  python manage.py opensearch_setup || echo 'Warning: opensearch_setup failed — search features unavailable until OpenSearch is ready.' && \
  gunicorn apis.wsgi:application \
    --bind 0.0.0.0:$PORT \
    --workers 1 \
    --worker-class gthread \
    --threads 4 \
    --worker-tmp-dir /dev/shm \
    --timeout 30 \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --max-requests 1000 \
    --max-requests-jitter 100"]
