# syntax=docker/dockerfile:1
# One image for both App Platform components (see .do/app.yaml):
#   web    - this CMD: apply migrations, create the first admin (once), apply auto-confirm rules, serve on 8080
#   worker - run_command override: python manage.py run_worker
# The official python image, pulled through Google's Docker Hub mirror to avoid Hub pull rate limits.
FROM mirror.gcr.io/library/python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Static files are built with production settings so the manifest matches what runs.
RUN HELOPATH_DEBUG=0 HELOPATH_SECRET_KEY=build-only-not-used-at-runtime python manage.py collectstatic --noinput -v0 \
 && useradd --create-home app \
 && mkdir -p /app/data \
 && chown -R app /app/data /app/staticfiles

USER app
ENV HELOPATH_DEBUG=0 \
    PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py ensure_admin && python manage.py apply_rules && exec gunicorn config.wsgi:application --workers 2 --bind 0.0.0.0:${PORT} --timeout 60 --max-requests 1000 --access-logfile -"]
