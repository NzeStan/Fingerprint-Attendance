# Upgrading

The project follows [semantic versioning](https://semver.org/). Before `1.0`, minor releases
may contain breaking changes, which are always listed in the [changelog](changelog.md) under
**Changed** or **Removed**, with migration steps here.

General procedure:

1. Read the changelog entries between your version and the target.
2. Upgrade in a staging environment and run `python manage.py check` (new settings are
   validated), then `python manage.py migrate`.
3. If the release adds settings, their defaults preserve previous behaviour unless the
   changelog says otherwise.
4. Devices need no changes unless a protocol change is noted.

Encryption keys: never remove a key from `TEMPLATE_ENCRYPTION_KEYS` until
`fpa_rotate_template_keys` has completed.

Changing `EMPLOYEE_MODEL` after the first migration needs a manual migration (like
`AUTH_USER_MODEL`): add a new relation, copy data, and swap.

## 0.1.0

First release.
