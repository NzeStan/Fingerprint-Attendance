"""Filtering.

Each viewset declares ``filter_spec = {param: (lookup, kind)}``. The same declaration drives
the built-in backend (no dependency) and auto-generated django-filter FilterSets
(``[filters]`` extra), selected by ``API_FILTER_BACKEND``. ``FILTERSET_OVERRIDES`` replaces the
generated FilterSet per viewset.

Kinds: ``str``, ``int``, ``uuid``, ``bool``, ``datetime``, ``date``, ``flag`` (punch flags).
"""

from __future__ import annotations

import importlib.util
import uuid
from typing import Any

from django.db.models import QuerySet
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.exceptions import ValidationError
from rest_framework.filters import BaseFilterBackend

from ..conf import import_object, settings

FilterSpec = dict[str, tuple[str, str]]


def use_django_filter() -> bool:
    mode = settings.API_FILTER_BACKEND or "auto"
    if mode == "builtin":
        return False
    available = importlib.util.find_spec("django_filters") is not None
    return available if mode == "auto" else True


def _convert(kind: str, raw: str, param: str) -> Any:
    try:
        if kind == "int":
            return int(raw)
        if kind == "uuid":
            return uuid.UUID(raw)
        if kind == "bool":
            if raw.lower() in ("1", "true", "yes", "on"):
                return True
            if raw.lower() in ("0", "false", "no", "off"):
                return False
            raise ValueError
        if kind == "datetime":
            value = parse_datetime(raw)
            if value is None and parse_date(raw) is not None:
                value = parse_datetime(f"{raw}T00:00:00")  # a plain date means midnight
            if value is None:
                raise ValueError
            if value.tzinfo is None:
                from django.utils import timezone

                value = timezone.make_aware(value)
            return value
        if kind == "date":
            day = parse_date(raw)
            if day is None:
                raise ValueError
            return day
    except ValueError:
        raise ValidationError({param: f"invalid {kind} value"}) from None
    return raw


def apply_filters(queryset: QuerySet, spec: FilterSpec, params: Any) -> QuerySet:
    needs_distinct = False
    for param, (lookup, kind) in spec.items():
        raw = params.get(param)
        if raw in (None, ""):
            continue
        needs_distinct = needs_distinct or "groups" in lookup
        if kind == "flag":
            for flag in str(raw).split(","):
                queryset = queryset.filter(**{f"{lookup}__contains": f"|{flag.strip()}|"})
            continue
        if lookup.endswith("__in"):
            values = [_convert(kind, v.strip(), param) for v in str(raw).split(",") if v.strip()]
            queryset = queryset.filter(**{lookup: values})
            continue
        queryset = queryset.filter(**{lookup: _convert(kind, str(raw), param)})
    return queryset.distinct() if needs_distinct else queryset


class BuiltinFilterBackend(BaseFilterBackend):
    def filter_queryset(self, request: Any, queryset: QuerySet, view: Any) -> QuerySet:
        spec = getattr(view, "filter_spec", None) or {}
        return apply_filters(queryset, spec, request.query_params)


def build_filterset(model: Any, spec: FilterSpec, name: str) -> Any:
    """Create a django-filter FilterSet class equivalent to ``spec``."""
    import django_filters

    attrs: dict[str, Any] = {}
    for param, (lookup, kind) in spec.items():
        field_name, _, lookup_expr = lookup.rpartition("__") if lookup.rsplit("__", 1)[-1] in (
            "gte", "lte", "gt", "lt", "in", "exact", "icontains", "contains", "date") \
            else (lookup, "", "exact")
        if kind == "flag":
            def method(qs: QuerySet, _name: str, value: str, _lookup: str = lookup) -> QuerySet:
                for flag in str(value).split(","):
                    qs = qs.filter(**{f"{_lookup}__contains": f"|{flag.strip()}|"})
                return qs

            attrs[param] = django_filters.CharFilter(method=method)
            continue
        if lookup_expr == "in":
            base = {"uuid": django_filters.UUIDFilter, "int": django_filters.NumberFilter}.get(
                kind, django_filters.CharFilter)
            cls = type(f"{param}InFilter", (django_filters.BaseInFilter, base), {})
            attrs[param] = cls(field_name=field_name, lookup_expr="in")
            continue
        filter_cls = {
            "int": django_filters.NumberFilter,
            "uuid": django_filters.UUIDFilter,
            "bool": django_filters.BooleanFilter,
            "datetime": django_filters.IsoDateTimeFilter,
            "date": django_filters.DateFilter,
        }.get(kind, django_filters.CharFilter)
        attrs[param] = filter_cls(field_name=field_name, lookup_expr=lookup_expr or "exact",
                                  distinct="groups" in field_name)

    class Meta:
        fields: list[str] = []

    Meta.model = model  # type: ignore[attr-defined]
    attrs["Meta"] = Meta
    return type(name, (django_filters.FilterSet,), attrs)


_filterset_cache: dict[str, Any] = {}


def filterset_for(view: Any) -> Any:
    override = (settings.FILTERSET_OVERRIDES or {}).get(view.basename)
    if override:
        return import_object(override, setting="FILTERSET_OVERRIDES")
    key = f"{view.__class__.__module__}.{view.__class__.__qualname__}"
    if key not in _filterset_cache:
        model = view.get_queryset().model
        _filterset_cache[key] = build_filterset(model, getattr(view, "filter_spec", {}) or {},
                                                f"{model.__name__}AutoFilterSet")
    return _filterset_cache[key]


class ConfiguredDjangoFilterBackend(BaseFilterBackend):
    """django-filter backend using :func:`filterset_for`."""

    def filter_queryset(self, request: Any, queryset: QuerySet, view: Any) -> QuerySet:
        filterset_class = filterset_for(view)
        filterset = filterset_class(data=request.query_params, queryset=queryset,
                                    request=request)
        if not filterset.is_valid():
            raise ValidationError(filterset.errors)
        return filterset.qs


def get_filter_backends() -> list[Any]:
    return [ConfiguredDjangoFilterBackend] if use_django_filter() else [BuiltinFilterBackend]
