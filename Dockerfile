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
# Extra packages not yet tracked in the root Pipfile.lock.
# Add them here until the lock file is regenerated.
RUN pip install tavily-python

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
CMD ["sh", "-c", "if [ -n \"$GEMINI_SA_BASE64\" ]; then mkdir -p /tmp && echo \"$GEMINI_SA_BASE64\" | base64 -d > /tmp/gemini-sa.json && chmod 600 /tmp/gemini-sa.json; fi && python manage.py collectstatic --noinput && python manage.py migrate --noinput && gunicorn apis.wsgi:application --bind 0.0.0.0:$PORT --workers 3"]
