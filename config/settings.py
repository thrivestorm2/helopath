"""Django settings for helopath (Air Medical Tracker, Phase 1)."""
import hashlib
import hmac
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

if os.environ.get("HELOPATH_SECRET_KEY"):
    SECRET_KEY = os.environ["HELOPATH_SECRET_KEY"]
elif os.environ.get("DATABASE_URL"):
    # On App Platform, derive a stable key from the database credentials (already a secret) so
    # there is no extra variable to manage. Rotating the database password logs everyone out.
    SECRET_KEY = hmac.new(os.environ["DATABASE_URL"].encode(), b"helopath-django-secret-v1", hashlib.sha256).hexdigest()
else:
    SECRET_KEY = "dev-insecure-change-me"
DEBUG = os.environ.get("HELOPATH_DEBUG", "1") == "1"
ALLOWED_HOSTS = [h for h in os.environ.get("HELOPATH_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("HELOPATH_CSRF_ORIGINS", "").split(",") if o]
# DigitalOcean App Platform sets APP_DOMAIN (e.g. helopath-abc12.ondigitalocean.app).
if os.environ.get("APP_DOMAIN"):
    ALLOWED_HOSTS.append(os.environ["APP_DOMAIN"])
    CSRF_TRUSTED_ORIGINS.append(f"https://{os.environ['APP_DOMAIN']}")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "import_export",
    "tracker",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Guardrail 1: every page requires login except views marked login_not_required.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "tracker.context_processors.roles",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATA_DIR = Path(os.environ.get("HELOPATH_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

if os.environ.get("DATABASE_URL"):
    # Production (App Platform managed PostgreSQL).
    DATABASES = {"default": dj_database_url.parse(os.environ["DATABASE_URL"], conn_max_age=60, conn_health_checks=True)}
    if os.environ.get("DATABASE_CA_CERT"):
        # DigitalOcean Managed Postgres signs with its own CA (App Platform: ${db.CA_CERT}); verify it.
        _ca = DATA_DIR / "db-ca.crt"
        _ca.write_text(os.environ["DATABASE_CA_CERT"])
        DATABASES["default"].setdefault("OPTIONS", {}).update(sslmode="verify-full", sslrootcert=str(_ca))
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("HELOPATH_DB", DATA_DIR / "helopath.sqlite3"),
            "OPTIONS": {
                # WAL lets the web workers read while the background job writes.
                "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;",
                "transaction_mode": "IMMEDIATE",
                "timeout": 30,
            },
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Chicago"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATIC_ROOT.mkdir(exist_ok=True)  # whitenoise warns if it is missing
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
                    if not DEBUG else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 3600

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {"tracker": {"handlers": ["console"], "level": "INFO"}},
}

# All inference thresholds live in one module (spec section 11).
from tracker.thresholds import *  # noqa: E402,F401,F403
