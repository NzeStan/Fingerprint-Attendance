# Security and privacy

Fingerprint templates are biometric data, which most data protection laws treat as a special
category of personal data, for example the Nigeria Data Protection Act 2023 and the EU GDPR.
This package provides technical controls that **support** compliance. It does not make you
compliant; you remain responsible for your lawful basis, notices, DPIAs, retention policy and
data subject requests. This page is not legal advice.

## Controls

| Control | How |
|---|---|
| Encryption at rest | Templates are stored as `fernet:<token>` using `MultiFernet(TEMPLATE_ENCRYPTION_KEYS)`. Encryption is on by default; startup fails (`fpa.E002`) without keys. |
| Key rotation | Put the new key first, keep the old one, run `fpa_rotate_template_keys`, then remove the old key. |
| External key custody | `TEMPLATE_STORAGE_BACKEND` can put template bytes in a vault/HSM (`BaseTemplateStorage`). |
| No bytes in transit by default | The API returns template metadata only. `templates/{id}/data/` needs `EXPOSE_TEMPLATE_DATA_IN_API` **and** the `view_template_data` permission, and each read is audited. Commands carrying templates are rendered at send time and stored redacted. |
| Log redaction | All package loggers pass through `RedactingFilter` (`TMP=`, tokens, keys, `Authorization`, long base64). Device events never contain template data. |
| Hashed secrets | Device tokens and agent keys are random, shown once, stored as SHA-256. |
| Consent | `ENROLLMENT_REQUIRE_CONSENT` blocks storage without active consent. `ConsentRecord` keeps version, text reference, method, who captured it and when. Withdrawal deletes templates on the server and queues deletion on **every** active device. |
| Deletion propagation | Deactivation, deletion (including cascades from your employee model) and consent withdrawal queue device deletes. Templates rejected at the device (no consent, unknown PIN) are deleted there too (`DELETE_REJECTED_DEVICE_TEMPLATES`). |
| Retention | `RETENTION_DELETE_TEMPLATES_AFTER_DEACTIVATION`, `RETENTION_PUNCHES_DAYS`, plus logs, commands, deliveries and audit, applied by `fpa_purge_retention` or the periodic job. |
| Audit log | Template reads/stores/deletes, consent changes, enrollee lifecycle, dangerous commands (clear, reboot, raw), key rotation, retention runs and more. |
| PIN hygiene | Retired PINs are never reissued (unless `PIN_REUSE_ALLOWED`), so old punches are never attributed to a new person. |
| Device access | Approval workflow, IP allowlists, trusted proxies, optional tokens, rate limits, size limits (see [ADMS setup](adms-setup.md)). |
| API access | Admin-only by default, model permissions on dangerous actions, and configurable authentication and throttling. |

## Supporting data subject rights

- **Access:** export an enrollee's `ConsentRecord`, `Punch`, `AttendanceDay` and template
  metadata through the API.
- **Erasure:** `services.withdraw_consent` / `services.delete_enrollee` remove biometrics
  everywhere, and `purge_retention` removes the rest per your policy. Punches may be kept
  under a different lawful basis (employment records); decide with your DPO.
- **Minimisation:** fingerprints only. Photos uploaded by devices are discarded, and device
  user passwords are dropped when parsing.

## Operational advice

- Keep `TEMPLATE_ENCRYPTION_KEYS` in a secret manager, not in the repository.
- Put ADMS endpoints behind a reverse proxy with IP restrictions (see
  [deployment](deployment.md)), because many devices only speak plain HTTP.
- Restrict `view_template_data`, `send_raw_command` and `clear_device_data` to very few people.
