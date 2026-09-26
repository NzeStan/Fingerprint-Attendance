# ADMS device setup

ADMS (also called "Push", "Cloud Server" or "iclock") lets the device call your server over
HTTP. The device keeps working offline and uploads when it reconnects.

## On the device

Menu names differ by model and firmware. Typical paths:

| Firmware family | Menu |
|---|---|
| Black & white / TFT (iFace, K40, MB-series) | **Menu → Comm → Cloud Server Setting** (or **ADMS**) |
| ZKBioTime-era touch screens (SpeedFace, ProFace) | **COMM → Cloud Server Setting** |

Enter:

| Field | Value |
|---|---|
| Server mode | ADMS |
| Enable domain name | On if you use a hostname (needs DNS set on the device) |
| Server address | your host, e.g. `attendance.example.com` or `192.168.1.10` |
| Server port | the port you expose (80, 8081, ...) |
| Enable proxy server | usually off |
| HTTPS | only if the firmware supports it; see [deployment](deployment.md) |

The device then calls `http://<host>:<port>/iclock/cdata?SN=<serial>&options=all...`. If you
changed `ADMS_URL_PREFIX`, the firmware must support a custom path, or you need a reverse
proxy rewrite (see deployment).

Also set on the device:

- **Date/Time:** correct time and timezone. Server-side drift detection and optional
  auto-correction help, but start right.
- **Device ID / PINs:** PINs must match `PIN_MAX_LENGTH` / `PIN_NUMERIC_ONLY`.

## On the server

1. The first request auto-registers the device as `pending_approval` (see
   `ADMS_AUTO_REGISTER_DEVICES`, `ADMS_REQUIRE_DEVICE_APPROVAL`). While pending, uploads get
   HTTP 403, so **the device keeps its data** until you approve.
2. Approve (admin action, API `POST devices/{id}/approve/`, or `services.approve_device`).
3. Optionally set per-device fields: `timezone`, `groups`, `location`, `options`
   (handshake overrides such as `{"Delay": 30}`), and `fp_algorithm_version` if the device
   doesn't report it.

### Hardening

| Setting | Effect |
|---|---|
| `ADMS_ALLOWED_IPS` | only these IPs/CIDRs may call ADMS endpoints |
| `ADMS_TRUSTED_PROXY_IPS` | take the device IP from `X-Forwarded-For` only when the request comes from these proxies |
| `ADMS_DEVICE_TOKEN_REQUIRED` | require a per-device token (`?token=`, `X-FPA-Device-Token` header, or the device `pushcommkey`); issue with `POST devices/{id}/issue-token/` |
| `ADMS_AUTO_REGISTER_DEVICES=False` | unknown serials are refused; register devices in advance |
| `ADMS_RATE_LIMIT` | per-serial request limit (Django cache) |
| `ADMS_MAX_REQUEST_BYTES` | reject oversized uploads (HTTP 413) |
| `ADMS_URL_PREFIX` | an unguessable path, e.g. `k3j2h/iclock/`, when the firmware lets you set a path |

Most firmware cannot send custom headers or query parameters. Tokens work where
`pushcommkey` is supported. Otherwise rely on IP allowlists, a VPN, or a secret path.

## Checking it works

- The admin device list shows **online**, `last_seen_at`, push version and counts.
- `GET /api/fingerprint/v1/devices/{id}/health/` shows pending commands, pending-upload
  estimate and clock drift.
- `GET /api/fingerprint/v1/device-events/?device=<id>` shows handshakes, OPERLOG and parse
  errors.
