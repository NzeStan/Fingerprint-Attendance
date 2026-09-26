# Testing with the simulator

`fingerprint_attendance.testing` ships a fake ADMS device that behaves like a real terminal.
It keeps local users, templates and a punch log with an upload cursor, speaks HTTP to your
URLconf through the Django test client, and can go offline.

```python
from datetime import datetime
from django.test import Client
from fingerprint_attendance import services
from fingerprint_attendance.models import Device
from fingerprint_attendance.testing import ADMSDeviceSimulator

def test_backlog(db):
    sim = ADMSDeviceSimulator(Client(), serial="SIM1", firmware="biodata")
    sim.handshake()                                   # auto-registers (pending)
    services.approve_device(Device.objects.get(serial_number="SIM1"))

    sim.go_offline()
    for minute in range(0, 600, 5):
        sim.punch("1001", datetime(2026, 1, 5, 8) + timedelta(minutes=minute))
    sim.go_online()                                   # handshake + catch-up upload
    assert Punch.objects.count() == 120

    sim.enroll_at_device("1001", 6, b"...template...")   # walk-up enrollment
    sim.run_commands()                                     # poll getrequest, apply, ack
```

| Method | Does |
|---|---|
| `handshake()` | `GET cdata`; adopts the server cursor (supports forced re-upload) |
| `punch(pin, when, status=, verify=, upload=)` | records locally; uploads immediately if online and realtime |
| `upload_pending(batch_size=, corrupt_line=, max_batches=)` | uploads records after the cursor in batches; stops at the first non-200 |
| `enroll_at_device(pin, finger, data)` | walk-up enrollment (OPERLOG or BIODATA per firmware) |
| `poll()` / `apply(text)` / `ack(results)` / `run_commands()` | command loop; applies commands to local state |
| `go_offline()` / `go_online(catch_up=True)` | connectivity |
| `fail_commands={"DATA": -1}` | force negative return codes |
| `enroll_factory=callable` | template bytes produced when the server triggers remote enrollment |

Firmware profiles: `legacy` (5-field ATTLOG, FP lines), `push2` (push 2.2, FINGERTMP),
`biodata` (push 2.4.1, BIODATA, 10-field ATTLOG).

For pull mode, set `PULL_ADAPTER="fingerprint_attendance.testing.FakePullAdapter"` and drive
`FakePullAdapter.terminals[serial]` (users, templates, punches, `live_queue`,
`fail_connects`).

The package's own suite (`pytest`) has 300+ tests including realistic protocol fixtures in
`tests/fixtures/adms/`.
