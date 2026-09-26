"""A fake ZKTeco ADMS device for tests (yours and ours).

It keeps local state like a real terminal (users, templates, a punch log with upload cursor),
talks to the server over HTTP through any client with Django test ``Client`` semantics, and can
be taken offline::

    from django.test import Client
    from fingerprint_attendance.testing import ADMSDeviceSimulator

    device = ADMSDeviceSimulator(Client(), serial="SIM001", firmware="biodata")
    device.handshake()
    device.punch("1001", datetime(2026, 1, 5, 8, 0))       # uploaded immediately
    device.go_offline()
    device.punch("1001", datetime(2026, 1, 5, 17, 0))      # stored locally
    device.go_online()                                      # handshake + catch-up upload
    device.run_commands()                                   # poll getrequest, apply, ack

Firmware profiles:

``legacy``   no push version, ``FP`` lines, 5-field ATTLOG, ``DATA FP``-era devices
``push2``    push 2.2, ``FP`` lines in OPERLOG, 7-field ATTLOG
``biodata``  push 2.4.1, ``BIODATA`` lines, 10-field ATTLOG (mask/temperature columns)
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

FIRMWARE: dict[str, dict[str, Any]] = {
    "legacy": {"pushver": None, "template_line": "FP", "attlog_fields": 5},
    "push2": {"pushver": "2.2.14", "template_line": "FP", "attlog_fields": 7},
    "biodata": {"pushver": "2.4.1", "template_line": "BIODATA", "attlog_fields": 10},
}


@dataclass
class SimulatedPunch:
    pin: str
    when: datetime
    status: int = 0
    verify: int = 1
    work_code: str = "0"


@dataclass
class SimulatorResponse:
    status: int
    text: str


@dataclass
class ADMSDeviceSimulator:
    client: Any
    serial: str = "SIM0001"
    firmware: str = "push2"
    prefix: str = "/iclock/"
    fp_version: str = "10"
    token: str | None = None
    ip: str = "10.0.0.50"
    realtime: bool = True
    #: callable(pin, finger) -> template bytes, used when the server asks for remote enrollment
    enroll_factory: Callable[[str, int], bytes] | None = None
    #: command return codes to force: {"DATA": -1} makes every DATA command fail
    fail_commands: dict[str, int] = field(default_factory=dict)

    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    templates: dict[tuple[str, int], bytes] = field(default_factory=dict)
    log: list[SimulatedPunch] = field(default_factory=list)
    uploaded: int = 0  # number of log records the server acknowledged
    options: dict[str, str] = field(default_factory=dict)
    online: bool = True
    executed: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ transport
    @property
    def profile(self) -> dict[str, Any]:
        return FIRMWARE[self.firmware]

    def _params(self, **extra: Any) -> dict[str, Any]:
        params: dict[str, Any] = {"SN": self.serial}
        if self.token:
            params["token"] = self.token
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _resp(self, response: Any) -> SimulatorResponse:
        body = response.content.decode("utf-8") if hasattr(response, "content") else \
            str(response.text)
        return SimulatorResponse(int(response.status_code), body)

    def get(self, endpoint: str, **params: Any) -> SimulatorResponse:
        self._require_online()
        return self._resp(self.client.get(f"{self.prefix}{endpoint}", self._params(**params),
                                          REMOTE_ADDR=self.ip))

    def post(self, endpoint: str, body: str, **params: Any) -> SimulatorResponse:
        self._require_online()
        url = f"{self.prefix}{endpoint}?{urlencode(self._params(**params))}"
        return self._resp(self.client.post(url, data=body.encode("utf-8"),
                                           content_type="text/plain", REMOTE_ADDR=self.ip))

    def _require_online(self) -> None:
        if not self.online:
            raise ConnectionError(f"simulated device {self.serial} is offline")

    # ------------------------------------------------------------------ protocol
    def info(self) -> str:
        return ",".join(str(x) for x in (
            "Ver 8.0.4-SIM", len(self.users), len(self.templates), len(self.log), self.ip,
            self.fp_version, 7, 0, 0, "1100000000"))

    def handshake(self) -> dict[str, str]:
        response = self.get("cdata", options="all", pushver=self.profile["pushver"],
                            language="69")
        if response.status != 200:
            raise AssertionError(f"handshake failed: {response.status} {response.text}")
        lines = response.text.strip().splitlines()
        self.options = dict(line.split("=", 1) for line in lines[1:] if "=" in line)
        stamp = self.options.get("ATTLOGStamp", "None")
        # The server is the source of truth for the cursor (supports forced re-upload).
        self.uploaded = int(stamp) if stamp.isdigit() else 0
        self.uploaded = min(self.uploaded, len(self.log))
        return self.options

    def _attlog_line(self, p: SimulatedPunch) -> str:
        fields = [p.pin, p.when.strftime("%Y-%m-%d %H:%M:%S"), str(p.status), str(p.verify),
                  p.work_code, "0", "0", "0", "36.5", "0"]
        return "\t".join(fields[: self.profile["attlog_fields"]])

    def punch(self, pin: str, when: datetime, *, status: int = 0, verify: int = 1,
              upload: bool | None = None) -> SimulatedPunch:
        record = SimulatedPunch(pin=str(pin), when=when, status=status, verify=verify)
        self.log.append(record)
        if (self.realtime if upload is None else upload) and self.online:
            self.upload_pending()
        return record

    def pending(self) -> list[SimulatedPunch]:
        return self.log[self.uploaded:]

    def upload_pending(self, *, batch_size: int = 500, corrupt_line: int | None = None,
                       max_batches: int | None = None) -> list[SimulatorResponse]:
        """Upload records after the cursor in batches; stops at the first non-200 reply."""
        responses = []
        batches = 0
        while self.uploaded < len(self.log):
            if max_batches is not None and batches >= max_batches:
                break
            chunk = self.log[self.uploaded:self.uploaded + batch_size]
            lines = [self._attlog_line(p) for p in chunk]
            if corrupt_line is not None and corrupt_line < len(lines):
                lines[corrupt_line] = "garbage\tnot-a-date"
            stamp = self.uploaded + len(chunk)
            response = self.post("cdata", "\n".join(lines) + "\n", table="ATTLOG",
                                 Stamp=stamp)
            responses.append(response)
            batches += 1
            if response.status != 200:
                break
            self.uploaded = stamp
        return responses

    def push_user(self, pin: str, name: str = "", privilege: int = 0) -> SimulatorResponse:
        self.users[pin] = {"name": name, "privilege": privilege}
        line = f"USER PIN={pin}\tName={name}\tPri={privilege}\tPasswd=\tCard=\tGrp=1\tTZ=0"
        return self.post("cdata", line + "\n", table="OPERLOG", Stamp=len(self.executed) + 1)

    def template_line(self, pin: str, finger: int, data: bytes) -> str:
        b64 = base64.b64encode(data).decode("ascii")
        if self.profile["template_line"] == "BIODATA":
            return (f"BIODATA Pin={pin}\tNo={finger}\tIndex=0\tValid=1\tDuress=0\tType=1"
                    f"\tMajorVer={self.fp_version}\tMinorVer=0\tFormat=0\tTmp={b64}")
        return f"FP PIN={pin}\tFID={finger}\tSize={len(b64)}\tValid=1\tTMP={b64}"

    def enroll_at_device(self, pin: str, finger: int, data: bytes | None = None, *,
                         name: str = "") -> SimulatorResponse:
        """Walk-up enrollment on the device menu; uploads USER + template via OPERLOG."""
        data = data or os.urandom(512)
        self.users.setdefault(pin, {"name": name, "privilege": 0})
        self.templates[(pin, finger)] = data
        body = (f"OPLOG 6\t0\t{datetime.now():%Y-%m-%d %H:%M:%S}\t{pin}\t{finger}\t0\t0\n"
                f"USER PIN={pin}\tName={name}\tPri=0\tPasswd=\tCard=\tGrp=1\tTZ=0\n"
                f"{self.template_line(pin, finger, data)}\n")
        table = "BIODATA" if self.profile["template_line"] == "BIODATA" else "OPERLOG"
        return self.post("cdata", body, table=table, Stamp=len(self.templates))

    def ping(self) -> SimulatorResponse:
        return self.get("ping")

    # ------------------------------------------------------------------ commands
    def poll(self) -> list[tuple[int, str]]:
        response = self.get("getrequest", INFO=self.info())
        if response.status != 200 or response.text.strip() == "OK":
            return []
        commands = []
        for line in response.text.strip().splitlines():
            if not line.startswith("C:"):
                continue
            _, cid, text = line.split(":", 2)
            commands.append((int(cid), text))
        return commands

    def _kv(self, text: str) -> dict[str, str]:
        return dict(p.split("=", 1) for p in text.split("\t") if "=" in p)

    def apply(self, text: str) -> tuple[int, str]:
        """Apply a command to local state; returns (return code, extra body)."""
        self.executed.append(text)
        verb = text.split(" ", 1)[0]
        if verb in self.fail_commands:
            return self.fail_commands[verb], ""
        upper = text.upper()
        extra = ""
        if upper.startswith(("DATA UPDATE USERINFO", "DATA USER ")):
            kv = self._kv(text.split(" ", 3)[-1] if upper.startswith("DATA UPDATE") else
                          text.split(" ", 2)[-1])
            self.users[kv["PIN"]] = {"name": kv.get("Name", ""),
                                     "privilege": int(kv.get("Pri", 0))}
        elif upper.startswith(("DATA DELETE USERINFO", "DATA DEL_USER")):
            pin = self._kv(text.split(" ", 3)[-1] if "DELETE" in upper
                           else text.split(" ", 2)[-1])["PIN"]
            self.users.pop(pin, None)
            for key in [k for k in self.templates if k[0] == pin]:
                self.templates.pop(key)
        elif upper.startswith(("DATA UPDATE FINGERTMP", "DATA FP ")):
            kv = self._kv(text.split(" ", 3)[-1] if "UPDATE" in upper else text.split(" ", 2)[-1])
            if kv["PIN"] not in self.users:
                return -1, ""
            self.templates[(kv["PIN"], int(kv["FID"]))] = base64.b64decode(kv["TMP"])
        elif upper.startswith("DATA UPDATE BIODATA"):
            kv = self._kv(text.split(" ", 3)[-1])
            if kv["Pin"] not in self.users:
                return -1, ""
            self.templates[(kv["Pin"], int(kv["No"]))] = base64.b64decode(kv["Tmp"])
        elif upper.startswith(("DATA DELETE FINGERTMP", "DATA DEL_FP", "DATA DELETE BIODATA")):
            kv = self._kv(text.split(" ", 3)[-1])
            pin = str(kv.get("PIN") or kv.get("Pin"))
            finger = int(kv.get("FID") or kv.get("No") or 0)
            self.templates.pop((str(pin), finger), None)
        elif upper.startswith(("ENROLL_FP", "ENROLL_BIO")):
            kv = self._kv(text.split(" ", 1)[1])
            pin = str(kv.get("PIN"))
            finger = int(kv.get("FID") or kv.get("NO") or 0)
            factory = self.enroll_factory or (lambda p, f: os.urandom(512))
            self._pending_enrolls.append((str(pin), finger, factory(str(pin), finger)))
        elif upper == "CLEAR LOG":
            self.log.clear()
            self.uploaded = 0
        elif upper == "CLEAR DATA":
            self.log.clear()
            self.uploaded = 0
            self.users.clear()
            self.templates.clear()
        elif upper == "INFO":
            extra = (f"~DeviceName=SIM-{self.firmware}\nFWVersion=Ver 8.0.4-SIM\n"
                     f"UserCount={len(self.users)}\nFPCount={len(self.templates)}\n"
                     f"TransactionCount={len(self.log)}\nFPVersion={self.fp_version}\n"
                     f"~MaxAttLogCount=10\n")
        elif upper.startswith("DATA QUERY ATTLOG"):
            self.uploaded = 0  # re-send stored logs on next upload
        return 0, extra

    _pending_enrolls: list[tuple[str, int, bytes]] = field(default_factory=list)

    def ack(self, results: list[tuple[int, int, str, str]]) -> SimulatorResponse:
        lines = []
        for cid, code, cmd, extra in results:
            lines.append(f"ID={cid}&Return={code}&CMD={cmd}")
            if extra:
                lines.append(extra.rstrip("\n"))
        return self.post("devicecmd", "\n".join(lines) + "\n")

    def run_commands(self, *, max_rounds: int = 20, ack: bool = True) -> list[str]:
        """Poll, apply and acknowledge commands until the queue is empty."""
        applied: list[str] = []
        for _ in range(max_rounds):
            commands = self.poll()
            if not commands:
                break
            results = []
            for cid, text in commands:
                code, extra = self.apply(text)
                results.append((cid, code, text.split(" ", 1)[0], extra))
                applied.append(text)
            if ack:
                self.ack(results)
            for pin, finger, data in self._pending_enrolls:
                self.enroll_at_device(pin, finger, data)
            self._pending_enrolls.clear()
        return applied

    # ------------------------------------------------------------------ connectivity
    def go_offline(self) -> None:
        self.online = False

    def go_online(self, *, catch_up: bool = True) -> list[SimulatorResponse]:
        self.online = True
        self.handshake()
        return self.upload_pending() if catch_up else []
