# REST API

Mounted at `API_URL_PREFIX` + `v1/` (default `/api/fingerprint/v1/`) under the
`API_URL_NAMESPACE` namespace. Public ids are UUIDs (`id`). Errors always look like:

```json
{"error": {"code": "consent_required", "message": "...", "details": {}}}
```

## Resources

| Path | Methods / actions |
|---|---|
| `devices/` | CRUD; `approve`, `disable`, `sync-status` (GET), `resync` (`{"full": true}`), `set-time`, `reboot`, `info`, `query-users`, `clear-logs`, `clear-data` (`{"confirm": "<serial>"}`), `reupload-logs` (`{"from": ...}`), `send-command`, `issue-token`, `health` (GET), `events` (GET); list action `reconciliation` |
| `device-groups/` | CRUD; `members`, `add-devices`, `remove-devices`, `add-enrollees`, `remove-enrollees` (`{"ids": [...]}`) |
| `enrollees/` | CRUD (`employee` = `EMPLOYEE_LOOKUP_FIELD` value); `activate`, `deactivate`, `fingers`, `sync-status`, `resync`, `consents`, `withdraw-consent` |
| `consents/` | list, create (record consent), `withdraw` |
| `templates/` | list/retrieve/delete (metadata only); `data` (bytes; requires `EXPOSE_TEMPLATE_DATA_IN_API` **and** `view_template_data`); `import` |
| `enrollment-sessions/` | list, create (`device` or `agent` target), `cancel` |
| `enrollment-agents/` | CRUD (create returns the key once); `rotate-key` |
| `agent/...` | the [agent API](agent.md) (`Authorization: Agent <key>`) |
| `commands/` | list/retrieve; `cancel`, `retry` |
| `punches/` | list (cursor paginated), retrieve; `latest?after=<sequence>` (polling), `manual`, `import`, `{id}/adjust` |
| `attendance-days/` | list/retrieve; `recompute` (404 when processing is disabled) |
| `calendar/` | resolved days for an enrollee and date range |
| `holidays/` | CRUD |
| `work-schedules/`, `shift-assignments/` | CRUD (when `SCHEDULE_MODELS_ENABLED`) |
| `webhooks/`, `webhook-deliveries/` | CRUD, `test`, `deliveries`, `redeliver` (when `WEBHOOKS_ENABLED`) |
| `health/` | device and queue health, today's counts |
| `device-events/`, `audit-log/` | read-only logs |

## Filters

Declared once per viewset and served by the built-in backend or by django-filter
(`API_FILTER_BACKEND`). Examples:

- `punches/?enrollee=<id>&from=2026-01-01T00:00:00Z&to=...&flag=late_sync,unknown_pin&source=adms&state=check_in&device=<id>&group=<id>&pin=1001`
- `enrollees/?is_active=true&consent_state=given&group=<id>&search=ada`
- `attendance-days/?from=2026-01-01&to=2026-01-31&status=absent&group=<id>`

Replace a generated FilterSet with `FILTERSET_OVERRIDES = {"punches": "myapp.MyFilterSet"}`.

## Permissions

`API_PERMISSION_CLASSES` defaults to `IsAdminUser`. Override per viewset or per action:

```python
"API_VIEWSET_PERMISSION_CLASSES": {
    "punches": ["rest_framework.permissions.IsAuthenticated"],
    "punches.create_manual": ["myapp.permissions.IsSupervisor"],
}
```

Dangerous actions also require model permissions, which superusers always pass:
`approve_device`, `control_device`, `clear_device_logs`, `clear_device_data`,
`reupload_device_logs`, `send_raw_command`, `view_template_data`, `import_templates`,
`create_manual_punch`, `import_punches`, `adjust_punch`.

## Other overrides

`SERIALIZER_OVERRIDES` (`"devices"` or `"devices.retrieve"`), `VIEWSET_OVERRIDES`,
`API_AUTHENTICATION_CLASSES`, `API_THROTTLE_CLASSES`, `API_PAGINATION_CLASS`,
`PUNCH_PAGINATION_CLASS`, `API_PAGE_SIZE`, `API_EXCEPTION_HANDLER` (`None` keeps DRF's format
while still mapping service errors).

## OpenAPI

Install `[openapi]`, add `drf_spectacular` to `INSTALLED_APPS` and set
`DEFAULT_SCHEMA_CLASS`. The package annotates custom actions and registers the `AgentKey`
security scheme.
