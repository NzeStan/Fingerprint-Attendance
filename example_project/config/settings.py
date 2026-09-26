"""Example project settings: custom employee model, Celery, Channels, custom processor,
webhooks. Not production settings."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "example-only-change-me")
DEBUG = True
ALLOWED_HOSTS = ["*"]
USE_TZ = True
TIME_ZONE = "Africa/Lagos"

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    "channels",
    "fingerprint_attendance",
    "hr",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3",
                         "NAME": BASE_DIR / "db.sqlite3"}}
STATIC_URL = "static/"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
}

# Channels: use Redis in production (channels_redis)
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}

# Celery (run: celery -A config worker -B -l info)
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
USE_CELERY = os.environ.get("CELERY_EAGER", "1") != "1"  # CELERY_EAGER=0 to use a real worker

FINGERPRINT_ATTENDANCE = {
    "EMPLOYEE_MODEL": "hr.Employee",
    "EMPLOYEE_LOOKUP_FIELD": "staff_id",
    "EMPLOYEE_DISPLAY_FIELD": "hr.hooks.device_name",
    "PIN_GENERATOR": "employee_field",
    "PIN_SOURCE_FIELD": "badge_number",
    # generate with: python manage.py fpa_generate_key
    "TEMPLATE_ENCRYPTION_KEYS": [os.environ.get(
        "FPA_KEY", "5vUBKyGNxgHWxv5xmmgC5kH9TGJvfQ_0FzP1e2uAdhU=")],  # example only
    # Celery when a broker is running; inline tasks for the zero-infrastructure demo
    "TASK_BACKEND": "celery" if USE_CELERY else "sync",
    "REALTIME_BACKEND": "channels",
    "ATTENDANCE_PROCESSOR": "hr.processing.OvertimeProcessor",
    "DAY_BOUNDARY_RESOLVER": "shift_aware",
    "DEFAULT_SCHEDULE": {"start": "08:00", "end": "17:00", "grace_in_minutes": 10,
                         "break_minutes": 60, "weekdays": [0, 1, 2, 3, 4]},
    "HOLIDAY_PROVIDER_CHAIN": ["database", "holidays"],
    "HOLIDAYS_COUNTRY": "NG",
    "SYNC_STRATEGY": "groups",
    "SYNC_UNGROUPED_TO_ALL": True,
    "WEBHOOKS_ENABLED": True,
    "WEBHOOK_SIGNING_SECRET": os.environ.get("FPA_WEBHOOK_SECRET", "example-secret"),
    "HOOKS": {"backlog_synced": ["hr.hooks.log_backlog"]},
    "API_PERMISSION_CLASSES": ["rest_framework.permissions.IsAdminUser"],
}
