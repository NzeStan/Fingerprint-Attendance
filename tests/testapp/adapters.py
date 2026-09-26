"""Example of subclassing the protocol adapter for odd firmware."""

from __future__ import annotations

from fingerprint_attendance.adms.adapter import ADMSProtocolAdapter


class QuirkyAdapter(ADMSProtocolAdapter):
    """Firmware that separates ATTLOG fields with semicolons."""

    def parse_attlog_line(self, line: str):  # type: ignore[no-untyped-def]
        return super().parse_attlog_line(line.replace(";", "\t"))
