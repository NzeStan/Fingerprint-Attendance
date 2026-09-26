# Enrollment and the desktop agent

Every enrollment path ends in `services.store_template()`. It validates finger rules, consent,
size and the maximum number of fingers, encrypts and versions the template, and fans it out
to every in-scope device.

| Path | How |
|---|---|
| Walk-up | An admin enrolls a PIN on the device menu. The template arrives in OPERLOG/BIODATA, is matched by PIN, stored, marked present on that device, and queued to the others. Unknown PINs, missing consent or disallowed fingers are rejected, logged, and (with `DELETE_REJECTED_DEVICE_TEMPLATES`) deleted from the device. |
| Remote | `start_enrollment_session(enrollee, device=..., fingers=[6])` queues `add_user`, then `ENROLL_FP`/`ENROLL_BIO` per finger. The session completes when the device uploads every requested finger, and fails if an enroll command fails. |
| Desktop agent | A local app with a USB reader (e.g. ZK9500 plus the vendor SDK) uploads templates through the agent API below. Uploads are staged (encrypted) on the session and become templates only on completion, so a cancelled session never replaces a good template. |

Rules (all settings): `ENROLLMENT_REQUIRE_CONSENT`, `ALLOWED_FINGER_INDEXES`,
`FINGERS_REQUIRED_MIN` (checked when an agent session completes: existing plus new fingers),
`FINGERS_ALLOWED_MAX`, `ENROLLMENT_SESSION_TTL`, `TEMPLATE_MAX_BYTES`.

## Agent contract

Create an agent (API `POST enrollment-agents/`, admin, or `fpa_create_agent "HR desk"
--algorithm 10`). The key is shown **once**; only its SHA-256 is stored.

All requests send `Authorization: Agent <key>` and are rate limited by `AGENT_RATE_LIMIT`.
Base URL: `/api/fingerprint/v1/agent/`.

| Method & path | Body | Response |
|---|---|---|
| `GET me/` | | agent info, allowed fingers, limits, server time |
| `GET sessions/` | | open sessions for this agent |
| `POST sessions/` | `{"enrollee": "<uuid or PIN>", "fingers": [5,6]}` | new session (only with `AGENT_CAN_START_SESSIONS`) |
| `GET sessions/{id}/` | | session |
| `POST sessions/{id}/claim/` | | marks `in_progress` |
| `POST sessions/{id}/templates/` | `{"finger_index": 6, "algorithm_version": "10", "template": "<base64>", "quality": 80}` | `{"captured": [...], "remaining": [...]}` |
| `POST sessions/{id}/complete/` | | `completed`; templates stored and synced |
| `POST sessions/{id}/cancel/` | `{"reason": "..."}` | `cancelled`; staged data discarded |

Errors use the standard format: `{"error": {"code", "message", "details"}}`. Codes include
`session_closed`, `wrong_agent`, `finger_not_requested`, `algorithm_incompatible`,
`consent_required`, `fingers_missing` and `too_few_fingers`.

**Algorithm compatibility:** the upload must match the agent's registered algorithm, and at
least one in-scope device must accept it (`ALGORITHM_COMPATIBILITY_MAP`, e.g.
`{"12": ["10"]}` if your v12 devices accept v10 templates). Devices that cannot use it are
marked `incompatible` in sync status.

## Example client (not part of the package)

```python
"""Minimal desktop enrollment agent. Replace capture() with your reader SDK."""
import base64
import time

import requests

BASE = "https://attendance.example.com/api/fingerprint/v1/agent"
HEADERS = {"Authorization": "Agent fpa_XXXXXXXXXXXXXXXX"}


def capture(finger_index: int) -> tuple[bytes, int]:
    """Call the vendor SDK (e.g. ZKFinger for ZK9500): merge 3 presses into one
    template and return (template_bytes, quality)."""
    raise NotImplementedError


def run() -> None:
    me = requests.get(f"{BASE}/me/", headers=HEADERS, timeout=10).json()
    print("connected as", me["name"])
    while True:
        sessions = requests.get(f"{BASE}/sessions/", headers=HEADERS, timeout=10).json()
        for session in sessions:
            sid = session["id"]
            requests.post(f"{BASE}/sessions/{sid}/claim/", headers=HEADERS, timeout=10)
            print("enrolling", session["enrollee"]["display_name"])
            try:
                for finger in session["fingers_requested"]:
                    data, quality = capture(finger)
                    r = requests.post(f"{BASE}/sessions/{sid}/templates/", headers=HEADERS,
                                      timeout=10, json={
                                          "finger_index": finger,
                                          "algorithm_version": me["algorithm_version"],
                                          "template": base64.b64encode(data).decode(),
                                          "quality": quality})
                    r.raise_for_status()
                requests.post(f"{BASE}/sessions/{sid}/complete/", headers=HEADERS,
                              timeout=10).raise_for_status()
            except Exception as exc:  # noqa: BLE001
                requests.post(f"{BASE}/sessions/{sid}/cancel/", headers=HEADERS, timeout=10,
                              json={"reason": str(exc)[:200]})
        time.sleep(3)


if __name__ == "__main__":
    run()
```

Admins start agent sessions with `POST /api/fingerprint/v1/enrollment-sessions/`
`{"enrollee": "<uuid>", "agent": "<agent uuid>", "fingers": [6, 1]}`.
