# Release checklist

1. `nox` is green (lint, mypy, full matrix, coverage ≥ 90%).
2. `python scripts/gen_settings_docs.py` produces no diff.
3. `python -m django makemigrations --check --dry-run` (with `tests.settings`) reports no changes.
4. Bump `__version__` in `src/fingerprint_attendance/__init__.py`.
5. Move "Unreleased" entries in `CHANGELOG.md` under the new version and date (copy it to
   `docs/changelog.md`). Add upgrade
   notes to `docs/upgrading.md`.
6. `nox -s build` (wheel and sdist pass `twine check`). Inspect the wheel: it contains
   migrations and `py.typed`, and no tests or example project.
7. Commit, then tag `vX.Y.Z` and push the tag. The `Release` workflow checks the tag matches
   the version, builds, and publishes to PyPI with **trusted publishing** (configure the PyPI
   project's trusted publisher: repository, `release.yml`, environment `pypi`). Docs are then
   deployed to GitHub Pages.
8. Create the GitHub release from the changelog entry.
