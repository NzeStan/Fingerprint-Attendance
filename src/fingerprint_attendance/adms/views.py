"""ADMS / Push protocol endpoints (plain-text Django views; devices do not speak DRF).

Reply semantics are chosen so that data is never lost and never re-sent forever:

* malformed lines are logged and skipped; the rest of the batch is accepted with ``OK``
* when the batch cannot be saved (database error) the reply is HTTP 500 and the upload
  cursor is not advanced, so the device re-sends
* devices awaiting approval get HTTP 403 for data uploads, so they keep their data until
  approved
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from ..conf import settings
from ..constants import DeviceStatus, EventLogType
from ..exceptions import DeviceRejected
from ..ingestion.candidates import PunchCandidate
from ..ingestion.pipeline import ingest_punches
from ..services import devices as device_service
from ..services.commands import fetch_commands, record_results
from ..utils import ratelimit
from ..utils.logging import get_logger, redact
from ..utils.security import client_ip
from .adapter import ADMSProtocolAdapter, get_adapter

logger = get_logger(__name__)


def text(body: str, status: int = 200) -> HttpResponse:
    return HttpResponse(body, status=status, content_type="text/plain; charset=utf-8")


@dataclass
class ADMSContext:
    device: Any
    adapter: ADMSProtocolAdapter
    params: dict[str, str]
    ip: str | None

    @property
    def pending(self) -> bool:
        return bool(self.device.status == DeviceStatus.PENDING_APPROVAL)


def adms_endpoint(view: Callable[[HttpRequest, ADMSContext], HttpResponse] | None = None, *,
                  handshake: bool = False) -> Any:
    """Common handling: enabled flag, size limit, IP/serial/token auth, rate limit, liveness."""
    if view is None:
        return functools.partial(adms_endpoint, handshake=handshake)

    @csrf_exempt
    @functools.wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not settings.ADMS_ENABLED:
            return text("ADMS disabled", 404)
        length = int(request.META.get("CONTENT_LENGTH") or 0)
        if length > settings.ADMS_MAX_REQUEST_BYTES:
            return text("request too large", 413)
        params: dict[str, str] = {str(k): str(v) for k, v in request.GET.items()}
        adapter = get_adapter()
        serial = adapter.get_serial(params)
        if not serial:
            return text("missing SN", 400)
        ip = client_ip(request, settings.ADMS_TRUSTED_PROXY_IPS or [])
        if not ratelimit.allow(f"adms:{serial}", settings.ADMS_RATE_LIMIT):
            return text("rate limited", 429)
        token = (params.get(settings.ADMS_TOKEN_PARAM) or str(request.headers.get("X-FPA-Device-Token") or "")
                 or adapter.get_param(params, "pushcommkey"))
        try:
            device = device_service.resolve_adms_device(serial, ip=ip, token=token)
        except DeviceRejected as exc:
            logger.warning("ADMS request from %s (%s) rejected: %s", serial, ip, exc.code)
            return text(exc.message, exc.status_code)
        adapter = get_adapter(device)
        info = adapter.parse_info_param(adapter.get_param(params, "INFO"))
        device_service.touch_device(device, ip=ip,
                                    push_version=adapter.get_param(params, "pushver",
                                                                   "PushVersion"),
                                    info=info or None,
                                    handshake=handshake and request.method == "GET")
        device_time = adapter.extract_device_time(params, request.headers)
        if device_time is not None:
            device_service.record_clock_drift(device, device_time, source="adms")
        ctx = ADMSContext(device=device, adapter=adapter, params=params, ip=ip)
        try:
            return view(request, ctx)
        except Exception:
            logger.exception("ADMS %s failed for %s", request.path, serial)
            return text("ERROR", 500)

    return wrapper


def _body(request: HttpRequest, ctx: ADMSContext) -> str:
    return ctx.adapter.decode_body(request.body or b"")


def _log_parse_errors(ctx: ADMSContext, table: str, errors: list[Any]) -> None:
    if not errors:
        return
    from ..models import DeviceEventLog

    DeviceEventLog.objects.bulk_create([
        DeviceEventLog(device=ctx.device, event_type=EventLogType.PARSE_ERROR,
                       message=f"{table} line {e.line_no}: {e.error}", data=e.as_dict())
        for e in errors[:200]
    ])
    logger.warning("%s: %d unparseable %s line(s) from %s", table, len(errors), table,
                   ctx.device.serial_number)


# --------------------------------------------------------------------------- cdata


@adms_endpoint(handshake=True)
def cdata(request: HttpRequest, ctx: ADMSContext) -> HttpResponse:
    if request.method == "GET":
        return text(ctx.adapter.render_handshake(ctx.device))
    table = (ctx.adapter.get_param(ctx.params, "table") or "").upper()
    stamp = ctx.adapter.get_param(ctx.params, "Stamp", "OpStamp")
    if table == "OPTIONS":
        info = ctx.adapter.parse_options_body(_body(request, ctx))
        device_service.apply_device_info(ctx.device, info)
        return text(ctx.adapter.render_ok())
    if ctx.pending and not settings.ADMS_ACCEPT_FROM_PENDING_DEVICES:
        # refuse so the device keeps its data until an administrator approves it
        return text("device awaiting approval", 403)
    handler = TABLE_HANDLERS.get(table, handle_unknown_table)
    return handler(request, ctx, table, stamp)


def handle_attlog(request: HttpRequest, ctx: ADMSContext, table: str,
                  stamp: str | None) -> HttpResponse:
    parsed = ctx.adapter.parse_attlog(_body(request, ctx))
    candidates = [PunchCandidate.from_attlog(r, device=ctx.device) for r in parsed.records]
    if ctx.pending:
        for candidate in candidates:
            candidate.flags.add("pending_device")
    with transaction.atomic():
        # bulk_create is chunked inside the pipeline (PUNCH_BULK_CHUNK_SIZE)
        created = ingest_punches(candidates, device=ctx.device).created_count
        _log_parse_errors(ctx, table, parsed.errors)
        # cursor moves in the same transaction as the data it covers
        device_service.advance_cursor(ctx.device, "ATTLOG", stamp)
    logger.info("ATTLOG from %s: %d lines, %d new, %d errors", ctx.device.serial_number,
                parsed.total, created, len(parsed.errors))
    return text(ctx.adapter.render_ok(parsed.total))


def _handle_operlog_batch(ctx: ADMSContext, batch: Any) -> None:
    from ..models import DeviceEventLog, Enrollee
    from ..services.templates import receive_device_template
    from ..sync.engine import mark_present

    if settings.ADMS_STORE_OPERLOG_EVENTS and batch.oplogs:
        DeviceEventLog.objects.bulk_create([
            DeviceEventLog(device=ctx.device, event_type=EventLogType.OPERLOG,
                           op_code=o.op_type[:16], message=redact(o.raw)[:500],
                           data={"admin": o.admin, "objects": o.objects, "time": o.raw_time})
            for o in batch.oplogs
        ])
    if batch.users:
        known = {e.device_pin: e for e in
                 Enrollee.objects.filter(device_pin__in=[u.pin for u in batch.users])}
        for user in batch.users:
            enrollee = known.get(user.pin)
            if enrollee is not None and enrollee.is_active:
                mark_present(ctx.device, enrollee, user=True)
        DeviceEventLog.objects.create(
            device=ctx.device, event_type=EventLogType.USER_INFO,
            message=f"{len(batch.users)} user record(s) reported",
            data={"pins": [u.pin for u in batch.users][:200],
                  "unknown": [u.pin for u in batch.users if u.pin not in known][:200]})
    if batch.templates:
        caps = dict(ctx.device.capabilities or {})
        if any(t.kind == "biodata" for t in batch.templates) and not caps.get("biodata"):
            caps["biodata"] = True
            ctx.device.capabilities = caps
            type(ctx.device).objects.filter(pk=ctx.device.pk).update(capabilities=caps)
        for record in batch.templates:
            receive_device_template(ctx.device, record)


def handle_operlog(request: HttpRequest, ctx: ADMSContext, table: str,
                   stamp: str | None) -> HttpResponse:
    body = _body(request, ctx)
    batch = ctx.adapter.parse_biodata(body) if table in ("BIODATA", "FINGERTMP") \
        else ctx.adapter.parse_operlog(body)
    with transaction.atomic():
        _handle_operlog_batch(ctx, batch)
        _log_parse_errors(ctx, table, batch.errors)
        device_service.advance_cursor(ctx.device, "BIODATA" if table == "BIODATA" else "OPERLOG",
                                      stamp)
    return text(ctx.adapter.render_ok(batch.total))


def handle_attphoto(request: HttpRequest, ctx: ADMSContext, table: str,
                    stamp: str | None) -> HttpResponse:
    # Photos are out of scope for a fingerprint package: acknowledge so the device moves on.
    device_service.advance_cursor(ctx.device, "ATTPHOTO", stamp)
    return text(ctx.adapter.render_ok())


def handle_unknown_table(request: HttpRequest, ctx: ADMSContext, table: str,
                         stamp: str | None) -> HttpResponse:
    from ..models import DeviceEventLog

    DeviceEventLog.objects.create(device=ctx.device, event_type=EventLogType.OTHER,
                                  message=f"unhandled table {table or '(none)'} acknowledged",
                                  data={"table": table, "bytes": len(request.body or b"")})
    return text(ctx.adapter.render_ok())


TABLE_HANDLERS: dict[str, Callable[..., HttpResponse]] = {
    "ATTLOG": handle_attlog,
    "OPERLOG": handle_operlog,
    "BIODATA": handle_operlog,
    "FINGERTMP": handle_operlog,
    "USERINFO": handle_operlog,
    "ATTPHOTO": handle_attphoto,
}

# --------------------------------------------------------------------------- commands


@adms_endpoint
def getrequest(request: HttpRequest, ctx: ADMSContext) -> HttpResponse:
    if ctx.pending:
        return text("OK")
    return text(ctx.adapter.render_commands(fetch_commands(ctx.device)))


@adms_endpoint
def devicecmd(request: HttpRequest, ctx: ADMSContext) -> HttpResponse:
    results = ctx.adapter.parse_devicecmd(_body(request, ctx))
    record_results(ctx.device, results)
    return text("OK")


# --------------------------------------------------------------------------- newer firmware


@adms_endpoint
def ping(request: HttpRequest, ctx: ADMSContext) -> HttpResponse:
    return text("OK")


@adms_endpoint
def registry(request: HttpRequest, ctx: ADMSContext) -> HttpResponse:
    """Push 3.x registration: device posts its info, server returns a RegistryCode."""
    import secrets

    info = ctx.adapter.parse_options_body(_body(request, ctx))
    if info:
        device_service.apply_device_info(ctx.device, info)
    caps = dict(ctx.device.capabilities or {})
    code = caps.get("registry_code") or secrets.token_hex(8)
    caps["registry_code"] = code
    type(ctx.device).objects.filter(pk=ctx.device.pk).update(capabilities=caps)
    return text(ctx.adapter.render_registry(ctx.device, code))


@adms_endpoint
def push(request: HttpRequest, ctx: ADMSContext) -> HttpResponse:
    """Push 3.x: the device asks for its configuration (same content as the handshake)."""
    return text(ctx.adapter.render_handshake(ctx.device).split("\n", 1)[1])


@adms_endpoint
def querydata(request: HttpRequest, ctx: ADMSContext) -> HttpResponse:
    """Push 3.x responses to ``DATA QUERY``: the body uses OPERLOG-style lines."""
    body = _body(request, ctx)
    batch = ctx.adapter.parse_operlog(body)
    with transaction.atomic():
        _handle_operlog_batch(ctx, batch)
        _log_parse_errors(ctx, "QUERYDATA", batch.errors)
    return text(ctx.adapter.render_ok(batch.total))


@adms_endpoint
def rtdata(request: HttpRequest, ctx: ADMSContext) -> HttpResponse:
    """Push 3.x time sync (``type=rttime``)."""
    kind = (ctx.adapter.get_param(ctx.params, "type") or "").lower()
    if kind == "rttime":
        return text(ctx.adapter.render_rttime(ctx.device))
    return text("OK")


@adms_endpoint
def fallback(request: HttpRequest, ctx: ADMSContext) -> HttpResponse:
    """Any other endpoint under the prefix (``fdata``, ``edata``, ``exchange``...) is
    acknowledged so the device does not retry forever; see docs for unsupported features."""
    return handle_unknown_table(request, ctx, request.path.rsplit("/", 1)[-1].upper(), None)
