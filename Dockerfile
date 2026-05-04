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

COPY backend_django ./backend_django

WORKDIR /app/backend_django

EXPOSE 8000

CMD ["sh", "-c", "python manage.py collectstatic --noinput && python manage.py migrate --noinput && gunicorn apis.wsgi:application --bind 0.0.0.0:$PORT --workers 3"]
