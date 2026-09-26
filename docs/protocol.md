# Protocol notes

The ZKTeco Push/ADMS protocol has no single public specification, and firmware varies. This
page records what the default `ADMSProtocolAdapter` does, what is covered by fixture tests, and
what is still uncertain. Anything uncertain is isolated in an adapter method you can override.
A device can use its own adapter via `device.options["protocol_adapter"]`.

## Endpoints

| Endpoint | Direction | Handling |
|---|---|---|
| `GET cdata?SN=&options=all&pushver=` | handshake | returns `GET OPTION FROM: <SN>` plus options: stamps (`ATTLOGStamp`, `OPERLOGStamp`, `ATTPHOTOStamp`, `BIODATAStamp`), `ErrorDelay`, `Delay`, `TransTimes`, `TransInterval`, `TransFlag`, `TimeZone`, `Realtime`, `Encrypt`, `ServerVer`, `PushProtVer`, `ADMS_HANDSHAKE_EXTRA_OPTIONS`, and per-device `options` |
| `POST cdata?table=ATTLOG&Stamp=` | punches | tab-separated lines `PIN, time, status, verify, workcode, reserved...`; extra columns kept |
| `POST cdata?table=OPERLOG` | ops + enrollment | `OPLOG`, `USER`, `FP`, `BIODATA` lines |
| `POST cdata?table=BIODATA` / `FINGERTMP` / `USERINFO` | templates | same parser; lines may omit the `BIODATA ` prefix |
| `POST cdata?table=ATTPHOTO` | photos | acknowledged and ignored (out of scope); cursor advanced |
| `POST cdata?table=options` | device info | `~DeviceName=..,FWVersion=..,UserCount=..` |
| `GET getrequest?INFO=` | command poll | `INFO` is parsed positionally (firmware, users, fps, logs, IP, fp algorithm, ...); returns `C:<id>:<cmd>` lines or `OK` |
| `POST devicecmd` | command results | `ID=..&Return=..&CMD=..` per line; lines after an `INFO` result are key/values |
| `GET ping` | keep-alive | `OK` |
| `POST registry` | push 3.x | stores device info, returns stable `RegistryCode=` |
| `POST push` | push 3.x | configuration (handshake body without the header line) |
| `POST querydata` | push 3.x | replies to `DATA QUERY` in OPERLOG-style lines |
| `GET rtdata?type=rttime` | push 3.x | `DateTime=<zk-encoded>,ServerTZ=+HHMM` |
| anything else under the prefix | `fdata`, `edata`, `exchange`, ... | logged and acknowledged so devices do not retry forever |

`cdata.aspx` and trailing slashes are accepted.

## Reply semantics

- Parsed OK, even with some bad lines → `OK: <lines>`. Bad lines are logged as
  `parse_error` device events and never cause a re-send loop.
- Database failure → HTTP 500 and the cursor is not advanced, so the device re-sends.
- Device pending approval → HTTP 403 for data, so the device keeps its logs.
- Disabled, unknown (no auto-register), bad token or disallowed IP → HTTP 403.

## Commands

| Type | Push 2.x (`FINGERTMP`) | BIODATA firmware | Legacy (`options.legacy_commands`) |
|---|---|---|---|
| `add_user` | `DATA UPDATE USERINFO PIN=\tName=\tPri=...` | same | `DATA USER ...` |
| `delete_user` | `DATA DELETE USERINFO PIN=` | same | `DATA DEL_USER PIN=` |
| `add_template` | `DATA UPDATE FINGERTMP PIN=\tFID=\tSize=\tValid=1\tTMP=` | `DATA UPDATE BIODATA Pin=\tNo=\tIndex=0\tValid=1\tDuress=\tType=1\tMajorVer=\tMinorVer=\tFormat=0\tTmp=` | `DATA FP ...` |
| `delete_template` | `DATA DELETE FINGERTMP PIN=\tFID=` | `DATA DELETE BIODATA Pin=\tType=1\tNo=` | `DATA DEL_FP ...` |
| `enroll_fingerprint` | `ENROLL_FP PIN=\tFID=\tRETRY=\tOVERWRITE=` | `ENROLL_BIO TYPE=1\tPIN=\tNO=\tRETRY=\tOVERWRITE=` | |
| `query_users` / `query_templates` / `query_attlog` | `DATA QUERY USERINFO/FINGERTMP/ATTLOG ...` | `DATA QUERY BIODATA ...` | |
| `clear_logs` / `clear_data` | `CLEAR LOG` / `CLEAR DATA` (safety-gated) | | |
| `set_time` | `SET OPTION DateTime=<zk-encoded>` (or ISO with `options.set_time_format="iso"`) | | |
| others | `REBOOT`, `INFO`, `CHECK`, `RELOAD OPTIONS`, `LOG`, `SET OPTION k=v`, `raw` | | |

BIODATA is used when the device reports push version ≥ 2.4.0, has ever uploaded BIODATA
(`capabilities.biodata`), or sets `options.biodata`.

## Open protocol questions

These are handled defensively and are overridable. Please report firmware behaviour.

1. **Setting the clock.** There is no universal command. The default renders
   `SET OPTION DateTime=<ZK-encoded>`. Some firmware wants ISO text, and many devices instead
   sync from the HTTP `Date` header of server replies or from `rtdata?type=rttime`. Override
   `render_set_time`.
2. **Device time in requests.** Few firmwares send their clock. The adapter recognises
   `DeviceTime`/`DevTime` parameters and an `X-Device-Time` header. Otherwise drift is known
   only in pull mode or from `INFO`-type data. `extract_device_time` is overridable.
3. **`Size` in `FINGERTMP`.** The default sends the base64 text length, which matches our
   fixtures. Some documents describe the binary size. Override `template_size_value`.
4. **`MaxAttLogCount` units.** Values below 1000 are treated as ×10,000 records.
5. **`BIODATA` `Index`.** The default always sends `Index=0` (one template per finger).
6. **Stamps.** ATTLOG stamps are treated as opaque strings echoed back in the handshake.
   "Force re-upload" sets them to `0`. Firmware that ignores stamp resets still re-sends on
   `DATA QUERY ATTLOG`, which the re-upload action also queues when a start date is given.
7. **Encrypted push (`exchange`, `Encrypt=1`).** Not supported; `Encrypt=None` is sent.
8. **Photos (`ATTPHOTO`, `fdata`).** Acknowledged and discarded (fingerprint-only scope).

Fixtures for each supported variant live in `tests/fixtures/adms/`, and every parser and
builder is covered by `tests/test_adapter.py`.
