"""Holiday providers.

* :class:`SettingsHolidayProvider` -- static list in ``HOLIDAYS``
* :class:`DatabaseHolidayProvider` -- the ``Holiday`` model (admin/API editable at runtime)
* :class:`PythonHolidaysProvider` -- the ``holidays`` library (``[holidays]`` extra)
* :class:`ChainedHolidayProvider` -- combines providers; the first provider with an entry for a
  date wins, so database rows can add, move (cancel + add) or cancel library holidays without
  a deploy.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..conf import import_object, settings
from ..utils.timeutils import daterange
from .types import HolidayInfo


class BaseHolidayProvider:
    """Return holidays (including cancellation overrides) for an inclusive date range."""

    def get_holidays(self, start: date, end: date, *, enrollee: Any = None) -> dict[date,
                                                                                    HolidayInfo]:
        raise NotImplementedError

    def get_holiday(self, day: date, *, enrollee: Any = None) -> HolidayInfo | None:
        info = self.get_holidays(day, day, enrollee=enrollee).get(day)
        if info is None or info.cancelled:
            return None
        return info


def _parse_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _recurring_dates(month: int, day: int, start: date, end: date) -> list[date]:
    out = []
    for year in range(start.year, end.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:  # 29 Feb in a non-leap year
            continue
        if start <= candidate <= end:
            out.append(candidate)
    return out


class SettingsHolidayProvider(BaseHolidayProvider):
    def get_holidays(self, start: date, end: date, *, enrollee: Any = None) -> dict[date,
                                                                                    HolidayInfo]:
        out: dict[date, HolidayInfo] = {}
        for item in settings.HOLIDAYS or []:
            base = _parse_date(item["date"])
            dates = (_recurring_dates(base.month, base.day, start, end) if item.get("recurring")
                     else ([base] if start <= base <= end else []))
            for d in dates:
                out[d] = HolidayInfo(date=d, name=str(item.get("name", "Holiday")),
                                     is_paid=bool(item.get("is_paid", True)),
                                     cancelled=bool(item.get("cancelled", False)),
                                     source="settings")
        return out


class DatabaseHolidayProvider(BaseHolidayProvider):
    """Holiday rows. Scope specificity: enrollee > device group > everyone."""

    def get_holidays(self, start: date, end: date, *, enrollee: Any = None) -> dict[date,
                                                                                    HolidayInfo]:
        from django.db.models import Q

        from ..constants import HolidayScope
        from ..models import Holiday

        scope_q = Q(scope=HolidayScope.ALL)
        if enrollee is not None:
            scope_q |= Q(scope=HolidayScope.ENROLLEE, enrollee=enrollee)
            group_ids = list(enrollee.groups.values_list("pk", flat=True))
            if group_ids:
                scope_q |= Q(scope=HolidayScope.DEVICE_GROUP, device_group_id__in=group_ids)
        rows = Holiday.objects.filter(scope_q).filter(
            Q(date__range=(start, end)) | Q(recurring=True))
        rank: dict[str, int] = {HolidayScope.ENROLLEE: 3, HolidayScope.DEVICE_GROUP: 2,
                                HolidayScope.ALL: 1}
        best: dict[date, tuple[int, HolidayInfo]] = {}
        for row in rows:
            dates = (_recurring_dates(row.date.month, row.date.day, start, end) if row.recurring
                     else ([row.date] if start <= row.date <= end else []))
            for d in dates:
                score = rank.get(row.scope, 0)
                current = best.get(d)
                if current is None or score > current[0]:
                    best[d] = (score, HolidayInfo(
                        date=d, name=row.name, is_paid=row.is_paid, cancelled=row.is_cancelled,
                        source="database", metadata={"holiday_id": str(row.uuid),
                                                     "scope": row.scope}))
        return {d: info for d, (_, info) in best.items()}


class PythonHolidaysProvider(BaseHolidayProvider):
    """Wraps the ``holidays`` package (``HOLIDAYS_COUNTRY``, ``HOLIDAYS_SUBDIVISION``)."""

    def __init__(self, country: str | None = None, subdivision: str | None = None) -> None:
        self.country = country or settings.HOLIDAYS_COUNTRY
        self.subdivision = subdivision or settings.HOLIDAYS_SUBDIVISION

    def get_holidays(self, start: date, end: date, *, enrollee: Any = None) -> dict[date,
                                                                                    HolidayInfo]:
        import holidays as holidays_lib

        if not self.country:
            return {}
        calendar = holidays_lib.country_holidays(
            self.country, subdiv=self.subdivision, years=range(start.year, end.year + 1))
        return {
            d: HolidayInfo(date=d, name=str(name), source="holidays")
            for d, name in calendar.items()
            if start <= d <= end
        }


class ChainedHolidayProvider(BaseHolidayProvider):
    """Highest priority first. The first provider with *any* entry for a date (including a
    cancellation) decides that date."""

    def __init__(self, providers: list[BaseHolidayProvider] | None = None) -> None:
        if providers is None:
            providers = []
            for path in settings.HOLIDAY_PROVIDER_CHAIN or []:
                obj = import_object(settings.resolve_path("HOLIDAY_PROVIDER_CHAIN", path),
                                    setting="HOLIDAY_PROVIDER_CHAIN")
                providers.append(obj() if isinstance(obj, type) else obj)
        self.providers = providers

    def get_holidays(self, start: date, end: date, *, enrollee: Any = None) -> dict[date,
                                                                                    HolidayInfo]:
        result: dict[date, HolidayInfo] = {}
        for provider in self.providers:
            for d, info in provider.get_holidays(start, end, enrollee=enrollee).items():
                result.setdefault(d, info)
        return result


def dates_in(start: date, end: date) -> list[date]:
    return daterange(start, end)
