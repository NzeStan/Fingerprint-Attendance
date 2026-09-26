# Extending

Every policy is a dotted path. Common extension points:

## Attendance processor

```python
from fingerprint_attendance.processing.default import FirstInLastOutProcessor

class PairedHoursProcessor(FirstInLastOutProcessor):
    version = "paired-1"

    def build_context(self, enrollee, work_date, punches):
        ctx = super().build_context(enrollee, work_date, punches)
        pairs = zip(punches[::2], punches[1::2])
        ctx.worked = sum((o.punched_at - i.punched_at for i, o in pairs), timedelta())
        return ctx

FINGERPRINT_ATTENDANCE["ATTENDANCE_PROCESSOR"] = "myapp.processing.PairedHoursProcessor"
```

Implement `BaseAttendanceProcessor.process_day(enrollee, work_date, create_empty=False)` from
scratch if you want full control. `process_range`, `recompute` and `process_pairs` are
provided. Set `ATTENDANCE_PROCESSOR = None` to disable processing and compute attendance
yourself from `Punch` rows (`Punch.objects.effective()` excludes soft duplicates).

## Sync strategy

```python
from fingerprint_attendance.sync.strategies import BaseSyncStrategy

class SiteStrategy(BaseSyncStrategy):
    def devices_for(self, enrollee):
        return Device.objects.active().filter(location=enrollee.employee.site)

    def enrollees_for(self, device):          # optional, for fast backfills
        return Enrollee.objects.active().filter(employee__site=device.location)
```

A plain function `(enrollee) -> QuerySet[Device]` also works.

## Punch state resolver

```python
from fingerprint_attendance.ingestion.state import BasePunchStateResolver

class WorkCodeResolver(BasePunchStateResolver):
    def resolve(self, candidates, context):
        for c in candidates:
            c.state = "overtime_in" if c.work_code == "9" else "check_in"
```

Set `needs_history = True` to use `context.previous_punch_count(candidate)`.

## Dedupe key

`DEDUPE_KEY_BUILDER = "myapp.dedupe.key"` with `key(candidate) -> str`. The default is
`serial|pin|UTC second`.

## Protocol adapter

```python
from fingerprint_attendance.adms.adapter import ADMSProtocolAdapter

class MyFirmwareAdapter(ADMSProtocolAdapter):
    def render_set_time(self, device, local):
        return f"SET OPTION DateTime={local:%Y%m%d%H%M%S}"

    def parse_attlog_line(self, line):
        return super().parse_attlog_line(line.replace(";", "\t"))
```

Set it globally with `ADMS_PROTOCOL_ADAPTER`, or per device with
`device.options["protocol_adapter"]`. Add commands with
`adms.commands.command_registry.register(CommandSpec(...))`.

## API

Use `SERIALIZER_OVERRIDES`, `VIEWSET_OVERRIDES`, `FILTERSET_OVERRIDES`, per-action
permissions, and the authentication, pagination and throttle settings. See [REST API](api.md).
Subclass the shipped classes in `fingerprint_attendance.api` to keep their behaviour.

## Models

Concrete models are thin subclasses of the abstract bases in
`fingerprint_attendance.models.abstract`. Reuse them for your own tables, and override the
relation fields, whose reverse names must be unique. Swapping the package's concrete models
is not supported; extend with one-to-one profile models instead.

## Everything else

Template storage (`TEMPLATE_STORAGE_BACKEND`, e.g. a vault), PIN generation, day boundaries,
calendar, holiday, leave and schedule providers, the realtime backend and group names, the
webhook sender, and the task backend. See the [configuration reference](configuration.md).

## Service layer

Everything the API does is available as a function in `fingerprint_attendance.services`, so
you can call it from your own views, admin actions or scripts.
