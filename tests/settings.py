"""Django settings for the package test-suite."""

from __future__ import annotations

import importlib.util
import os
from urllib.parse import urlparse

HAS_SPECTACULAR = importlib.util.find_spec("drf_spectacular") is not None

SECRET_KEY = "tests-only-not-secret"
DEBUG = False
USE_TZ = True
TIME_ZONE = "Africa/Lagos"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "rest_framework",
    *(["drf_spectacular"] if HAS_SPECTACULAR else []),
    "fingerprint_attendance",
    "tests.testapp",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "tests.urls"

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
if os.environ.get("TEST_DATABASE_URL"):  # e.g. postgres://user:pw@host:5432/db (CI)
    _url = urlparse(os.environ["TEST_DATABASE_URL"])
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _url.path.lstrip("/"),
        "USER": _url.username,
        "PASSWORD": _url.password,
        "HOST": _url.hostname,
        "PORT": _url.port or 5432,
    }

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    **({"DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema"} if HAS_SPECTACULAR else {}),
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
}

# Fixed test key (never use in production).
TEST_FERNET_KEY = "yF9nH0N0hVxgE0l3j3Q7k9Z0r7m3m8q2s1u4v6w8x0A="

FINGERPRINT_ATTENDANCE = {
    "EMPLOYEE_MODEL": "testapp.Employee",
    "EMPLOYEE_DISPLAY_FIELD": "full_name",
    "EMPLOYEE_LOOKUP_FIELD": "staff_number",
    "TEMPLATE_ENCRYPTION_KEYS": [TEST_FERNET_KEY],
    "EVENTS_ON_COMMIT": False,
    "TASK_BACKEND_OPTIONS": {"raise_errors": True},
    "ADMS_RATE_LIMIT": None,
    "AGENT_RATE_LIMIT": None,
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
