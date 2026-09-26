"""Test matrix. ``nox -l`` lists sessions; ``nox -s tests`` runs the whole matrix.

Supported: Django 5.2 LTS (Python 3.10-3.14), Django 6.0 and 6.1 (Python 3.12-3.14).
"""

from __future__ import annotations

import nox

nox.options.default_venv_backend = "venv"
nox.options.sessions = ["lint", "typecheck", "tests"]

MATRIX = [
    ("3.10", "5.2"),
    ("3.11", "5.2"),
    ("3.12", "5.2"),
    ("3.13", "5.2"),
    ("3.14", "5.2"),
    ("3.12", "6.0"),
    ("3.13", "6.0"),
    ("3.14", "6.0"),
    ("3.12", "6.1"),
    ("3.13", "6.1"),
    ("3.14", "6.1"),
]
TEST_DEPS = ["pytest>=8", "pytest-django>=4.8", "pytest-cov>=5", "daphne>=4"]


@nox.session
@nox.parametrize("python,django", MATRIX)
def tests(session: nox.Session, django: str) -> None:
    session.install(f"django~={django}.0", *TEST_DEPS)
    session.install("-e", ".[all]")
    session.run("pytest", "--cov", "--cov-report=term-missing:skip-covered",
                "--cov-fail-under=90", *session.posargs)


@nox.session(python="3.12")
def minimal(session: nox.Session) -> None:
    """Core install only: the package must import and pass checks without any extra."""
    session.install(*TEST_DEPS, "-e", ".")
    session.run("python", "-c", "import fingerprint_attendance.conf")
    session.run("pytest", "tests/test_conf.py", "tests/test_models_crypto.py",
                "tests/test_adms_views.py", "-p", "no:cacheprovider")


@nox.session(python="3.12")
def lint(session: nox.Session) -> None:
    session.install("ruff>=0.6")
    session.run("ruff", "check", "src", "tests", "scripts")
    session.run("ruff", "format", "--check", "src", "tests", "scripts", success_codes=[0, 1])


@nox.session(python="3.12")
def typecheck(session: nox.Session) -> None:
    session.install("mypy>=1.11", "django-stubs>=5.0", "djangorestframework-stubs>=3.15",
                    "-e", ".[all]", *TEST_DEPS)
    session.run("mypy")


@nox.session(python="3.12")
def docs(session: nox.Session) -> None:
    session.install("mkdocs>=1.6,<2", "mkdocs-material>=9.5", "-e", ".")
    session.run("python", "scripts/gen_settings_docs.py")
    session.run("mkdocs", "build", "--strict")


@nox.session(python="3.12")
def build(session: nox.Session) -> None:
    session.install("build", "twine")
    session.run("python", "-m", "build")
    session.run("twine", "check", "dist/*")
