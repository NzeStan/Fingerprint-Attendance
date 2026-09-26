# System checks

Run `python manage.py check`. IDs are stable.

| ID | Meaning |
|---|---|
| `fpa.E000` | `FINGERPRINT_ATTENDANCE` is not a dict |
| `fpa.E001` | a dotted-path setting cannot be imported |
| `fpa.E002` | encryption enabled but `TEMPLATE_ENCRYPTION_KEYS` is empty |
| `fpa.E003` | an encryption key is not a valid Fernet key |
| `fpa.E004` | `USE_TZ` is False |
| `fpa.E005` | `EMPLOYEE_MODEL` is not an installed model |
| `fpa.E006` | invalid timezone name |
| `fpa.E007` | invalid finger limits |
| `fpa.E008` | `ALLOWED_FINGER_INDEXES` outside 0–9 |
| `fpa.E009` | task backend needs Celery / Django 6 tasks |
| `fpa.E010` | realtime backend needs Channels |
| `fpa.E012` | a value cannot be cast to its type (settings or env) |
| `fpa.E013` | invalid `DEDUPE_WINDOW_ACTION` |
| `fpa.E014` | invalid or unavailable `API_FILTER_BACKEND` |
| `fpa.E015` | URL prefix must not start with `/` and must end with `/` |
| `fpa.E016` | invalid IP/CIDR in an allowlist |
| `fpa.E017` | invalid rate limit |
| `fpa.E018` | invalid weekend days |
| `fpa.E019` | pull mode enabled without pyzk |
| `fpa.E020` | invalid `COMMAND_EXPIRY` duration |
| `fpa.E021` | invalid or unimportable `HOOKS` entry |
| `fpa.E022` | invalid `DEFAULT_SCHEDULE` |
| `fpa.E023` | `PIN_MAX_LENGTH` below 1 |
| `fpa.E024` | `holidays` provider needs the package and `HOLIDAYS_COUNTRY` |
| `fpa.W001` | templates stored unencrypted |
| `fpa.W002` | unknown key in `FINGERPRINT_ATTENDANCE` (typo?) |
| `fpa.W003` | webhooks enabled without a signing secret |
| `fpa.W004` | tokens required together with auto-registration |
